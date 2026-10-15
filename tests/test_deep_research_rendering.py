"""Re-render only exact generated snapshots, never arbitrary edited Markdown."""
import copy
import hashlib
import json
import unittest
from unittest.mock import MagicMock

from src.deep_research_workflow import generate_deep_research_report, render_deep_research_body
from src.research_packet import _render_packet_appendix, render_packet_appendix
import test_deep_research_workflow as deep_fixtures
from test_deep_research_workflow import AS_OF, TOPIC, Stream, design, evidence, sources
from test_research_workflow import draft as legacy_draft


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class DeepRenderingTests(unittest.TestCase):
    def argumentative(self, *, fail_review=False):
        case = deep_fixtures.DeepResearchTests()
        return case.generate(case.client(revise=True, fail_at=5 if fail_review else None))

    def legacy(self, *, gap=False):
        client = MagicMock()
        def response(**kwargs):
            payload = json.loads(kwargs["messages"][1]["content"])
            if "available_sources" in payload:
                value = design()
            elif "previous_items" in payload:
                value = evidence()
            elif "paragraphs" in payload:
                value = {"claims": [{"section": row["section"], "paragraph": row["paragraph"],
                    "status": "unknown", "evidence_ids": [], "calculation_ids": [],
                    "reason": "This synthetic claim needs checking."} for row in payload["paragraphs"]],
                    "summary": "Synthetic unresolved audit."}
            elif "writer_draft" in payload:
                value = {"draft": legacy_draft(), "findings": [], "summary": "Synthetic review retained."}
            else:
                value = legacy_draft()
            return Stream(value)
        client.chat.completions.create.side_effect = response
        return generate_deep_research_report(TOPIC, sources(), api_key="synthetic-key", as_of=AS_OF,
            client=client, retrieve_sources=False, writing_style="legacy", report_mode="gap" if gap else "legacy")

    def test_argumentative_and_legacy_normal_generation_use_same_idempotent_renderer(self):
        for report in (self.argumentative(), self.legacy(), self.legacy(gap=True)):
            with self.subTest(style=report["metadata"].get("report_mode")):
                before = copy.deepcopy(report)
                draft_hash = fingerprint(report["metadata"]["structured_draft"])
                rendered = render_deep_research_body(report)
                self.assertEqual(rendered, report["body"])
                report["body"] = rendered
                self.assertEqual(render_deep_research_body(report), rendered)
                self.assertEqual(report, before)
                self.assertEqual(fingerprint(report["metadata"]["structured_draft"]), draft_hash)
                self.assertEqual(rendered.count("## 研究設計與證據紀錄"), 1)

    def test_old_calculation_appendix_refreshes_without_changing_any_structured_field(self):
        report = self.argumentative()
        metadata = report["metadata"]
        packet = metadata["research_packet"]
        # Build the exact previous application representation from structured
        # inputs; the refresh itself never splits or edits the existing body.
        old_limits = "沒有來源提供集中度或宏觀數值。"
        packet["calculation_limits"] = [old_limits]
        packet["packet_hash"] = fingerprint({key: value for key, value in packet.items() if key != "packet_hash"})
        from src.argumentative_workflow import _body
        from src.deep_research_workflow import _DEEP_GAP_NOTICE, _audit_findings
        from src.research_packet import packet_findings
        generated = [*packet_findings(packet), *_audit_findings(metadata["claim_audit"])]
        publication_findings = [row for row in metadata["review_findings"] if row not in generated]
        core = _body(metadata["structured_draft"], report["sources"], AS_OF,
            plan=metadata["argument_plan"], warnings=metadata["validation_warnings"],
            findings=publication_findings, summary=metadata["review_summary"], stage=metadata["workflow_stage"])
        report["body"] = _DEEP_GAP_NOTICE + core + _render_packet_appendix(packet, historical_calculation_notes=True)
        self.assertIn(old_limits, report["body"])
        before = copy.deepcopy(report)
        draft_hash = fingerprint(metadata["structured_draft"])
        rendered = render_deep_research_body(report)
        self.assertNotIn(old_limits, rendered)
        self.assertTrue(rendered.endswith(render_packet_appendix(packet)))
        self.assertIn("不代表原始材料沒有數字", rendered)
        self.assertEqual(report, before)
        self.assertEqual(fingerprint(metadata["structured_draft"]), draft_hash)
        report["body"] = rendered
        self.assertEqual(render_deep_research_body(report), rendered)

    def test_failed_review_snapshot_keeps_failure_status_and_message(self):
        report = self.argumentative(fail_review=True)
        before = copy.deepcopy(report)
        rendered = render_deep_research_body(report)
        self.assertIn("後續審稿未完成", rendered)
        self.assertIn(report["metadata"]["review_error"], rendered)
        self.assertEqual(report["metadata"]["review_status"], "review_failed")
        self.assertEqual(report, before)

    def test_human_edits_and_unmatched_snapshots_are_rejected_without_mutation(self):
        original = self.argumentative()
        variants = []
        for change in (
            lambda row: row.update(body=row["body"] + "\nHuman appended analysis."),
            lambda row: row.update(body=row["body"].replace("Orders", "Edited orders", 1)),
            lambda row: row.update(title="Human title"),
            lambda row: row.update(as_of="2026-10-13"),
            lambda row: row["sources"][0].update(content="Changed evidence."),
            lambda row: row["metadata"].update(structured_draft=None),
            lambda row: row["metadata"].update(restoration={"from_version": 1}),
            lambda row: row["metadata"].update(manually_verified=True),
            lambda row: row.update(version=3, metadata={**row["metadata"], "reviewed_version": 2}),
        ):
            edited = copy.deepcopy(original)
            change(edited)
            variants.append(edited)
        for index, edited in enumerate(variants):
            before = copy.deepcopy(edited)
            with self.subTest(index=index), self.assertRaises(ValueError):
                render_deep_research_body(edited)
            self.assertEqual(edited, before)


if __name__ == "__main__":
    unittest.main()
