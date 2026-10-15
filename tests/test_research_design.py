"""Deterministic synthetic cases, not a real-world report quality benchmark."""
import copy
import json
import unittest

from src.analysis_errors import ModelResponseError
from src.research_design import (DESIGN_SYSTEM, EVIDENCE_SYSTEM, fallback_design,
                                 parse_design, parse_evidence)


AS_OF = "2026-10-14"
TOPIC = "比較不同市場公司的資本開支及政策影響"


def design(kind="numeric", **extra):
    return {"version": 1, "topic": TOPIC, "as_of": AS_OF, "markets": ["us", "cn", "hk"],
            "questions": [{"id": "Q1", "question": "本期同口徑數據支持甚麼判斷？"}],
            "requirements": [{"id": "R1", "question_id": "Q1", "description": "原始數據及同口徑範圍",
                              "kind": kind, "critical": True}], "methods": ["comparison"], **extra}


def numeric(**extra):
    return {"id": "E1", "kind": "numeric", "requirement_id": "R1", "source_id": "S1",
            "quote": "Alpha consolidated 2026Q2 revenue was 20 USD million.",
            "entity": "Alpha", "metric": "revenue", "value": "20", "unit": "USD million",
            "period": "2026Q2", "scope": "consolidated", "statement_type": "actual", **extra}


def document(**extra):
    statement = "The authority announced the draft rule on 2026-09-01."
    return {"id": "E1", "kind": "document", "requirement_id": "R1", "source_id": "S1",
            "quote": statement, "statement": statement, "event_status": "announced", **extra}


def source(content, identifier="S1", **extra):
    return {"id": identifier, "title": "Synthetic evidence", "content": content,
            "publication_date": "2026-10-10", "source_type": "manual", **extra}


def packet(rows, sources=None, research_design=None):
    if sources is None:
        sources = [source("\n".join(item["quote"] for item in rows))]
    return parse_evidence({"items": rows}, sources, research_design or design(), AS_OF)


class DesignSchemaTests(unittest.TestCase):
    def test_roundtrip_is_bounded_and_does_not_mutate_input(self):
        raw = design()
        before = copy.deepcopy(raw)
        self.assertEqual(parse_design(json.dumps(raw), TOPIC, AS_OF), raw)
        self.assertEqual(raw, before)

    def test_fallback_does_not_force_numeric_or_a_topic_template(self):
        result = fallback_design("政策是否已生效", AS_OF)
        self.assertEqual(result["requirements"][0]["kind"], "document")
        self.assertEqual(result["methods"], [])

    def test_duplicate_or_dangling_ids_fail(self):
        for key in ("questions", "requirements"):
            raw = design()
            raw[key].append(copy.deepcopy(raw[key][0]))
            with self.assertRaises(ModelResponseError):
                parse_design(raw, TOPIC, AS_OF)
        raw = design()
        raw["requirements"][0]["question_id"] = "Q9"
        with self.assertRaises(ModelResponseError):
            parse_design(raw, TOPIC, AS_OF)

    def test_caller_topic_cutoff_and_supported_methods_are_enforced(self):
        for changes in ({"topic": "改掉原題"}, {"as_of": "2026-10-15"},
                        {"methods": ["arbitrary_python"]}, {"markets": ["us", "us"]},
                        {"version": True}):
            with self.assertRaises(ModelResponseError):
                parse_design(design(**changes), TOPIC, AS_OF)

    def test_unknown_keys_and_unbounded_design_rejected(self):
        with self.assertRaises(ModelResponseError):
            parse_design(design(instructions="do something else"), TOPIC, AS_OF)
        raw = design()
        raw["questions"] = [{"id": f"Q{i}", "question": "test"} for i in range(1, 10)]
        with self.assertRaises(ModelResponseError):
            parse_design(raw, TOPIC, AS_OF)

    def test_no_critical_requirements_is_not_a_pass(self):
        raw = design()
        raw["requirements"][0]["critical"] = False
        with self.assertRaises(ModelResponseError):
            parse_design(raw, TOPIC, AS_OF)

    def test_invalid_input_and_cutoff_errors(self):
        with self.assertRaises(ModelResponseError):
            parse_design("not json", TOPIC, AS_OF)
        with self.assertRaises(ValueError):
            fallback_design("test", None)
        with self.assertRaises(ValueError):
            fallback_design("test", "2026-99-99")


