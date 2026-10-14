"""Synthetic argument structures; no paid provider or credentials are used."""

import copy
import json
import unittest

from src.analysis_errors import ModelResponseError
from src.argumentation import (
    essay_findings, essay_paragraphs, parse_essay, parse_plan, render_essay,
)


SOURCES = [{"id": "S1", "content": "Orders improved in the disclosed business."},
           {"id": "S2", "content": "Costs constrained margins in the disclosed business."},
           {"id": "S3", "content": "An unrelated travel promotion."}]


def paragraph(text, ids=("S1",)):
    return {"text": text, "source_ids": list(ids)}


def plan():
    return {"research_question": "Can improved orders support sustainable profits?",
            "thesis": "Orders improve the starting point but cost pressure remains a constraint.",
            "answer_scope": "narrowed", "source_ids": ["S1", "S2"],
            "arguments": [{"heading": "Orders need to become deliveries and profit",
                           "claim": "Demand recovery alone does not establish profit recovery.",
                           "source_ids": ["S1"], "counter_source_ids": ["S2"],
                           "reasoning": "Delivery and cost determine whether orders become profit.",
                           "verification": "If deliveries improve but margin fails to improve, costs remain a constraint."}],
            "missing_evidence": ["Comparable deliveries and margins for the same business."]}


def essay():
    return {"title": "When will order recovery become profit recovery?",
            "thesis": [paragraph("Demand has improved, while its conversion into profit remains conditional.", ("S1", "S2"))],
            "arguments": [{"heading": "Conversion requires delivery and margin",
                           "claim": paragraph("Order improvement alone does not establish profit recovery.", ("S1", "S2")),
                           "evidence": [paragraph("Orders improved in the disclosed business."),
                                        paragraph("Costs constrained margins in the disclosed business.", ("S2",))],
                           "reasoning": paragraph("Orders enter profit only through delivery with margin; costs can interrupt that conversion.", ("S1", "S2")),
                           "counterargument": paragraph("An alternative hypothesis is that delivery efficiency offsets costs; this remains unverified.", ("S2",)),
                           "condition": paragraph("If subsequent disclosures show delivery growth without margin improvement, the cost explanation is supported.", ("S1", "S2"))}],
            "conclusion": [paragraph("Profit durability remains conditional on delivery and cost evidence.", ("S1", "S2"))],
            "watchlist": [paragraph("Compare delivery and margin in the next disclosure; improvement in both supports profit conversion.", ("S1", "S2"))],
            "limitations": [paragraph("Comparable delivery and margin disclosures are not available in these snapshots.", ("S1", "S2"))]}


