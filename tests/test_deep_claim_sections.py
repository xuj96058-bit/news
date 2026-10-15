"""Report headings are claims too; structural labels need no factual support."""
import copy
import json
import unittest

from src.analysis_errors import ModelResponseError
from src.argumentative_workflow import _review
from src.deep_research_workflow import audit_paragraphs
from src.research_audit import parse_claim_audit


def paragraph(text="A OCF was 100 USD billion.", ids=("S1",)):
    return {"text": text, "source_ids": list(ids)}


def essay(title="研究報告", heading="Evidence"):
    return {"title": title, "thesis": [paragraph()], "arguments": [{"heading": heading,
        "claim": paragraph(), "evidence": [paragraph(ids=("S2",))], "reasoning": paragraph(ids=("S1", "S2")),
        "counterargument": paragraph(ids=("S3",)), "condition": paragraph(ids=("S4",))}],
        "conclusion": [paragraph()], "watchlist": [paragraph(ids=("S5",))],
        "limitations": [paragraph(ids=("S6",))]}


def fact(identifier, metric, value):
    return {"id": identifier, "kind": "numeric", "source_id": "S1",
            "quote": "A OCF was 100 USD billion; revenue was 200 USD billion.",
            "entity": "A", "metric": metric, "value": value, "unit": "USD billion", "period": "2025",
            "scope": "consolidated", "statement_type": "actual", "eligible": True,
            "traceability": "anchored", "anchor": {"start": 0, "end": 59}, "conflict": False}


def claimed_supported(paragraphs):
    return {"claims": [{"section": row["section"], "paragraph": row["paragraph"], "status": "supported",
                         "evidence_ids": ["E1", "E2"], "calculation_ids": [],
                         "reason": "The model claims this is supported."} for row in paragraphs],
            "summary": "Snapshot audit."}


