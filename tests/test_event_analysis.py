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

    def test_writer_and_reviewer_receive_the_same_complete_output_schema(self):
        self.seed_responses()
        self.run_analysis()
        writing = next(packet for stage, packet, _ in self.packets if stage == "writing")
        reviewing = next(packet for stage, packet, _ in self.packets if stage == "reviewing")
        self.assertEqual(writing["writer_schema"], EventBrief.model_json_schema())
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
