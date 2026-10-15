"""Deterministic end-to-end deep workflow; no live providers or data services."""
import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from src.deep_research_workflow import research_budget
from src.research import prepare_sources
from src.research_packet import prepare_research_packet, render_packet_appendix
from src.research_workflow import generate_research_workflow
from test_argumentation import essay, plan

TOPIC = "Orders and profits"
AS_OF = "2026-10-14"


def sources():
    return prepare_sources([{"id": i, "title": title, "content": content,
        "published_at": "2026-10-10", "content_scope": "provided_text"}
        for i, title, content in [(1, "Orders", "Orders improved in the disclosed business."),
                                  (2, "Costs", "Costs constrained margins in the disclosed business.")]])


def design():
    return {"version": 1, "topic": TOPIC, "as_of": AS_OF, "markets": ["us", "hk", "cn"],
        "questions": [{"id": "Q1", "question": "What supports the operating outlook?"}],
        "requirements": [{"id": "R1", "question_id": "Q1", "description": "Operating evidence",
                          "kind": "document", "critical": True}], "methods": []}


def evidence():
    return {"items": [{"id": f"E{i}", "kind": "document", "requirement_id": "R1",
        "source_id": source["id"], "quote": source["content"], "statement": source["content"],
        "event_status": "unknown"} for i, source in enumerate(sources(), 1)]}


class Stream:
    def __init__(self, value): self.value = value
    def __iter__(self):
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=json.dumps(self.value)), finish_reason="stop")])
    def close(self): pass


