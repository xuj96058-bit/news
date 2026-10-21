"""Offline, behavioural checks of event memory, evidence and bounded work."""
import copy
import json
import unittest
from unittest.mock import Mock, patch

from src.analysis_errors import ModelResponseError
from src.event_analysis import EventBrief, _context, _current_sources, analyze_event, brief_to_analysis, parse_event_brief
from src.event_store import EventStore, EventStoreError


def paragraph(text, ids=("S1",)):
    return {"text": text, "source_ids": list(ids)}


def article(text="Orders grew but costs remain elevated.", identifier="manual-news"):
    return {"id": identifier, "title": "Orders and margins", "content": text,
            "content_scope": "provided_text", "source_type": "manual"}


def plan(gaps=None):
    gaps = [] if gaps is None else gaps
    return {"research_question": "Do orders support profit growth?",
            "thesis": "Delivery and cost determine whether orders become profit.",
            "answer_scope": "narrowed" if gaps else "full", "source_ids": ["S1"],
            "arguments": [{"heading": "Conversion to profit", "claim": "Demand alone is insufficient.",
                           "source_ids": ["S1"], "counter_source_ids": [],
                           "reasoning": "Costs can offset demand growth.",
                           "verification": "Compare delivery and margin in the next disclosure."}],
            "missing_evidence": gaps}


def brief(judgement="initial"):
    return {"headline": "Orders need to become profit",
            "summary": paragraph("Demand supports revenue, but costs can constrain profit."),
            "facts": [paragraph("Orders grew but costs remain elevated.")],
            "new_information": paragraph("The supplied report describes stronger orders."),
            "judgement": judgement,
            "judgement_reason": paragraph("Profit recovery remains conditional on costs."),
            "impacts": [{"ticker": "TEST", "direction": "mixed",
                         "mechanism": paragraph("Sales growth can be offset by input costs."), "horizon": "medium term"}],
            "impact_scores": [],
            "counterarguments": [paragraph("An unverified alternative is that delivery efficiencies offset costs.")],
            "watchlist": [paragraph("If margin fails to improve as orders rise, the cost explanation gains support.")],
            "missing_evidence": [], "confidence": "medium"}


class EventAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.store = EventStore(":memory:")
        self.addCleanup(self.store.close)
        self.packets = []
        self.responses = []
        self.patch = patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected network"))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def complete(self, system, user, *, stage):
        self.packets.append((stage, json.loads(user), system))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def seed_responses(self, result=None, gaps=None):
        result = result or brief()
        self.responses = [plan(gaps), result,
                          {"brief": result, "findings": [], "verdict": "pass"}]

    def run_analysis(self, articles=None, **kwargs):
        return analyze_event(articles or [article()], topic="Orders and margins", store=self.store,
            provider="deepseek", model="deepseek-test", api_key="synthetic-secret",
            completion=self.complete, **kwargs)

    def test_explicit_three_stages_save_original_and_no_default_search(self):
        self.seed_responses()
        result = self.run_analysis(searcher=Mock(side_effect=AssertionError("Not opted in")))
        self.assertEqual([row[0] for row in self.packets], ["planning", "writing", "reviewing"])
        self.assertEqual(result["metadata"]["api_call_count"], 3)
        self.assertEqual(result["metadata"]["search_calls"], 0)
        self.assertEqual(result["original_metadata"]["review_status"], "pending")
        self.assertEqual(result["metadata"]["review_status"], "needs_review")
        self.assertNotIn("synthetic-secret", json.dumps(result))
        self.assertNotIn("synthetic-secret", json.dumps(self.packets))
        self.assertEqual(brief_to_analysis(result["brief"]).summary, result["brief"]["summary"]["text"])

    def test_same_inputs_reuse_snapshot_without_paid_call(self):
        self.seed_responses()
        first = self.run_analysis()
        again = self.run_analysis(event_id=first["event_id"])
        self.assertEqual(again["id"], first["id"])
        self.assertTrue(again["metadata"]["cached"])
        self.assertEqual(len(self.packets), 3)
        self.assertEqual(len(self.store.list_revisions(first["event_id"])), 1)

    def test_new_workflow_regenerates_legacy_brief_then_caches_new_version(self):
        self.seed_responses()
        with patch("src.event_analysis.WORKFLOW_VERSION", 2):
            old = self.run_analysis()
        self.seed_responses(brief("maintained"))
        updated = self.run_analysis(event_id=old["event_id"])
        self.assertNotEqual(updated["id"], old["id"])
        self.assertEqual(updated["metadata"]["workflow_version"], 4)
        self.assertEqual(self.store.list_revisions(old["event_id"])[1]["brief"], old["brief"])
        cached = self.run_analysis(event_id=old["event_id"])
        self.assertEqual(cached["id"], updated["id"])
        self.assertEqual(len(self.packets), 6)

    def test_company_only_product_news_can_retain_conditional_significance(self):
        # A short announcement lacks an earnings forecast, but still supports
        # an explicitly conditional explanation of the stated technical goal.
        content = ("英伟达与思科计划推出USFP光模块，支持每通道400Gb/s，"
                   "单模块总带宽最高12.8Tb/s，目标是在现有网络空间连接更多计算设备，"
                   "同时降低网络在整体AI工厂功耗中的占比。")
        item = article(content)
        item["title"] = "英伟达与思科计划推出USFP光模块"
        result_brief = brief()
        result_brief.update(
            headline="英偉達與思科瞄準AI工廠的互連瓶頸",
            summary=paragraph("英偉達與思科計劃推出USFP光模組，單模組總頻寬最高12.8Tb/s，"
                "目標是在相同網絡空間連接更多計算設備，並降低網絡的功耗佔比。"
                "若達成設計目標，較高密度的光互連有助緩解AI工廠擴展時的連接與能耗限制，"
                "因此對兩家的AI網絡方案具有產品布局意義；目前仍是計劃，不能視為已落地的訂單。"),
            facts=[paragraph(content)],
            new_information=paragraph("本次宣布USFP光模組的開發計劃與傳輸規格。"),
            judgement_reason=paragraph("提高互連密度若能實現，有助在既有空間連接更多計算設備。"),
            impacts=[],
            counterarguments=[paragraph("計劃中的規格仍須落實，技術目標不等於客戶採用。")],
            watchlist=[paragraph("後續實際產品能否達到公布的傳輸及功耗目標，將支持或削弱部署價值。")],
            missing_evidence=["實際產品達成傳輸與功耗目標的驗證"],
        )
        event_plan = {"research_question": "USFP開發計劃如何影響AI工廠互連？",
            "thesis": "更高互連密度有助緩解連接與能耗限制，仍以產品達成規格為條件。",
            "answer_scope": "narrowed", "source_ids": ["S1"],
            "arguments": [{"heading": "互連密度與部署", "claim": "密度改善有助連接更多設備。",
                "source_ids": ["S1"], "counter_source_ids": [],
                "reasoning": "相同網絡空間可連接更多設備，因而減輕擴展限制。",
                "verification": "核對實際產品是否達成公布規格。"}],
            "missing_evidence": result_brief["missing_evidence"]}
        self.responses = [event_plan, result_brief,
            {"brief": result_brief, "findings": [], "verdict": "needs_review"}]
        saved = self.run_analysis([item])
        self.assertEqual(saved["brief"]["summary"], result_brief["summary"])
        self.assertEqual(saved["brief"]["impacts"], [])
        self.assertEqual(saved["sources"][0]["content"], content)
        self.assertEqual(saved["metadata"]["api_call_count"], 3)
        self.assertEqual(brief_to_analysis(saved["brief"]).summary, result_brief["summary"]["text"])
        self.assertFalse(any("數字" in warning for warning in saved["metadata"]["validation_warnings"]))

    def test_article_tickers_preserved_as_labels_without_becoming_factual_evidence(self):
        item = article("The company plans a new product.")
        item["article_id"] = "canonical-article-id"
        item["tickers"] = [" nvda ", "NVDA", "CSCO", "2330", "not a ticker", None]
        source = _current_sources([item])[0]
        self.assertEqual(source["article_id"], "canonical-article-id")
        self.assertEqual(source["entity_metadata"], {
            "tickers": ["NVDA", "CSCO", "2330"], "role": "article_labels_not_news_evidence"})
        self.assertEqual(source["content"], item["content"])
        candidate = brief()
        candidate["facts"][0] = paragraph("Orders rose 2330%.")
        _, warnings = parse_event_brief(candidate, [source])
        self.assertTrue(any("數字" in warning for warning in warnings))
        self.seed_responses()
        saved = self.run_analysis([item])
        self.assertEqual(saved["sources"][0]["entity_metadata"], source["entity_metadata"])
        self.assertEqual(self.packets[0][1]["sources"][0]["entity_metadata"], source["entity_metadata"])

    def test_changed_ticker_labels_invalidate_cached_snapshot(self):
        item = article()
        item["tickers"] = ["TEST"]
        self.seed_responses()
        first = self.run_analysis([item])
        item["tickers"] = ["TEST", "SECOND"]
        self.seed_responses(brief("maintained"))
        second = self.run_analysis([item], event_id=first["event_id"])
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(second["sources"][0]["entity_metadata"]["tickers"], ["TEST", "SECOND"])

    def test_legacy_brief_defaults_to_no_scores_without_fabricating_ratings(self):
        legacy = brief()
        legacy.pop("impact_scores")
        result, _ = parse_event_brief(legacy, [{"id": "S1", "content": "Orders grew."}])
        self.assertEqual(result["impact_scores"], [])
        self.assertNotIn("impact_scores", legacy)
        self.assertEqual(brief_to_analysis(legacy).extraction.entities, ["TEST"])

    def test_new_writer_omitting_scores_is_rejected_before_save_or_review(self):
        candidate = brief()
        candidate.pop("impact_scores")
        self.responses = [plan(), candidate]
        with self.assertRaises(ModelResponseError):
            self.run_analysis()
        self.assertEqual(self.store.list_events(), [])
        self.assertEqual([row[0] for row in self.packets], ["planning", "writing"])

    def test_reviewer_omitting_scores_preserves_scored_draft_as_failed_review(self):
        candidate = brief()
        candidate["impact_scores"] = [{"target": "Company", "bullish_score": 6, "bearish_score": 2,
            "reason": paragraph("Demand supports sales while input costs pressure profit.")}]
        reviewed = copy.deepcopy(candidate)
        reviewed.pop("impact_scores")
        self.responses = [plan(), candidate, {"brief": reviewed, "findings": [], "verdict": "pass"}]
        saved = self.run_analysis()
        self.assertEqual(saved["metadata"]["review_status"], "failed")
        self.assertEqual(saved["brief"]["impact_scores"], candidate["impact_scores"])
        self.assertEqual(saved["brief"], saved["original_brief"])
        self.assertEqual(saved["metadata"]["api_call_count"], 3)

    def test_score_type_and_range_validation_rejects_non_integer_or_out_of_range_values(self):
        sources = [{"id": "S1", "content": "A company plans a new product."}]
        for field in ("bullish_score", "bearish_score"):
            for value in (-1, 11, True, False, 6.0, "6", None):
                with self.subTest(field=field, value=value):
                    candidate = brief()
                    score = {"target": "A company", "bullish_score": 6, "bearish_score": 1,
                             "reason": paragraph("The product may expand its offering.")}
                    score[field] = value
                    candidate["impact_scores"] = [score]
                    with self.assertRaises(ModelResponseError):
                        parse_event_brief(candidate, sources)
        for bullish, bearish in ((0, 0), (10, 10), (7, 1)):
            candidate = brief()
            candidate["impact_scores"] = [{"target": "A company", "bullish_score": bullish,
                "bearish_score": bearish, "reason": paragraph("The product could expand its offering.")}]
            result, warnings = parse_event_brief(candidate, sources)
            self.assertEqual(result["impact_scores"][0]["bullish_score"], bullish)
            self.assertEqual(result["impact_scores"][0]["bearish_score"], bearish)
            # Subjective score magnitudes are not source financial statistics.
            self.assertFalse(any("數字" in warning for warning in warnings))

    def test_score_reason_uses_owned_sources_and_retains_numeric_traceability(self):
        candidate = brief()
        candidate["impact_scores"] = [{"target": "A company", "bullish_score": 6,
            "bearish_score": 1, "reason": paragraph("The product may expand its offering.", ("S999",))}]
        self.responses = [plan(), candidate]
        with self.assertRaises(ModelResponseError):
            self.run_analysis()
        self.assertEqual(self.store.list_events(), [])
        self.assertEqual([row[0] for row in self.packets], ["planning", "writing"])
        candidate["impact_scores"][0]["reason"] = paragraph("Profit rose 73%.")
        _, warnings = parse_event_brief(candidate, [{"id": "S1", "content": "A new product was announced."}])
        self.assertTrue(any("數字" in warning for warning in warnings))

    def test_score_targets_are_cleaned_and_company_scores_survive_storage_and_digest(self):
        candidate = brief()
        candidate["impacts"] = []
        candidate["summary"] = paragraph("可能利好思科，因為新產品若達到設計目標，可擴展其網絡方案。")
        candidate["impact_scores"] = [{"target": "[思科](https://invented.example)", "bullish_score": 6,
            "bearish_score": 0, "reason": paragraph("新產品若達到設計目標，可擴展網絡方案。") }]
        self.seed_responses(candidate)
        saved = self.run_analysis([article("思科計劃推出新網絡產品。")])
        self.assertEqual(saved["brief"]["impact_scores"][0]["target"], "思科")
        self.assertNotIn("https://invented", json.dumps(saved["brief"], ensure_ascii=False))
        self.assertEqual(self.store.list_revisions(saved["event_id"])[0]["brief"]["impact_scores"],
                         saved["brief"]["impact_scores"])
        compatible = brief_to_analysis(saved["brief"])
        self.assertEqual(compatible.extraction.entities, ["思科"])
        self.assertEqual(compatible.extraction.direction, "positive")
        self.assertEqual(compatible.impacts, [])
        for bullish, bearish, expected in ((0, 0, "neutral"), (0, 6, "negative"), (6, 2, "mixed")):
            saved["brief"]["impact_scores"][0].update(bullish_score=bullish, bearish_score=bearish)
            self.assertEqual(brief_to_analysis(saved["brief"]).extraction.direction, expected)

    def test_scores_reject_empty_cleaned_targets_and_more_than_six_items(self):
        candidate = brief()
        score = {"target": "<script>", "bullish_score": 1, "bearish_score": 0,
                 "reason": paragraph("The product may expand its offering.")}
        candidate["impact_scores"] = [score]
        with self.assertRaises(ModelResponseError):
            parse_event_brief(candidate, [{"id": "S1", "content": "A company announced a product."}])
        candidate["impact_scores"] = [{**score, "target": f"Company {i}"} for i in range(7)]
        with self.assertRaises(ModelResponseError):
            parse_event_brief(candidate, [{"id": "S1", "content": "Several companies announced products."}])

    def test_writer_and_reviewer_limit_inference_from_omissions_and_procurement_terms(self):
        item = article("A software service will join a cloud marketplace. Customers may use existing cloud spending commitments.")
        self.seed_responses()
        self.run_analysis([item])
        for stage, packet, system in self.packets:
            if stage not in {"writing", "reviewing"}:
                continue
            with self.subTest(stage=stage):
                self.assertIn("材料沒有提及不等於現實中不存在", system)
                self.assertIn("材料未提供部署／實測等資料", system)
                self.assertIn("不能推成不必簽新合約、免新增付款流程", system)
                self.assertIn("最多支持「可能減少採購摩擦」", system)
                # Neither stage receives a fabricated contract or deployment
                # claim injected into the frozen evidentiary source.
                self.assertEqual(packet["sources"][0]["content"], item["content"])
        reviewer_system = next(system for stage, _, system in self.packets if stage == "reviewing")
        self.assertIn("逐句檢查三種越界並直接修正文句", reviewer_system)

    def test_writer_and_reviewer_condition_producer_benefit_on_unimpaired_output(self):
        item = article("Reports of an attack on a producing region raise concern about oil supply disruption.")
        self.seed_responses()
        self.run_analysis([item])
        for stage, packet, system in self.packets:
            if stage not in {"writing", "reviewing"}:
                continue
            with self.subTest(stage=stage):
                self.assertIn("商品供給、商品價格和企業收入分開分析", system)
                self.assertIn("自身產量、運輸及交付未受損為條件", system)
                self.assertIn("不能只根據售價上升判定淨利好", system)
                self.assertEqual(packet["sources"][0]["content"], item["content"])
        reviewer_system = next(system for stage, _, system in self.packets if stage == "reviewing")
        self.assertIn("不能只把必要條件藏在 counterarguments", reviewer_system)

    def test_writer_and_reviewer_receive_the_same_complete_output_schema(self):
        self.seed_responses()
        self.run_analysis()
        writing = next(packet for stage, packet, _ in self.packets if stage == "writing")
        reviewing = next(packet for stage, packet, _ in self.packets if stage == "reviewing")
        expected_schema = EventBrief.model_json_schema()
        self.assertNotIn("impact_scores", expected_schema["required"])
        expected_schema["required"].append("impact_scores")
        self.assertEqual(writing["writer_schema"], expected_schema)
        self.assertEqual(reviewing["writer_schema"], writing["writer_schema"])
        mechanism = writing["writer_schema"]["$defs"]["EventImpact"]["properties"]["mechanism"]
        self.assertEqual(mechanism, {"$ref": "#/$defs/Statement"})
        self.assertEqual(writing["writer_schema"]["$defs"]["Statement"]["required"], ["text", "source_ids"])

    def test_plain_text_impact_mechanism_is_rejected_without_retry_or_review(self):
        bad = brief()
        bad["impacts"][0]["mechanism"] = "Sales growth can be offset by input costs."
        self.responses = [plan(), bad]
        with self.assertRaises(ModelResponseError):
            self.run_analysis()
        self.assertEqual(self.store.list_events(), [])
        self.assertEqual([row[0] for row in self.packets], ["planning", "writing"])

    def test_nested_impact_citation_must_belong_to_supplied_sources(self):
        bad = brief()
        bad["impacts"][0]["mechanism"]["source_ids"] = ["S999"]
        self.responses = [plan(), bad]
        with self.assertRaises(ModelResponseError):
            self.run_analysis()
        self.assertEqual(self.store.list_events(), [])
        self.assertEqual([row[0] for row in self.packets], ["planning", "writing"])

    def test_changed_news_uses_remapped_raw_evidence_and_labels_prior_judgement(self):
        self.seed_responses()
        first = self.run_analysis()
        changed = brief("weakened")
        changed["summary"] = paragraph("Higher costs weaken the prior profitability view.", ("S1", "S2"))
        self.seed_responses(changed)
        second = self.run_analysis([article("Costs increased despite demand recovery.", "manual-update")],
                                   event_id=first["event_id"])
        packet = self.packets[3][1]
        self.assertEqual(packet["current_source_ids"], ["S1"])
        self.assertEqual(packet["history"][0]["role"], "prior_model_judgement_not_evidence")
        self.assertEqual(packet["history"][0]["summary"]["source_ids"], ["S2"])
        self.assertIn("Orders grew", packet["sources"][1]["content"])
        self.assertNotIn("Demand supports revenue", packet["sources"][1]["content"])
        self.assertEqual(second["brief"]["judgement"], "weakened")
        self.assertEqual(second["version"], 2)
        self.assertEqual(self.store.list_revisions(first["event_id"])[1]["brief"], first["brief"])

    def test_short_paste_equal_to_title_still_analyzed(self):
        self.seed_responses()
        item = article("Orders grew.")
        item["title"] = item["content"]
        result = self.run_analysis([item])
        self.assertEqual(result["sources"][0]["content"], "Orders grew.")

    def test_two_round_limit_replans_and_preserves_search_provenance(self):
        gaps = ["Comparable company margin disclosure"]
        self.responses = [plan(gaps), plan(gaps), plan(gaps), brief(),
                          {"brief": brief(), "findings": [], "verdict": "pass"}]
        search = Mock(side_effect=[{"sources": [{"title": "Company disclosure", "url": f"https://example.org/{i}",
            "content": f"Margin disclosure excerpt {i}", "source_type": "search"}],
            "usage": {"total_tokens": 20}, "warnings": []} for i in (1, 2)])
        result = self.run_analysis(search_enabled=True, search_api_key="separate-search-secret",
                                  max_search_rounds=2, max_search_turns=1, searcher=search)
        self.assertEqual(search.call_count, 2)
        self.assertEqual(len(self.packets), 5)
        self.assertEqual(result["metadata"]["api_call_count"], 5)
        self.assertEqual(result["metadata"]["search_calls"], 2)
        self.assertEqual(result["sources"][1]["source_type"], "search")
        self.assertEqual(result["sources"][1]["content_scope"], "search_summary")
        self.assertEqual(result["brief"]["missing_evidence"], gaps)
        self.assertEqual(search.call_args.kwargs["max_turns"], 1)
        self.assertEqual(search.call_args.kwargs["max_output_tokens"], 1500)
        self.assertNotIn("separate-search-secret", json.dumps(result))

    def test_no_source_search_stops_loop_and_never_invents_evidence(self):
        self.seed_responses(gaps=["Original disclosure"])
        search = Mock(return_value={"sources": [], "usage": {}, "warnings": ["No citations"]})
        result = self.run_analysis(search_enabled=True, search_api_key="search-secret",
                                  max_search_rounds=2, searcher=search)
        self.assertEqual(search.call_count, 1)
        self.assertEqual(len(result["sources"]), 1)
        self.assertEqual(result["brief"]["missing_evidence"], ["Original disclosure"])

    def test_failed_search_records_safe_usage_and_continues_with_gaps(self):
        self.seed_responses(gaps=["Original disclosure"])
        error = ModelResponseError("truncated")
        error.usage = {"total_tokens": 99}
        result = self.run_analysis(search_enabled=True, search_api_key="search-secret",
                                  searcher=Mock(side_effect=error))
        self.assertEqual(result["metadata"]["search_outcomes"][0]["usage"]["total_tokens"], 99)
        self.assertEqual(result["metadata"]["search_outcomes"][0]["status"], "failed")

    def test_unknown_source_rejected_before_save_or_review(self):
        bad = brief()
        bad["summary"]["source_ids"] = ["S999"]
        self.responses = [plan(), bad]
        with self.assertRaises(ModelResponseError):
            self.run_analysis()
        self.assertEqual(self.store.list_events(), [])
        self.assertEqual(len(self.packets), 2)

    def test_failed_draft_save_stops_before_reviewer(self):
        self.seed_responses()
        with patch.object(self.store, "create_revision", side_effect=EventStoreError("Save failed")):
            with self.assertRaises(EventStoreError):
                self.run_analysis()
        self.assertEqual(len(self.packets), 2)

    def test_failed_reviewer_retains_initial_snapshot_without_error_body(self):
        self.responses = [plan(), brief(), RuntimeError("synthetic-secret provider raw response")]
        result = self.run_analysis()
        self.assertEqual(result["metadata"]["review_status"], "failed")
        self.assertEqual(result["brief"], result["original_brief"])
        self.assertNotIn("synthetic-secret", json.dumps(result))

    def test_concurrent_existing_snapshot_is_not_reviewed_or_overwritten(self):
        self.seed_responses()
        existing = self.run_analysis()
        self.seed_responses()
        with patch.object(self.store, "find_revision", return_value=None), \
                patch.object(self.store, "create_revision", return_value=existing), \
                patch.object(self.store, "update_revision") as update:
            result = self.run_analysis(event_id=existing["event_id"])
        self.assertTrue(result["metadata"]["cached"])
        update.assert_not_called()
        self.assertEqual([x[0] for x in self.packets[3:]], ["planning", "writing"])

    def test_failed_final_save_returns_durable_draft_identity_for_recovery(self):
        self.seed_responses()
        with patch.object(self.store, "update_revision", side_effect=EventStoreError("storage unavailable")):
            result = self.run_analysis()
        self.assertEqual(result["metadata"]["review_status"], "pending")
        self.assertTrue(any("未能儲存" in x for x in result["metadata"]["validation_warnings"]))
        self.assertEqual(self.store.list_revisions(result["event_id"])[0]["id"], result["id"])
        self.assertEqual(self.run_analysis(event_id=result["event_id"])["id"], result["id"])
        self.assertEqual(len(self.packets), 3)

    def test_history_retains_truncation_and_stable_identity_across_generations(self):
        original = _current_sources([article("A" * 8000)])
        old = {"id": "prior", "created_at": "2026-10-21T00:00:00Z", "sources": original,
               "brief": brief(), "metadata": {}}
        second, _ = _context(_current_sources([article("New costs", "manual-new")]), [old])
        next_revision = {**old, "id": "next", "sources": second}
        third, _ = _context(_current_sources([article("New demand", "manual-newer")]), [next_revision, old])
        retained = [s for s in third if s.get("article_id") == "manual-news"]
        self.assertEqual(len(retained), 1)
        self.assertTrue(retained[0]["content_truncated"])
        self.assertEqual(retained[0]["included_character_count"], len(retained[0]["content"]))
        self.assertEqual(len(retained[0]["content"]), 3000)

    def test_initial_judgement_numeric_checks_and_model_links(self):
        value = brief("reversed")
        value["confidence"] = "high"
        value["facts"][0]["text"] = "Profit rose 73% [bad](https://invented.example) [S999]."
        result, warnings = parse_event_brief(value, [{"id": "S1", "content": "Orders grew.", "title": "Orders"}])
        self.assertEqual(result["judgement"], "initial")
        self.assertNotIn("https://invented", result["facts"][0]["text"])
        self.assertNotIn("S999", result["facts"][0]["text"])
        self.assertTrue(any("數字" in warning for warning in warnings))
        self.assertEqual(result["confidence"], "medium")

    def test_invalid_limits_and_missing_separate_search_key_prevent_paid_work(self):
        for options in ({"max_search_rounds": 3}, {"max_search_turns": True},
                        {"search_enabled": True, "search_api_key": ""}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.run_analysis(**options)
        self.assertEqual(self.packets, [])


if __name__ == "__main__":
    unittest.main()
