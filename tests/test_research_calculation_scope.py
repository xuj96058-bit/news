"""An extraction gap must not become a factual claim that sources lack data."""
import copy
import json
import unittest

from src.research_design import EVIDENCE_SYSTEM
from src.research_packet import (CALCULATION_SYSTEM, DEEP_WRITING_RULES,
    prepare_research_packet, prompt_packet, render_packet_appendix)
from test_research_design import AS_OF, TOPIC, design, numeric, source


MODEL_LIMIT = "完全沒有任何來源提供宏觀數值或集中度數據。"
DOCUMENT = "The institute reports a yield of 5.25% and two firms contributing 36% of expected index earnings growth."


class CalculationScopeTests(unittest.TestCase):
    def prepare(self, *, computed=False, document_operand=False):
        spec = design()
        spec["requirements"].append({"id": "R2", "question_id": "Q1",
            "description": "Attributed macro and concentration observations",
            "kind": "document", "critical": False})
        items = [numeric()]
        if computed:
            items.append(numeric(id="E2", value="25", period="2026Q3",
                quote="Alpha consolidated 2026Q3 revenue was 25 USD million."))
        document_id = f"E{len(items) + 1}"
        items.append({"id": document_id, "kind": "document", "requirement_id": "R2",
            "source_id": "S1", "quote": DOCUMENT, "statement": DOCUMENT,
            "event_status": "unknown"})
        requests = []
        if computed or document_operand:
            requests.append({"id": "C1", "kind": "percent_change",
                "operand_ids": {"base": "E1", "current": document_id if document_operand else "E2"},
                "comparison_mode": "period_change"})
        def request(_system, _payload, stage):
            if stage == "designing":
                return spec
            if stage == "organizing":
                return {"items": items}
            self.assertEqual(stage, "calculating")
            return {"requests": requests, "limitations": [MODEL_LIMIT]}
        return prepare_research_packet(TOPIC, [source("\n".join(row["quote"] for row in items))],
            as_of=AS_OF, request=request, retrieve_sources=False)

    def test_model_absence_claim_stays_in_json_not_appendix_or_writer_context(self):
        packet = self.prepare()
        before = copy.deepcopy(packet)
        self.assertEqual(packet["calculation_limits"], [MODEL_LIMIT])
        self.assertEqual(len(packet["evidence"]["items"]), 2)
        self.assertTrue(all(row["eligible"] for row in packet["evidence"]["items"]))
        body, compact = render_packet_appendix(packet), prompt_packet(packet)
        for rendered in (body, json.dumps(compact, ensure_ascii=False)):
            self.assertNotIn(MODEL_LIMIT, rendered)
            self.assertIn("不代表原始材料沒有數字", rendered)
            self.assertIn("1 項通過欄位與原文錨定檢查的數值紀錄、0 項程式計算結果", rendered)
        self.assertNotIn("calculation_limits", compact)
        self.assertEqual(compact["evidence"]["items"][1]["quote"], DOCUMENT)
        self.assertEqual(json.loads(json.dumps(packet))["calculation_limits"], [MODEL_LIMIT])
        self.assertEqual(packet, before)

    def test_completed_calculations_keep_their_values_and_operand_provenance(self):
        packet = self.prepare(computed=True)
        computed = packet["calculations"][0]
        self.assertEqual(computed["status"], "computed")
        self.assertEqual(computed["result"]["percent_change"], "25")
        compact = prompt_packet(packet)
        self.assertEqual(compact["calculations"], packet["calculations"])
        body = render_packet_appendix(packet)
        self.assertIn("2 項通過欄位與原文錨定檢查的數值紀錄、1 項程式計算結果", body)
        self.assertIn("percent\\_change：25", body)
        self.assertIn("E1", body)
        self.assertIn("E2", body)
        self.assertNotIn(MODEL_LIMIT, body)

    def test_document_numbers_do_not_become_unvalidated_calculation_operands(self):
        packet = self.prepare(document_operand=True)
        self.assertEqual(packet["calculations"][0]["status"], "unsupported")
        self.assertEqual(packet["calculations"][0]["reason"], "ineligible_evidence")
        self.assertIsNone(packet["calculations"][0]["result"])
        self.assertIn("0 項程式計算結果", render_packet_appendix(packet))

    def test_prompts_distinguish_extracted_fields_from_original_material(self):
        self.assertIn("未整理為 eligible numeric 不代表來源沒有數字", CALCULATION_SYSTEM)
        self.assertIn("未提取成 numeric 不代表原始材料沒有數據", EVIDENCE_SYSTEM)
        self.assertIn("操作數不足不等於原始來源沒有數字", DEEP_WRITING_RULES)


if __name__ == "__main__":
    unittest.main()