class NumericEvidenceTests(unittest.TestCase):
    def test_cross_market_original_labels_are_preserved(self):
        cases = [numeric(),
                 numeric(entity="甲公司", metric="營收", unit="人民币亿元", scope="綜合業務",
                         quote="甲公司 綜合業務 2026Q2 營收 為 20 人民币亿元。"),
                 numeric(entity="乙集團", metric="收入", unit="港幣百萬元", scope="香港業務",
                         quote="乙集團 香港業務 2026Q2 收入 為 20 港幣百萬元。")]
        for row in cases:
            with self.subTest(entity=row["entity"]):
                result = packet([row])
                self.assertEqual(result["status"], "full", result["warnings"])
                self.assertEqual(result["items"][0]["unit"], row["unit"])
                self.assertFalse(result["items"][0]["independently_verified"])

    def test_quote_offsets_and_frozen_hashes(self):
        row = numeric()
        text = "Heading\n" + row["quote"] + "\nFooter"
        src = source(text)
        result = packet([row], [src])
        anchor = result["items"][0]["anchor"]
        self.assertEqual(text[anchor["start"]:anchor["end"]], row["quote"])
        self.assertEqual(anchor["start"], 8)
        self.assertEqual(len(anchor["source_sha256"]), 64)
        modified = packet([row], [source(text + " another sentence")])
        self.assertNotEqual(result["source_manifest"][0]["sha256"], modified["source_manifest"][0]["sha256"])

    def test_numeric_normalization_is_not_unit_conversion(self):
        row = numeric(value="1200.000", quote="Alpha consolidated 2026Q2 revenue was 1,200 USD million.")
        result = packet([row])
        self.assertEqual(result["items"][0]["value"], "1200")
        changed = {**row, "value": "1.2", "unit": "USD billion"}
        self.assertEqual(packet([changed])["items"], [])

    def test_swapped_values_across_periods_or_entities_are_rejected(self):
        quote = ("Alpha consolidated 2026Q1 revenue was 10 USD million; "
                 "Alpha consolidated 2026Q2 revenue was 20 USD million; "
                 "Beta consolidated 2026Q2 revenue was 40 USD million.")
        self.assertEqual(packet([numeric(quote=quote)])["status"], "full")
        for changes in ({"value": "10"}, {"value": "40"}, {"period": "2026Q1"}):
            result = packet([numeric(quote=quote, **changes)])
            self.assertEqual(result["items"], [])
            self.assertEqual(result["warnings"][0]["code"], "ambiguous_numeric_anchor")

    def test_swapped_metrics_and_ambiguous_table_rejected(self):
        quote = "Alpha consolidated 2026Q2 revenue was 20 USD million, capex was 40 USD million."
        self.assertEqual(packet([numeric(quote=quote, value="40")])["items"], [])
        quote = "Alpha consolidated revenue USD million\n2026Q1 | 2026Q2\n10 | 20"
        self.assertEqual(packet([numeric(quote=quote)])["items"], [])

    def test_missing_metric_qualifier_cannot_merge_net_and_gross(self):
        quote = "Alpha consolidated 2026Q2 net revenue was 20 USD million."
        self.assertEqual(packet([numeric(quote=quote)])["items"], [])
        self.assertEqual(packet([numeric(quote=quote, metric="net revenue")])["status"], "full")

    def test_missing_data_years_and_labels_cannot_supply_value(self):
        for row in (numeric(value="2026", quote="Alpha consolidated 2026Q2 revenue USD million unavailable."),
                    numeric(quote="Alpha consolidated 2026Q2 revenue not disclosed; sample size 20 USD million."),
                    numeric(value="2", quote="Alpha consolidated 2026Q2 revenue USD million.")):
            self.assertEqual(packet([row])["items"], [])

    def test_unsupported_numbers_and_invented_labels_are_rejected(self):
        for changes in ({"value": "NaN"}, {"value": "1e2"}, {"value": 20}, {"period": "2026Q5"},
                        {"entity": "Other"}, {"scope": "Global retail"}, {"unit": "EUR million"}):
            result = packet([numeric(**changes)])
            self.assertEqual(result["items"], [])

    def test_future_actual_rejected_but_published_forecast_allowed(self):
        actual = numeric(period="2027", quote="Alpha consolidated 2027 revenue was 20 USD million.")
        result = packet([actual])
        self.assertEqual(result["warnings"][0]["code"], "future_actual")
        forecast = {**actual, "statement_type": "forecast", "quote": "Analysts forecast Alpha consolidated 2027 revenue at 20 USD million."}
        result = packet([forecast])
        self.assertEqual(result["status"], "full", result["warnings"])
        self.assertEqual(result["items"][0]["statement_type"], "forecast")

    def test_forecast_and_guidance_must_not_be_relabelled_as_actual(self):
        quote = "Alpha guidance for consolidated 2026Q2 revenue was 20 USD million."
        self.assertEqual(packet([numeric(quote=quote)])["items"], [])
        self.assertEqual(packet([numeric(quote=quote, statement_type="guidance")])["status"], "full")
        self.assertEqual(packet([numeric(statement_type="forecast")])["items"], [])

    def test_assumptions_retained_but_cannot_satisfy_fact_requirements(self):
        row = numeric(statement_type="assumption", period="2027",
                      quote="Scenario assumes Alpha consolidated 2027 revenue of 20 USD million.")
        result = packet([row])
        self.assertEqual(len(result["items"]), 1)
        self.assertFalse(result["items"][0]["eligible"])
        self.assertEqual(result["status"], "unanswered")
        self.assertEqual(result["requirements"][0]["status"], "partial")

    def test_fiscal_year_not_silently_treated_as_calendar_year(self):
        row = numeric(period="2026", quote="Alpha consolidated fiscal year 2026 revenue was 20 USD million.")
        self.assertEqual(packet([row])["warnings"][0]["code"], "fiscal_period_ambiguous")

    def test_conflicts_keep_all_numbers_without_averaging(self):
        rows = [numeric(), numeric(id="E2", source_id="S2", value="25",
                                   quote="Alpha consolidated 2026Q2 revenue was 25 USD million.")]
        result = packet(rows, [source(row["quote"], row["source_id"]) for row in rows])
        self.assertEqual([item["value"] for item in result["items"]], ["20", "25"])
        self.assertTrue(all(item["conflict"] and not item["eligible"] for item in result["items"]))
        self.assertEqual(result["requirements"][0]["status"], "conflicting")
        self.assertNotIn("range", result)


