"""Adversarial snapshot fixtures for claims that have citations but no support."""
import copy
import unittest

from src.analysis_errors import ModelResponseError
from src.research_audit import parse_claim_audit
from src.research_calculations import calculate_requests
from test_research_calculations import fact


def paragraph(text="A OCF was 120 USD billion in FY2025.", section="thesis", number=1):
    return {"section": section, "paragraph": number, "text": text, "source_ids": ["S1"]}


def audit(status="supported", evidence_ids=None, calculation_ids=None):
    return {"claims": [{"section": "thesis", "paragraph": 1, "status": status,
                         "evidence_ids": ["E1"] if evidence_ids is None else evidence_ids,
                         "calculation_ids": calculation_ids or [], "reason": "Matches the stated source and scope."}],
            "summary": "Snapshot comparison only."}


class ResearchClaimAuditTests(unittest.TestCase):
    def test_supported_snapshot_is_not_independent_verification(self):
        result = parse_claim_audit(audit(), [paragraph()], [fact()], [])
        self.assertEqual(result["status"], "reviewed_snapshot")
        self.assertEqual(result["unresolved_count"], 0)
        self.assertFalse(result["independently_verified"])

    def test_same_number_in_another_role_cannot_pass(self):
        evidence = [fact(), fact("E2", metric="revenue", value="200")]
        result = parse_claim_audit(audit(evidence_ids=["E1", "E2"]),
                                  [paragraph("A OCF was 200 USD billion; revenue was 120 USD billion.")], evidence, [])
        self.assertEqual(result["claims"][0]["status"], "unknown")
        self.assertEqual(result["claims"][0]["model_status"], "supported")
        self.assertTrue(result["claims"][0]["program_reasons"])

    def test_same_metric_different_entity_cannot_pass(self):
        evidence = [fact(), fact("E2", entity="B", value="200")]
        result = parse_claim_audit(audit(evidence_ids=["E1", "E2"]),
                                  [paragraph("A OCF was 200; B OCF was 120.")], evidence, [])
        self.assertEqual(result["claims"][0]["status"], "unknown")

    def test_mixed_fiscal_year_currency_or_magnitude_is_unknown(self):
        for text in ("A OCF was 120 USD billion in CY2025.", "A OCF was 120 USD million in FY2025.",
                     "A OCF was 120 HKD billion in FY2025."):
            with self.subTest(text=text):
                result = parse_claim_audit(audit(), [paragraph(text)], [fact()], [])
                self.assertEqual(result["claims"][0]["status"], "unknown")

    def test_forecast_cannot_be_presented_as_actual_cashflow(self):
        for text in ("A OCF was 120 USD billion.", "A actual OCF was 120 USD billion."):
            result = parse_claim_audit(audit(), [paragraph(text)], [fact(statement_type="forecast")], [])
            self.assertEqual(result["claims"][0]["status"], "unknown")
        result = parse_claim_audit(audit(), [paragraph("A forecast OCF is 120 USD billion.")],
                                  [fact(statement_type="forecast")], [])
        self.assertEqual(result["claims"][0]["status"], "supported")

    def test_missing_paragraph_cannot_be_hidden_by_summary_or_scores(self):
        payload = audit()
        payload["quality"] = {"evidence": 4}
        result = parse_claim_audit(payload, [paragraph(), paragraph("Another claim.", "conclusion")], [fact()], [])
        self.assertEqual(result["claims"][1]["status"], "unknown")
        self.assertFalse(result["complete"])
        self.assertEqual(result["unresolved_count"], 1)

    def test_unknown_and_contradicted_stay_unresolved(self):
        for status in ("partial", "contradicted", "unknown"):
            result = parse_claim_audit(audit(status), [paragraph()], [fact()], [])
            self.assertEqual(result["claims"][0]["status"], status)
            self.assertEqual(result["unresolved_count"], 1)

    def test_ineligible_or_unanchored_evidence_cannot_support(self):
        for changes in ({"eligible": False}, {"anchor": None}, {"traceability": "unverified"}, {"conflict": True}):
            result = parse_claim_audit(audit(), [paragraph()], [fact(**changes)], [])
            self.assertEqual(result["claims"][0]["status"], "unknown")

    def test_unknown_ids_reject_response_and_empty_support_becomes_unknown(self):
        with self.assertRaises(ModelResponseError):
            parse_claim_audit(audit(evidence_ids=["E404"]), [paragraph()], [fact()], [])
        result = parse_claim_audit(audit(evidence_ids=[]), [paragraph()], [fact()], [])
        self.assertEqual(result["claims"][0]["status"], "unknown")

    def test_document_can_support_policy_claim_but_not_unbound_financial_number(self):
        document = fact(kind="document", quote="The policy becomes effective on the stated date.")
        document.pop("value")
        result = parse_claim_audit(audit(), [paragraph("The policy specifies an effective date.")], [document], [])
        self.assertEqual(result["claims"][0]["status"], "supported")
        result = parse_claim_audit(audit(), [paragraph("OCF was 120.")], [document], [])
        self.assertEqual(result["claims"][0]["status"], "unknown")

    def test_calculated_fcf_requires_complete_inputs_and_source_citations(self):
        evidence = [fact(), fact("E2", metric="CapEx", value="80")]
        calculations = calculate_requests([{"kind": "cashflow_coverage", "operand_ids": {"ocf": "E1", "capex": "E2"}}], evidence)
        result = parse_claim_audit(audit(evidence_ids=[], calculation_ids=["C1"]),
                                  [paragraph("A FCF was 40 USD billion.")], evidence, calculations)
        self.assertEqual(result["claims"][0]["status"], "supported")
        changed = paragraph("A FCF was 40 USD billion.")
        changed["source_ids"] = []
        result = parse_claim_audit(audit(evidence_ids=[], calculation_ids=["C1"]), [changed], evidence, calculations)
        self.assertEqual(result["claims"][0]["status"], "unknown")

    def test_scenario_npv_is_not_an_actual_return(self):
        calculation = {"id": "C1", "kind": "npv", "status": "computed", "value_type": "scenario",
                       "result": {"npv": "10"}, "entity": "A", "evidence_ids": [], "source_ids": []}
        payload = audit(evidence_ids=[], calculation_ids=["C1"])
        result = parse_claim_audit(payload, [paragraph("A NPV was 10.")], [], [calculation])
        self.assertEqual(result["claims"][0]["status"], "unknown")
        result = parse_claim_audit(payload, [paragraph("Under these scenario assumptions, A NPV was 10.")], [], [calculation])
        self.assertEqual(result["claims"][0]["status"], "supported")

    def test_computed_fcf_keeps_currency_period_and_actual_forecast_identity(self):
        actual = [fact(), fact("E2", metric="CapEx", value="80")]
        forecast = [fact(statement_type="forecast"), fact("E2", metric="CapEx", value="80", statement_type="forecast")]
        request = [{"kind": "cashflow_coverage", "operand_ids": {"ocf": "E1", "capex": "E2"}}]
        payload = audit(evidence_ids=["E1", "E2"], calculation_ids=["C1"])
        for evidence, output in ((actual, "FCF was 40 HKD million in CY2025."),
                                 (actual, "FCF was 40 USD million in FY2025."),
                                 (actual, "FCF was 40 USD billion in FY2024."),
                                 (actual, "FCF was 1.5 USD billion in FY2025."),
                                 (actual, "coverage ratio was 1.5%."),
                                 (forecast, "Actual FCF was 40 USD billion in FY2025.")):
            with self.subTest(output=output):
                inputs = ("A forecast OCF was 120 USD billion; forecast CapEx was 80 USD billion; "
                          if evidence is forecast else "A OCF was 120 USD billion; CapEx was 80 USD billion; ")
                result = parse_claim_audit(payload, [paragraph(inputs + output)], evidence,
                                          calculate_requests(request, evidence))
                self.assertEqual(result["claims"][0]["status"], "unknown")
                self.assertTrue(result["claims"][0]["program_reasons"])
        for evidence, text in ((actual, "A FCF was 40 USD billion in FY2025; coverage ratio was 1.5."),
                               (forecast, "A forecast FCF is 40 USD billion in FY2025.")):
            result = parse_claim_audit(payload, [paragraph(text)], evidence, calculate_requests(request, evidence))
            self.assertEqual(result["claims"][0]["status"], "supported", result)

    def test_qualifiers_must_match_one_record_not_cherry_picked_records(self):
        evidence = [fact(value="40", period="FY2025", unit="USD billion"),
                    fact("E2", value="40", period="FY2024", unit="HKD billion")]
        result = parse_claim_audit(audit(evidence_ids=["E1", "E2"]),
                                  [paragraph("A OCF was 40 USD billion in FY2024.")], evidence, [])
        self.assertEqual(result["claims"][0]["status"], "unknown")

    def test_valid_percentage_calculation_uses_percent_unit_and_both_periods(self):
        evidence = [fact(value="100", period="FY2024"), fact("E2", value="125", period="FY2025")]
        calculation = calculate_requests([{"kind": "percent_change", "operand_ids": {"base": "E1", "current": "E2"},
                                           "comparison_mode": "period_change"}], evidence)
        before = copy.deepcopy(calculation)
        payload = audit(evidence_ids=["E1", "E2"], calculation_ids=["C1"])
        result = parse_claim_audit(payload, [paragraph("A OCF increased 25% from FY2024 to FY2025.")], evidence, calculation)
        self.assertEqual(result["claims"][0]["status"], "supported", result)
        result = parse_claim_audit(payload, [paragraph("A OCF increased 25% from FY2023 to FY2025.")], evidence, calculation)
        self.assertEqual(result["claims"][0]["status"], "unknown")
        self.assertEqual(calculation, before)

    def test_bad_schema_and_duplicate_locations_are_rejected(self):
        for update in ({"status": []}, {"paragraph": True}, {"section": "unknown"}):
            payload = audit()
            payload["claims"][0].update(update)
            with self.assertRaises(ModelResponseError):
                parse_claim_audit(payload, [paragraph()], [fact()], [])
        payload = audit()
        payload["claims"].append(copy.deepcopy(payload["claims"][0]))
        with self.assertRaises(ModelResponseError):
            parse_claim_audit(payload, [paragraph()], [fact()], [])


if __name__ == "__main__":
    unittest.main()
