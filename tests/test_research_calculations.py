"""Fixed financial fixtures: units, periods, scope and scenario semantics."""
import copy
from decimal import Decimal
import unittest

from src.research_calculations import calculate_requests


def fact(identifier="E1", **changes):
    result = {"id": identifier, "kind": "numeric", "source_id": "S1", "quote": "A FY2025 OCF USD billion 120",
              "entity": "A", "metric": "OCF", "value": "120", "unit": "USD billion", "period": "FY2025",
              "scope": "consolidated including leases", "statement_type": "actual", "eligible": True,
              "traceability": "anchored", "anchor": {"start": 0, "end": 28}, "conflict": False}
    result.update(changes)
    return result


class ResearchCalculationTests(unittest.TestCase):
    def coverage(self, left=None, right=None):
        return calculate_requests([{"id": "C1", "kind": "cashflow_coverage",
                                    "operand_ids": {"ocf": "E1", "capex": "E2"}}],
                                  [left or fact(), right or fact("E2", metric="CapEx", value="80")])[0]

    def test_cashflow_coverage_has_traceable_inputs_and_does_not_claim_roi(self):
        result = self.coverage()
        self.assertEqual(result["status"], "computed")
        self.assertEqual(result["result"], {"coverage_ratio": "1.5", "fcf": "40"})
        self.assertEqual(result["evidence_ids"], ["E1", "E2"])
        self.assertEqual(result["source_ids"], ["S1"])
        self.assertEqual(result["inputs"]["ocf"]["value"], "120")
        self.assertIn("不等於新增投資回報", result["limitation"])

    def test_cross_market_and_period_mismatches_are_not_calculated(self):
        for field, value in (("entity", "B"), ("unit", "CNY billion"), ("unit", "USD million"),
                             ("period", "CY2025"), ("period", "FY2024"), ("period", "2025-Q1"),
                             ("scope", "AI only"), ("statement_type", "forecast")):
            with self.subTest(field=field, value=value):
                result = self.coverage(right=fact("E2", metric="CapEx", value="80", **{field: value}))
                self.assertEqual(result["status"], "unsupported")
                self.assertEqual(result["reason"], "incompatible_" + field)
                self.assertIsNone(result["result"])

    def test_revenue_cannot_be_used_as_operating_cashflow(self):
        result = self.coverage(left=fact(metric="revenue"))
        self.assertEqual(result["reason"], "incompatible_metric_role")
        self.assertIsNone(result["result"])

    def test_unavailable_or_conflicted_values_do_not_default_to_zero(self):
        for changes in ({"value": None}, {"eligible": False}, {"conflict": True},
                        {"traceability": "unverified"}, {"scope": ""}, {"statement_type": "assumption"}):
            with self.subTest(changes=changes):
                result = self.coverage(left=fact(**changes))
                self.assertEqual(result["status"], "unsupported")
                self.assertIsNone(result["result"])

    def test_nonpositive_ocf_or_capex_is_explicitly_not_applicable(self):
        for left, right in ((fact(value="0"), fact("E2", metric="CapEx", value="80")),
                            (fact(), fact("E2", metric="CapEx", value="-80"))):
            result = self.coverage(left, right)
            self.assertEqual(result["status"], "not_applicable")
            self.assertIsNone(result["result"])

    def test_historical_change_requires_explicit_compatible_periods(self):
        evidence = [fact(period="FY2024", value="100"), fact("E2", period="FY2025", value="125")]
        request = {"kind": "percent_change", "operand_ids": {"base": "E1", "current": "E2"}}
        self.assertEqual(calculate_requests([request], evidence)[0]["reason"], "incompatible_period")
        request["comparison_mode"] = "period_change"
        result = calculate_requests([request], evidence)[0]
        self.assertEqual(result["result"], {"percent_change": "25"})
        self.assertEqual(result["period"], ["FY2024", "FY2025"])
        evidence[1]["period"] = "CY2025"
        self.assertEqual(calculate_requests([request], evidence)[0]["status"], "unsupported")

    def test_percent_change_never_hides_zero_or_negative_denominator(self):
        for value in ("0", "-10"):
            result = calculate_requests([{"kind": "percent_change", "operand_ids": {"base": "E1", "current": "E2"}}],
                                        [fact(value=value), fact("E2", value="20")])[0]
            self.assertEqual(result["status"], "not_applicable")

    def test_fiscal_end_dates_alone_do_not_establish_equal_duration(self):
        evidence = [fact(period="2024-06-30", value="100"), fact("E2", period="2025-06-30", value="125")]
        request = {"kind": "percent_change", "operand_ids": {"base": "E1", "current": "E2"}, "comparison_mode": "period_change"}
        self.assertEqual(calculate_requests([request], evidence)[0]["reason"], "incompatible_period")

    def test_delta_preserves_decimal_precision_and_input_objects(self):
        evidence = [fact(value="0.1"), fact("E2", value="0.3")]
        original = copy.deepcopy(evidence)
        result = calculate_requests([{"kind": "delta", "operand_ids": {"base": "E1", "current": "E2"}}], evidence)[0]
        self.assertEqual(result["result"], {"delta": "0.2"})
        self.assertEqual(evidence, original)

    def scenario(self):
        return {"id": "C1", "kind": "npv", "assumptions": {
            "investment": "100", "cash_flows": ["30"] * 5, "discount_rate": "0.1", "years": 5,
            "entity": "hypothetical project", "unit": "USD million", "scope": "net incremental project cash flows",
            "period": "five years from investment", "basis": "Teaching scenario; not an observed company return."}}

    def test_npv_matches_hand_calculation_and_is_explicitly_a_scenario(self):
        result = calculate_requests([self.scenario()], [])[0]
        self.assertEqual(result["status"], "computed")
        self.assertEqual(result["value_type"], "scenario")
        self.assertAlmostEqual(float(result["result"]["npv"]), 13.7236030823, places=8)
        self.assertEqual(result["inputs"]["discount_rate_unit"], "decimal")
        self.assertFalse(result["independently_verified"])

    def test_npv_requires_all_cashflows_and_explicit_assumptions(self):
        for key in ("investment", "cash_flows", "discount_rate", "years", "basis", "unit", "scope"):
            request = self.scenario()
            request["assumptions"].pop(key)
            result = calculate_requests([request], [])[0]
            self.assertEqual(result["status"], "unsupported", key)
            self.assertIsNone(result["result"])
        request = self.scenario()
        request["assumptions"]["cash_flows"][2] = None
        self.assertEqual(calculate_requests([request], [])[0]["status"], "unsupported")

    def test_arbitrary_formula_and_unknown_evidence_are_not_executed(self):
        result = calculate_requests([{"kind": "eval", "formula": "__import__('os')"}], [])[0]
        self.assertEqual(result["reason"], "unsupported_formula")
        result = calculate_requests([{"kind": "delta", "operand_ids": {"base": "E1", "current": "E9"}}], [fact()])[0]
        self.assertEqual(result["reason"], "missing_evidence")


if __name__ == "__main__":
    unittest.main()