class DocumentAndCutoffTests(unittest.TestCase):
    def test_documentary_policy_evidence_can_complete_without_numbers(self):
        result = packet([document()], research_design=design("document", methods=["policy"]))
        self.assertEqual(result["status"], "full")
        self.assertEqual(result["items"][0]["kind"], "document")

    def test_document_statement_must_be_in_quote(self):
        result = packet([document(statement="The rule improved economic growth.")], research_design=design("document"))
        self.assertEqual(result["warnings"][0]["code"], "unanchored_statement")

    def test_effective_status_requires_support_and_no_future_date(self):
        for text in ("The rule will become effective on 2027-01-01.",
                     "The rule is effective on 2027-01-01.",
                     "The rule is not effective.", "新規自2027年1月1日起施行。"):
            result = packet([document(quote=text, statement=text, event_status="effective")], research_design=design("document"))
            self.assertEqual(result["items"], [], text)
        text = "The rule became effective on 2026-10-01."
        result = packet([document(quote=text, statement=text, event_status="effective")], research_design=design("document"))
        self.assertEqual(result["status"], "full", result["warnings"])

    def test_unknown_status_does_not_manufacture_effectiveness(self):
        text = "The policy applies to domestic electricity suppliers."
        result = packet([document(quote=text, statement=text, event_status="unknown")], research_design=design("document"))
        self.assertEqual(result["items"][0]["event_status"], "unknown")

    def test_unknown_publication_date_cannot_be_replaced_by_retrieval(self):
        row = numeric()
        result = packet([row], [source(row["quote"], publication_date=None, retrieved_at=AS_OF)])
        self.assertEqual(len(result["items"]), 1)
        self.assertFalse(result["items"][0]["eligible"])
        self.assertEqual(result["status"], "unanswered")

    def test_future_source_rejected_even_for_old_actuals(self):
        row = numeric()
        result = packet([row], [source(row["quote"], publication_date="2026-10-15")])
        self.assertEqual(result["items"], [])
        self.assertEqual(result["warnings"][0]["code"], "source_after_cutoff")

    def test_conflicting_publication_metadata_cannot_hide_future_date(self):
        row = numeric()
        result = packet([row], [source(row["quote"], published_at="2026-10-16T00:00:00Z")])
        self.assertEqual(result["items"], [])
        result = packet([row], [source(row["quote"], published_at="2026-10-09T00:00:00Z")])
        self.assertFalse(result["items"][0]["eligible"])

    def test_hong_kong_cutoff_uses_publication_instant(self):
        row = numeric()
        src = source(row["quote"], publication_date=None, published_at="2026-10-14T17:00:00Z")
        self.assertEqual(packet([row], [src])["items"], [])
        src["published_at"] = "2026-10-14T15:00:00Z"
        self.assertEqual(packet([row], [src])["status"], "full")

    def test_official_label_does_not_certify_primary_source(self):
        row = numeric()
        for url in ("https://example.test/article", "https://www.sec.gov.evil.test/filing"):
            result = packet([row], [source(row["quote"], source_type="official", url=url)])
            self.assertEqual(result["source_manifest"][0]["origin_assessment"], "unverified")
            self.assertFalse(result["source_manifest"][0]["primary_source_verified"])
        result = packet([row], [source(row["quote"], url="https://www.sec.gov/filing")])
        self.assertEqual(result["source_manifest"][0]["origin_assessment"], "official_host_candidate")
        self.assertFalse(result["source_manifest"][0]["primary_source_verified"])