class DeepClaimSectionTests(unittest.TestCase):
    def test_title_and_heading_use_their_corresponding_source_union(self):
        draft = essay("A OCF was 200 USD billion", "A cash flow strengthens")
        original = copy.deepcopy(draft)
        rows = audit_paragraphs(draft, "argumentative")
        indexed = {row["section"]: row for row in rows}
        self.assertEqual(indexed["title"]["source_ids"], ["S1", "S2", "S3", "S4", "S5", "S6"])
        self.assertEqual(indexed["arguments.1.heading"]["source_ids"], ["S1", "S2", "S3", "S4"])
        self.assertIn("watchlist", indexed)
        self.assertIn("arguments.1.condition", indexed)
        self.assertIn("limitations", indexed)
        self.assertEqual(draft, original)

    def test_pure_structural_headings_and_section_numbers_are_exempt(self):
        for title, heading in (("研究報告", "Evidence"), ("研究草稿", "1. Evidence"),
                               ("Research report", "一、結論"), ("資料缺口簡報", "2. Methodology")):
            with self.subTest(title=title, heading=heading):
                rows = audit_paragraphs(essay(title, heading), "argumentative")
                self.assertFalse(any(row["section"] == "title" or row["section"].endswith(".heading") for row in rows))
                self.assertTrue(rows)

    def test_factual_premise_in_future_watchlist_or_condition_is_not_exempt(self):
        from src.deep_research_workflow import _audit_findings
        draft = essay()
        draft["arguments"][0]["condition"] = paragraph("Given A OCF was 200 USD billion, if margin improves, revisit the outlook.")
        draft["watchlist"] = [paragraph("A OCF was 200 USD billion; monitor future capex.")]
        rows = audit_paragraphs(draft, "argumentative")
        audit = parse_claim_audit(claimed_supported(rows), rows, [fact("E1", "OCF", "100"), fact("E2", "revenue", "200")], [])
        failures = {row["section"]: row for row in audit["claims"]}
        for section in ("arguments.1.condition", "watchlist"):
            self.assertEqual(failures[section]["status"], "unknown")
        self.assertTrue(any("數值角色未匹配" in row["message"] for row in _audit_findings(audit)))

    def test_non_numeric_substantive_headings_are_still_audited(self):
        rows = audit_paragraphs(essay("公司現金流已覆蓋投資", "政策已正式生效"), "argumentative")
        self.assertTrue(any(row["section"] == "title" for row in rows))
        self.assertTrue(any(row["section"] == "arguments.1.heading" for row in rows))

    def test_same_number_in_revenue_cannot_support_cash_flow_title(self):
        rows = audit_paragraphs(essay("A OCF was 200 USD billion", "A OCF was 200 USD billion"), "argumentative")
        evidence = [fact("E1", "OCF", "100"), fact("E2", "revenue", "200")]
        audited = parse_claim_audit(claimed_supported(rows), rows, evidence, [])
        failures = {row["section"]: row for row in audited["claims"]}
        for section in ("title", "arguments.1.heading"):
            self.assertEqual(failures[section]["model_status"], "supported")
            self.assertEqual(failures[section]["status"], "unknown")
            self.assertTrue(failures[section]["program_reasons"])
        self.assertGreaterEqual(audited["unresolved_count"], 2)

    def test_revised_headings_are_reextracted_and_checked(self):
        initial = essay("A OCF was 100 USD billion", "A OCF was 100 USD billion")
        revised = copy.deepcopy(initial)
        revised["title"] = "A OCF was 200 USD billion"
        revised["arguments"][0]["heading"] = "A OCF was 200 USD billion"
        initial_rows = audit_paragraphs(initial, "argumentative")
        revised_rows = audit_paragraphs(revised, "argumentative")
        evidence = [fact("E1", "OCF", "100"), fact("E2", "revenue", "200")]
        result = parse_claim_audit(claimed_supported(revised_rows), revised_rows, evidence, [])
        self.assertEqual(initial_rows[0]["text"], "A OCF was 100 USD billion")
        self.assertEqual(revised_rows[0]["text"], "A OCF was 200 USD billion")
        self.assertEqual(result["claims"][0]["status"], "unknown")

    def test_legacy_report_titles_receive_the_same_numeric_check(self):
        draft = {"title": "A OCF was 200 USD billion", "executive_findings": [paragraph()],
                 "chart_data_interpretation": [paragraph(ids=("S2",))]}
        rows = audit_paragraphs(draft, "legacy")
        title = rows[0]
        self.assertEqual(title["section"], "title")
        self.assertEqual(set(title["source_ids"]), {"S1", "S2"})
        result = parse_claim_audit(claimed_supported([title]), [title],
                                  [fact("E1", "OCF", "100"), fact("E2", "revenue", "200")], [])
        self.assertEqual(result["claims"][0]["status"], "unknown")

    def test_reviewer_accepts_actual_title_and_heading_locations_only(self):
        sources = [{"id": f"S{number}", "content": "A OCF was 100 USD billion."} for number in range(1, 7)]
        payload = {"draft": essay(), "findings": [], "summary": "Snapshot review.",
                   "quality": {"relevance": 3, "evidence": 3, "reasoning": 3, "readability": 3, "verdict": "pass"}}
        for section, number, valid in (("title", 1, True), ("arguments.1.heading", 1, True),
                                       ("title", 2, False), ("arguments.0.heading", 1, False),
                                       ("arguments.2.heading", 1, False), ("arguments.1.heading", 2, False)):
            payload["findings"] = [{"section": section, "paragraph": number, "severity": "warning",
                                    "message": "The heading requires evidence.", "source_ids": ["S1"]}]
            with self.subTest(section=section, paragraph=number):
                if valid:
                    findings = _review(json.dumps(payload), sources, None, None)[2]
                    self.assertTrue(any(item["section"] == section for item in findings))
                else:
                    with self.assertRaises(ModelResponseError):
                        _review(json.dumps(payload), sources, None, None)


if __name__ == "__main__":
    unittest.main()