class DeepResearchTests(unittest.TestCase):
    def client(self, *, revise=False, fail_at=None):
        client = MagicMock()
        self.payloads = []
        def respond(**kwargs):
            payload = json.loads(kwargs["messages"][1]["content"])
            self.payloads.append(payload)
            if fail_at == len(self.payloads):
                raise RuntimeError("synthetic-secret-provider-failure")
            if "available_sources" in payload:
                value = design()
            elif "previous_items" in payload:
                value = evidence()
            elif "paragraphs" in payload:
                value = {"claims": [{"section": p["section"], "paragraph": p["paragraph"],
                    "status": "supported", "evidence_ids": ["E1", "E2"], "calculation_ids": [],
                    "reason": "Compared with the frozen source excerpts."} for p in payload["paragraphs"]],
                    "summary": "Compared with evidence."}
            elif "writer_draft" in payload:
                draft = essay()
                if revise:
                    draft["title"] = "Revised operating outlook"
                value = {"draft": draft, "findings": [], "summary": "Reviewed against sources.",
                         "quality": {**{key: 3 for key in ("relevance", "evidence", "reasoning", "readability")}, "verdict": "pass"}}
            elif "argument_plan" in payload:
                value = essay()
            else:
                value = plan()
            return Stream(value)
        client.chat.completions.create.side_effect = respond
        return client

    def generate(self, client, **kwargs):
        return generate_research_workflow(TOPIC, sources(), api_key="synthetic-key", as_of=AS_OF,
            deep_research=True, writing_style="argumentative", report_mode="argumentative",
            retrieve_sources=False, client=client, **kwargs)

    def test_budgets_are_bounded_and_defaults_are_not_mutated(self):
        self.assertEqual(research_budget()["max_calls"], 16)
        for value in ({"max_calls": True}, {"max_calls": 25}, {"max_rounds": 4},
                      {"timeout_seconds": 119}, {"extra": 1}):
            with self.subTest(value=value), self.assertRaises(ValueError): research_budget(value)
        research_budget()["max_calls"] = 6
        self.assertEqual(research_budget()["max_calls"], 16)

    def test_real_writers_receive_packet_and_review_receives_claim_audit(self):
        saved = []
        client = self.client()
        result = self.generate(client, on_draft=saved.append)
        self.assertEqual(client.chat.completions.create.call_count, 7, result["metadata"])
        self.assertTrue(any(p.get("claim_audit") for p in self.payloads if "writer_draft" in p))
        self.assertTrue(any(p.get("research_packet") for p in self.payloads if "argument_plan" in p))
        self.assertEqual(saved[0]["metadata"]["workflow_stage"], "research_prepared")
        self.assertTrue(any(p["metadata"].get("structured_draft") for p in saved))
        self.assertEqual(result["metadata"]["answer_status"], "limited")  # narrowed original question
        self.assertEqual(result["metadata"]["review_status"], "needs_review")
        self.assertEqual(result["metadata"]["research_execution"]["api_call_count"], 7)
        self.assertIn("研究設計與證據紀錄", result["body"])
        self.assertNotIn("synthetic-key", json.dumps(result))

    def test_final_audit_uses_revised_title_and_body(self):
        result = self.generate(self.client(revise=True))
        audits = [p for p in self.payloads if "paragraphs" in p]
        self.assertEqual(len(audits), 2)
        self.assertEqual(audits[-1]["paragraphs"][0]["text"], "Revised operating outlook")
        self.assertNotEqual(audits[0]["paragraphs"][0]["text"], audits[-1]["paragraphs"][0]["text"])
        self.assertEqual(result["metadata"]["claim_audit"]["draft_hash"], result["metadata"]["research_packet"]["claim_audit"]["draft_hash"])

    def test_audit_failure_never_promotes_report(self):
        result = self.generate(self.client(fail_at=7))
        self.assertFalse(result["metadata"]["claim_audit"]["complete"])
        self.assertEqual(result["metadata"]["review_status"], "needs_review")
        self.assertTrue(result["metadata"]["report_has_body"])
        self.assertNotIn("synthetic-secret-provider-failure", json.dumps(result))

    def test_reviewer_failure_never_reclassifies_saved_body_as_preparation(self):
        saved = []
        result = self.generate(self.client(fail_at=6), on_draft=saved.append)
        self.assertEqual(result["metadata"]["review_status"], "review_failed")
        self.assertTrue(result["metadata"]["report_has_body"])
        self.assertTrue(saved[-1]["metadata"]["report_has_body"])
        self.assertTrue(saved[-1]["metadata"]["structured_draft"])
        self.assertNotIn("正文尚未生成", result["body"])

    def test_call_budget_stops_before_additional_paid_audit(self):
        client = self.client()
        result = self.generate(client, budget={"max_calls": 6})
        self.assertEqual(client.chat.completions.create.call_count, 6)
        self.assertEqual(result["metadata"]["research_execution"]["api_call_count"], 6)
        self.assertFalse(result["metadata"]["claim_audit"]["complete"])
        self.assertEqual(result["metadata"]["review_status"], "needs_review")

    def test_evidence_api_failure_preserves_design_and_continues_to_prose(self):
        saved = []
        result = self.generate(self.client(fail_at=2), on_draft=saved.append)
        self.assertFalse(saved[0]["metadata"]["report_has_body"])
        self.assertTrue(saved[-1]["metadata"]["report_has_body"])
        self.assertTrue(result["metadata"]["structured_draft"])
        self.assertEqual(result["metadata"]["review_status"], "needs_review")
        self.assertEqual(result["metadata"]["failure_stage"], "organizing")
        self.assertEqual(result["metadata"]["failure_reason"], "provider_or_processing_error")
        self.assertEqual(result["metadata"]["answer_status"], "unanswered")
        self.assertNotIn("正文尚未生成", result["body"])
        self.assertEqual(result["metadata"]["research_packet"]["design"]["questions"], design()["questions"])
        self.assertNotIn("synthetic-secret-provider-failure", json.dumps(result))

    def test_design_api_failure_uses_sources_without_claiming_completed_research(self):
        result = self.generate(self.client(fail_at=1))
        self.assertTrue(result["metadata"]["report_has_body"])
        self.assertEqual(result["metadata"]["failure_stage"], "designing")
        self.assertEqual(result["metadata"]["answer_status"], "unanswered")
        self.assertEqual(result["metadata"]["review_status"], "needs_review")
        self.assertNotIn("正文尚未生成", result["body"])

    def test_repeated_provider_failure_keeps_memo_separate_from_a_draft(self):
        client = self.client()
        original = client.chat.completions.create.side_effect
        def fail_after_design(**kwargs):
            if len(self.payloads):
                raise RuntimeError("synthetic-secret-token")
            return original(**kwargs)
        client.chat.completions.create.side_effect = fail_after_design
        result = self.generate(client)
        self.assertFalse(result["metadata"]["report_has_body"])
        self.assertEqual(result["metadata"]["workflow_stage"], "preparation_failed")
        self.assertEqual(result["metadata"]["review_status"], "preparation_failed")
        self.assertEqual(result["metadata"]["failure_stage"], "planning")
        self.assertNotIn("synthetic-secret-token", json.dumps(result))
        self.assertEqual(client.chat.completions.create.call_count, 3)

    def test_preparation_call_budget_reserves_plan_writer_and_reviewer(self):
        packet = prepare_research_packet(TOPIC, sources(), as_of=AS_OF,
            request=lambda _s, _p, stage: design() if stage == "designing" else evidence(),
            retrieve_sources=False)
        def busy_preparation(*_args, request, on_checkpoint, **_kwargs):
            on_checkpoint(packet)
            for _ in range(10):
                request("Extraction", {"previous_items": []}, "organizing")
            return packet
        client = self.client()
        with patch("src.deep_research_workflow.prepare_research_packet", side_effect=busy_preparation):
            result = self.generate(client, budget={"max_calls": 6})
        stages = [row["stage"] for row in result["metadata"]["research_execution"]["stages"]]
        self.assertEqual(stages, ["organizing"] * 3 + ["planning", "writing", "reviewing"])
        self.assertTrue(result["metadata"]["report_has_body"])
        self.assertEqual(result["metadata"]["failure_reason"], "writing_budget_reserved")
        self.assertEqual(client.chat.completions.create.call_count, 6)

    def test_preparation_timeout_leaves_time_for_body_and_review(self):
        from src.research import ResearchError
        now = [0.0]
        client = self.client()
        original = client.chat.completions.create.side_effect
        def slow_extraction(**kwargs):
            value = original(**kwargs)
            if "previous_items" in self.payloads[-1]:
                # With a 120-second overall budget, preparation stops at 48s.
                now[0] = 49.0
                raise ResearchError("generation_timeout")
            return value
        client.chat.completions.create.side_effect = slow_extraction
        with patch("src.deep_research_workflow.time.monotonic", side_effect=lambda: now[0]):
            result = self.generate(client, budget={"timeout_seconds": 120})
        self.assertTrue(result["metadata"]["report_has_body"])
        self.assertEqual(result["metadata"]["failure_reason"], "generation_timeout")
        self.assertTrue(any("writer_draft" in payload for payload in self.payloads))
        self.assertLess(result["metadata"]["research_execution"]["duration_seconds"], 120)

    def test_cancelled_preparation_does_not_start_another_request(self):
        callbacks = []
        client = self.client()
        original = client.chat.completions.create.side_effect
        def cancel_during_extraction(**kwargs):
            value = original(**kwargs)
            if "previous_items" in self.payloads[-1]:
                callbacks[0]()
                raise RuntimeError("closed transport")
            return value
        client.chat.completions.create.side_effect = cancel_during_extraction
        result = self.generate(client, register_cancel=callbacks.append)
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertFalse(result["metadata"]["report_has_body"])
        self.assertEqual(result["metadata"]["workflow_stage"], "preparation_failed")

    def test_thirty_source_failure_shape_still_generates_evidence_bounded_body(self):
        # The real regression saved a design for 30 sources, then failed on the
        # first organizing request with no rounds or evidence saved.
        rows = [copy.deepcopy(sources()[index % 2]) for index in range(30)]
        for index, row in enumerate(rows, 1):
            row["id"] = f"S{index}"
        client = self.client(fail_at=2)
        result = generate_research_workflow(TOPIC, rows, api_key="synthetic-key", as_of=AS_OF,
            deep_research=True, writing_style="argumentative", report_mode="argumentative",
            retrieve_sources=False, client=client)
        self.assertEqual(len(result["sources"]), 30)
        self.assertEqual(result["metadata"]["research_packet"]["evidence"]["items"], [])
        self.assertTrue(result["metadata"]["structured_draft"])
        self.assertEqual(result["metadata"]["answer_status"], "unanswered")
        self.assertNotIn("正文尚未生成", result["body"])

    def test_durable_save_failure_stops_before_next_model_request(self):
        client = self.client()
        def fail(_): raise OSError("synthetic disk failure")
        with self.assertRaisesRegex(OSError, "synthetic disk failure"):
            self.generate(client, on_draft=fail)
        self.assertEqual(client.chat.completions.create.call_count, 1)