class ArgumentationTests(unittest.TestCase):
    def test_plan_excludes_unrelated_sources_without_mutating_inputs(self):
        raw = plan()
        before = copy.deepcopy(raw)
        selected = parse_plan(json.dumps(raw), SOURCES, "Orders and profits")
        self.assertEqual(selected["source_ids"], ["S1", "S2"])
        self.assertNotIn("S3", selected["source_ids"])
        self.assertEqual(raw, before)

    def test_unknown_plan_ids_never_authorize_or_fall_back_to_all_sources(self):
        for field in ("source_ids", "counter_source_ids"):
            raw = plan()
            raw["arguments"][0][field].append("S999")
            with self.subTest(field=field), self.assertRaises(ModelResponseError):
                parse_plan(raw, SOURCES, "Orders and profits")
        raw = plan()
        raw["source_ids"].append("S999")
        with self.assertRaises(ModelResponseError):
            parse_plan(raw, SOURCES, "Orders and profits")

    def test_real_but_unselected_argument_source_is_rejected(self):
        raw = plan()
        raw["arguments"][0]["source_ids"] = ["S3"]
        with self.assertRaises(ModelResponseError):
            parse_plan(raw, SOURCES, "Orders and profits")

    def test_insufficient_plan_does_not_fabricate_arguments(self):
        raw = plan()
        raw.update(answer_scope="insufficient", source_ids=[], arguments=[])
        parsed = parse_plan(raw, SOURCES, "Orders and profits")
        self.assertEqual(parsed["arguments"], [])
        self.assertEqual(parsed["source_ids"], [])
        raw["answer_scope"] = "full"
        with self.assertRaises(ModelResponseError):
            parse_plan(raw, SOURCES, "Orders and profits")

    def test_dynamic_argument_bounds_and_missing_scope_explanation(self):
        raw = plan()
        raw["arguments"] *= 6
        with self.assertRaises(ModelResponseError):
            parse_plan(raw, SOURCES, "Orders and profits")

    def test_wrong_scope_type_and_empty_cleaned_gap_are_schema_errors(self):
        raw = plan()
        raw["answer_scope"] = ["full"]
        with self.assertRaises(ModelResponseError):
            parse_plan(raw, SOURCES, "Orders and profits")
        raw = plan()
        raw["missing_evidence"] = ["[S1]"]
        with self.assertRaises(ModelResponseError):
            parse_plan(raw, SOURCES, "Orders and profits")
        raw = plan()
        raw["missing_evidence"] = []
        with self.assertRaises(ModelResponseError):
            parse_plan(raw, SOURCES, "Orders and profits")

    def test_unknown_and_inline_citations_cannot_enter_markdown(self):
        raw = essay()
        raw["arguments"][0]["evidence"][0] = paragraph(
            "Orders improved [S999] [Read](https://bad.example/path) <a href='https://bad.example'>link</a>",
            ("S1", "S999", "S1"))
        parsed, warnings = parse_essay(raw, SOURCES)
        self.assertEqual(parsed["arguments"][0]["evidence"][0]["source_ids"], ["S1"])
        markdown = "\n".join(render_essay(parsed))
        self.assertNotIn("S999", markdown)
        self.assertNotIn("https://bad.example", markdown)
        self.assertNotIn("<a", markdown)
        self.assertIn("[S1]", markdown)
        self.assertIn("unknown_source_ids", {item["code"] for item in warnings})
        self.assertIn("inline_citations_ignored", {item["code"] for item in warnings})

    def test_paragraph_locations_cover_claims_and_optional_argument_parts(self):
        parsed, _ = parse_essay(essay(), SOURCES)
        locations = {(section, index) for section, index, _ in essay_paragraphs(parsed)}
        self.assertIn(("arguments.1.evidence", 2), locations)
        self.assertIn(("arguments.1.counterargument", 1), locations)
        self.assertIn(("arguments.1.condition", 1), locations)
        self.assertIn(("watchlist", 1), locations)
        self.assertEqual(len(locations), 10)

    def test_short_but_complete_argument_has_no_length_quality_penalty(self):
        parsed, warnings = parse_essay(essay(), SOURCES)
        self.assertEqual(warnings, [])
        self.assertEqual(essay_findings(parsed, plan()), [])

    def test_duplicate_material_is_not_reasoning_and_missing_parts_are_flagged(self):
        raw = essay()
        argument = raw["arguments"][0]
        argument["reasoning"] = copy.deepcopy(argument["evidence"][0])
        argument["counterargument"] = None
        argument["condition"] = None
        raw["watchlist"] = [paragraph("Keep an eye on earnings.")]
        parsed, _ = parse_essay(raw, SOURCES)
        findings = essay_findings(parsed, plan())
        errors = [finding for finding in findings if finding["severity"] == "error"]
        self.assertTrue(any(finding["section"] == "arguments.1.reasoning" for finding in errors))
        self.assertTrue(any("反方" in finding["message"] for finding in findings))
        self.assertTrue(any(finding["section"] == "watchlist" for finding in findings))

    def test_unrelated_real_source_in_essay_is_flagged_against_plan(self):
        raw = essay()
        raw["thesis"][0]["source_ids"] = ["S3"]
        parsed, _ = parse_essay(raw, SOURCES)
        findings = essay_findings(parsed, plan())
        self.assertTrue(any(finding["severity"] == "error" and finding["section"] == "thesis"
                            and finding["source_ids"] == ["S3"] for finding in findings))

    def test_citations_in_later_draft_cannot_hide_omitted_counterargument(self):
        raw = essay()
        raw["arguments"][0]["counterargument"]["source_ids"] = []
        parsed, _ = parse_essay(raw, SOURCES)
        findings = essay_findings(parsed, plan())
        self.assertTrue(any("反方來源" in item["message"] and item["source_ids"] == ["S2"] for item in findings))

    def test_each_evidence_paragraph_requires_its_own_source(self):
        raw = essay()
        raw["arguments"][0]["evidence"][1]["source_ids"] = []
        parsed, _ = parse_essay(raw, SOURCES)
        findings = essay_findings(parsed, plan())
        self.assertTrue(any(item["section"] == "arguments.1.evidence" and item["paragraph"] == 2
                            and item["severity"] == "error" for item in findings))

    def test_verification_recognizes_common_economic_metrics(self):
        raw = essay()
        raw["watchlist"] = [paragraph("下一次 CPI 若仍持續轉弱，支持通脹降溫的判斷。")]
        raw["arguments"][0]["condition"] = paragraph("如果 GDP 繼續轉弱，需求壓力的判斷獲得支持。")
        parsed, _ = parse_essay(raw, SOURCES)
        findings = essay_findings(parsed, plan())
        self.assertFalse(any(item["section"] in {"watchlist", "arguments.1.condition"} for item in findings))

    def test_no_arguments_retains_useful_gap_draft_but_not_full_report_approval(self):
        raw = essay()
        raw["arguments"] = []
        parsed, _ = parse_essay(raw, SOURCES)
        self.assertTrue(any(item["severity"] == "error" for item in essay_findings(parsed, plan())))
        insufficient = plan()
        insufficient["answer_scope"] = "insufficient"
        self.assertFalse(any(item["severity"] == "error" for item in essay_findings(parsed, insufficient)))

    def test_rendering_does_not_make_model_markup_or_heading_active(self):
        raw = essay()
        raw["arguments"][0]["heading"] = "### Claimed [proof]"
        raw["thesis"][0]["text"] = "*Claim* [arbitrary reference] remains conditional."
        parsed, _ = parse_essay(raw, SOURCES)
        markdown = "\n".join(render_essay(parsed))
        self.assertIn(r"## \#\#\# Claimed \[proof\]", markdown)
        self.assertIn(r"\*Claim\* \[arbitrary reference\]", markdown)
        self.assertIn("[S1][S2]", markdown)


if __name__ == "__main__":
    unittest.main()
