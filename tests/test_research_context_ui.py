"""Opt-in research search and explicitly selected historical judgement."""

import json
import os
import unittest
from unittest.mock import patch

from src.research import _validated_sources, prepare_evidence, prepare_sources
from src.research_store import ResearchStore
from tests.test_research_ui import ResearchUiHarness


class ResearchContextUiTests(ResearchUiHarness, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.start_patch(patch.dict(os.environ, {"XAI_API_KEY": ""}))

    def test_context_defaults_do_not_select_history_or_search_on_rerun(self):
        self.create(title="Previous judgement")
        app = self.app(report_mode="argumentative")
        self.assertEqual(app.selectbox(key="research_previous_report").value, "")
        self.assertFalse(app.checkbox(key="research_search_enabled").value)
        app.run()
        self.generator.assert_not_called()
        app.sidebar.text_input(key="ai_api_key").input("synthetic-analysis-key").run()
        app.button(key="research_generate").click().run()
        self.assertEqual(len(app.exception), 0)
        options = self.generator.call_args.kwargs
        self.assertFalse(options["search_enabled"])
        self.assertEqual(options["search_api_key"], "")
        self.assertIsNone(options["previous_research"])

    def test_separate_xai_key_is_required_for_other_analysis_provider(self):
        app = self.app(report_mode="argumentative")
        app.sidebar.text_input(key="ai_api_key").input("synthetic-analysis-key").run()
        app.checkbox(key="research_search_enabled").check().run()
        self.assertTrue(app.button(key="research_generate").disabled)
        self.assertEqual(app.text_input(key="research_search_api_key").value, "")
        self.generator.assert_not_called()
        app.text_input(key="research_search_api_key").input("synthetic-xai-search-key").run()
        app.checkbox(key="research_search_x").check().run()
        app.number_input(key="research_search_rounds").set_value(2).run()
        self.generator.assert_not_called()
        app.button(key="research_generate").click().run()
        self.assertEqual(len(app.exception), 0)
        options = self.generator.call_args.kwargs
        self.assertEqual(options["api_key"], "synthetic-analysis-key")
        self.assertEqual(options["search_api_key"], "synthetic-xai-search-key")
        self.assertTrue(options["search_enabled"])
        self.assertTrue(options["include_x"])
        self.assertEqual(options["max_search_rounds"], 2)
        with ResearchStore(self.db) as store:
            saved = store.list_reports()[0]
        self.assertNotIn("synthetic-xai-search-key", json.dumps(saved))
        self.assertNotIn("synthetic-analysis-key", json.dumps(saved))

    def test_xai_analysis_uses_current_key_and_search_key_can_be_cleared(self):
        app = self.app(report_mode="argumentative")
        app.checkbox(key="research_search_enabled").check().run()
        app.text_input(key="research_search_api_key").input("synthetic-search-key").run()
        app.button(key="research_clear_search_key").click().run()
        self.assertEqual(app.text_input(key="research_search_api_key").value, "")
        app.sidebar.selectbox(key="ai_provider").select("xai").run()
        app.sidebar.text_input(key="ai_api_key").input("synthetic-current-xai-key").run()
        app.button(key="research_generate").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(self.generator.call_args.kwargs["search_api_key"], "synthetic-current-xai-key")

    def test_explicit_prior_report_passes_compact_saved_snapshot_without_old_sources(self):
        previous = self.create(title="Order recovery", body="Prior judgement [S1]. " + "x" * 13000)
        app = self.app(report_mode="argumentative")
        app.sidebar.text_input(key="ai_api_key").input("synthetic-analysis-key").run()
        app.selectbox(key="research_previous_report").select(previous["id"]).run()
        self.generator.assert_not_called()
        app.button(key="research_generate").click().run()
        self.assertEqual(len(app.exception), 0)
        context = self.generator.call_args.kwargs["previous_research"]
        self.assertEqual(context["id"], previous["id"])
        self.assertEqual(context["version"], previous["version"])
        self.assertEqual(len(context["body"]), 12000)
        self.assertTrue(context["body_truncated"])
        self.assertNotIn("sources", context)
        with ResearchStore(self.db) as store:
            self.assertEqual(store.get_report(previous["id"])["body"], previous["body"])

    def test_switching_to_legacy_does_not_apply_stale_options(self):
        previous = self.create()
        app = self.app(report_mode="argumentative")
        app.sidebar.text_input(key="ai_api_key").input("synthetic-analysis-key").run()
        app.selectbox(key="research_previous_report").select(previous["id"]).run()
        app.checkbox(key="research_search_enabled").check().run()
        app.text_input(key="research_search_api_key").input("synthetic-search-key").run()
        app.radio(key="research_report_mode").set_value("資料缺口簡報").run()
        app.button(key="research_generate").click().run()
        self.assertEqual(len(app.exception), 0)
        options = self.generator.call_args.kwargs
        self.assertFalse(options["search_enabled"])
        self.assertEqual(options["search_api_key"], "")
        self.assertIsNone(options["previous_research"])

    def test_saved_comparison_and_search_outcomes_are_visible_without_new_calls(self):
        self.reviewed_draft["metadata"].update(
            previous_research_summary={"id": "prior", "title": "Earlier orders study", "version": 2,
                                       "as_of": "2026-10-14"},
            research_change={"status": "weaken", "new_information": {
                "text": "Costs reduce the strength of the prior order thesis.", "source_ids": ["S1"]},
                "reason": {"text": "Orders need to convert to profit.", "source_ids": ["S1"]}},
            evidence_search={"enabled": True, "rounds": [{"round": 1, "status": "added",
                "message": "Synthetic supplement retained.", "source_ids": ["S2"],
                "usage": {"input_tokens": 40}}], "warnings": ["Synthetic summary remains unverified."]})
        app = self.app(report_mode="argumentative")
        app.sidebar.text_input(key="ai_api_key").input("synthetic-analysis-key").run()
        app.button(key="research_generate").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(any("Costs reduce" in item.value for item in app.text))
        self.assertTrue(any("Synthetic supplement retained" in item.value for item in app.text))
        self.assertTrue(any("Synthetic summary remains unverified" in item.value for item in app.text))
        app.run()
        self.assertEqual(self.generator.call_count, 1)


class ResearchSearchSourceTests(unittest.TestCase):
    def test_search_provenance_is_preserved_without_verification_upgrade(self):
        source = {"title": "Search summary", "content": "Model search summary of orders.",
                  "url": "https://issuer.example/results", "source_type": "search",
                  "content_scope": "official_evidence", "verification_status": "verified",
                  "independently_verified": True, "independent_evidence": True}
        prepared = prepare_evidence([], [source])
        self.assertEqual(prepared[0]["source_type"], "search")
        self.assertEqual(prepared[0]["content_scope"], "search_summary")
        checked = _validated_sources([{**source, "id": "S1"}])[0]
        self.assertEqual(checked["verification_status"], "not_independently_verified")
        self.assertFalse(checked["independently_verified"])
        self.assertFalse(checked["independent_evidence"])
        article_prepared = prepare_sources([source])[0]
        self.assertEqual(article_prepared["source_type"], "search")
        self.assertEqual(article_prepared["content_scope"], "search_summary")


if __name__ == "__main__":
    unittest.main()