class PreparationTests(unittest.TestCase):
    def numeric_packet(self, *, primary=False):
        from test_research_design import numeric
        rows = [numeric(metric="OCF", value="120", quote="Alpha consolidated 2026Q2 OCF was 120 USD million."),
                numeric(id="E2", metric="CapEx", value="80", quote="Alpha consolidated 2026Q2 CapEx was 80 USD million.")]
        source_rows = [sources()[0]]
        source_rows[0]["content"] = "\n".join(row["quote"] for row in rows)
        if primary:
            source_rows[0].update(url="https://www.sec.gov/Archives/example.htm", content_scope="html_excerpt")
        research_design = design()
        research_design["requirements"][0]["kind"] = "numeric"
        research_design["methods"] = ["cash_flow"]
        responses = iter([research_design, {"items": rows}, {"requests": [{"id": "C1", "kind": "cashflow_coverage",
            "operand_ids": {"ocf": "E1", "capex": "E2"}}], "limitations": []}])
        return prepare_research_packet(TOPIC, source_rows, as_of=AS_OF,
            request=lambda *args: next(responses), retrieve_sources=False)

    def test_secondary_numeric_anchor_remains_an_original_document_gap(self):
        packet = self.numeric_packet()
        self.assertEqual(packet["answer_status"], "limited")
        self.assertEqual(packet["evidence"]["requirements"][0]["source_basis"], "original_document_missing")
        self.assertTrue(packet["evidence"]["items"][0]["eligible"])  # usable as an attributed observation
        self.assertEqual(packet["calculations"][0]["result"], {"coverage_ratio": "1.5", "fcf": "40"})

    def test_primary_excerpt_allows_traceable_calculation_without_claiming_verification(self):
        packet = self.numeric_packet(primary=True)
        self.assertEqual(packet["answer_status"], "full")
        self.assertEqual(packet["calculations"][0]["inputs"]["ocf"]["value"], "120")
        self.assertFalse(packet["independently_verified"])
        self.assertEqual(packet["evidence"]["items"][0]["source_basis"], "primary_document_candidate")

    def test_design_fallback_cannot_claim_original_question_answered(self):
        response = iter(["invalid json", evidence()])
        packet = prepare_research_packet(TOPIC, sources(), as_of=AS_OF,
            request=lambda *args: next(response), retrieve_sources=False)
        self.assertEqual(packet["evidence"]["status"], "full")
        self.assertEqual(packet["answer_status"], "limited")
        self.assertIn("研究設計格式不符", render_packet_appendix(packet))

    def test_future_publication_is_removed_before_model_sees_it(self):
        source_rows = sources()
        # A stale date field must not hide a newer known timestamp.
        source_rows[1].update(publication_date="2026-10-10", published_at="2026-10-15")
        payloads = []
        def request(system, payload, stage):
            payloads.append(payload)
            return design() if stage == "designing" else {"items": [evidence()["items"][0]]}
        packet = prepare_research_packet(TOPIC, source_rows, as_of=AS_OF, request=request, retrieve_sources=False)
        self.assertEqual([s["id"] for s in packet["sources"]], ["S1"])
        self.assertEqual(len(packet["input_sources"]), 2)
        self.assertEqual(packet["excluded_sources"][0]["reason"], "published_after_cutoff")
        self.assertEqual(len(payloads[0]["available_sources"]), 1)

    def test_new_source_is_checkpointed_before_next_extraction_fails(self):
        added = copy.deepcopy(sources()[1]); added["id"] = "S3"
        added["retrieval"] = {"document_sha256": "a" * 64, "selection_spans": [[0, 10]]}
        snapshots = []
        def request(system, payload, stage):
            if stage == "designing": return design()
            if any(row["id"] == "S3" for row in payload["sources"]): raise RuntimeError("extraction unavailable")
            return {"items": []}
        with patch("src.research_discovery.supplement_sources", return_value={
                "sources": [*sources(), added], "outcomes": [], "attempted_urls": [], "added_count": 1}):
            with self.assertRaises(RuntimeError):
                prepare_research_packet(TOPIC, sources(), as_of=AS_OF, request=request, on_checkpoint=snapshots.append)
        self.assertEqual(len(snapshots[-1]["sources"]), 3)
        self.assertEqual(snapshots[-1]["source_captures"]["S3"]["document_sha256"], "a" * 64)
        self.assertEqual(len(snapshots[0]["sources"]), 2)  # immutable callback value

    def test_missing_evidence_stops_when_no_new_sources(self):
        with patch("src.research_discovery.supplement_sources", return_value={
                "sources": sources(), "outcomes": [], "attempted_urls": [], "added_count": 0}) as supplement:
            packet = prepare_research_packet(TOPIC, sources(), as_of=AS_OF,
                request=lambda system, payload, stage: design() if stage == "designing" else {"items": []})
        self.assertEqual(supplement.call_count, 1)
        self.assertEqual(packet["answer_status"], "unanswered")
        self.assertEqual(packet["stop_reason"], "no_new_sources")

    def test_rendered_calculations_keep_each_result_unit_and_input(self):
        responses = iter([design(), evidence()])
        packet = prepare_research_packet(TOPIC, sources(), as_of=AS_OF,
            request=lambda *args: next(responses), retrieve_sources=False)
        packet["calculations"] = [{"id": "C1", "kind": "cashflow_coverage", "status": "computed",
            "formula": "coverage_ratio = ocf / capex; fcf = ocf - capex", "unit": "USD million",
            "result": {"coverage_ratio": "1.5", "fcf": "40"},
            "result_units": {"coverage_ratio": "multiple", "fcf": "USD million"},
            "inputs": {"ocf": {"value": "120"}, "capex": {"value": "80"}},
            "source_ids": ["S1", "S2"], "period": "2026Q2", "entity": "A", "scope": "consolidated"}]
        body = render_packet_appendix(packet)
        self.assertIn("1.5 multiple", body)
        self.assertIn("40 USD million", body)
        self.assertNotIn("1.5 USD million", body)
        self.assertIn("120", body)
        self.assertIn("S1、S2", body)


if __name__ == "__main__":
    unittest.main()