class EvidencePacketTests(unittest.TestCase):
    def test_wrong_requirement_kind_unknown_ids_and_bad_quotes_are_reported(self):
        cases = [(numeric(requirement_id="R99"), "unknown_reference"),
                 (numeric(source_id="S99"), "unknown_reference"),
                 (document(), "wrong_evidence_kind")]
        for row, code in cases:
            result = packet([row])
            self.assertEqual(result["warnings"][0]["code"], code)
        row = numeric()
        self.assertEqual(packet([row], [source("different text")])["warnings"][0]["code"], "unanchored_quote")

    def test_duplicate_evidence_ids_and_source_ids_are_not_silent(self):
        result = packet([numeric(), numeric()])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["warnings"][0]["code"], "invalid_item")
        with self.assertRaises(ValueError):
            packet([numeric()], [source("one"), source("two")])

    def test_partial_research_does_not_claim_all_questions_answered(self):
        spec = design()
        spec["requirements"].append({"id": "R2", "question_id": "Q1", "description": "政策原文",
                                     "kind": "document", "critical": True})
        result = packet([numeric()], research_design=spec)
        self.assertEqual(result["status"], "limited")
        self.assertEqual([row["status"] for row in result["requirements"]], ["fulfilled", "missing"])
        self.assertEqual(packet([], research_design=spec)["status"], "unanswered")

    def test_parse_does_not_mutate_caller_data(self):
        rows = [numeric()]
        sources = [source(rows[0]["quote"])]
        spec = design()
        before = copy.deepcopy((rows, sources, spec))
        packet(rows, sources, spec)
        self.assertEqual((rows, sources, spec), before)

    def test_payload_schema_not_arbitrary_calculations(self):
        with self.assertRaises(ModelResponseError):
            parse_evidence({"items": [], "calculations": []}, [], design(), AS_OF)
        with self.assertRaises(ModelResponseError):
            parse_evidence({"items": [numeric()] * 81}, [], design(), AS_OF)

    def test_prompt_contract_preserves_original_labels(self):
        self.assertIn("不翻譯", EVIDENCE_SYSTEM)
        self.assertIn("statement_type", EVIDENCE_SYSTEM)
        self.assertIn("kind", DESIGN_SYSTEM)


if __name__ == "__main__":
    unittest.main()
