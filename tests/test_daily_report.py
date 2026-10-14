"""Daily publication boundaries and original-source workflow integration."""
import copy
import unittest
from unittest.mock import MagicMock, patch

from src.analysis_errors import ProviderConfigurationError
from src.research import ResearchError
from src.report import DAILY_TOPIC, generate_daily_essay, generate_daily_report, load_report_events


def event(title="Company earnings", published="2026-10-14T00:00:00Z", **changes):
    item = {
        "id": title, "title": title, "published_at": published,
        "source": "Publisher A", "source_url": "https://example.test/" + title.replace(" ", "-"),
        "summary": "Original news reports revenue of 20 million, up 5 percent.",
        "content": "", "content_scope": "summary",
        "analysis": {"extraction": {"event_type": "earnings", "entities": ["Company"],
            "fact": title, "direction": "positive", "confidence": 0.9, "reasoning": "Synthetic analysis"},
            "impacts": [], "summary": "OLD AI INTERPRETATION", "overall_confidence": 0.9},
    }
    item.update(changes)
    return item


class DailyDigestTests(unittest.TestCase):
    def test_hong_kong_day_includes_exact_boundaries_and_excludes_stale_unknown(self):
        events = [event("Before boundary", "2026-10-13T15:59:59Z"),
                  event("At boundary", "2026-10-13T16:00:00Z"),
                  event("End of day", "2026-10-14T15:59:59Z"),
                  event("Tomorrow", "2026-10-14T16:00:00Z"),
                  event("Unknown date", None)]
        with patch("src.report.load_report_events", return_value=events):
            report = generate_daily_report(as_of="2026-10-14")
        self.assertIn("共 2 則", report)
        self.assertIn("At boundary", report)
        self.assertIn("End of day", report)
        self.assertNotIn("Before boundary", report)
        self.assertNotIn("Tomorrow", report)
        self.assertNotIn("Unknown date", report)
        self.assertIn("日期未知 1 則", report)
        self.assertIn("非當日 2 則", report)

    def test_explicit_date_only_is_hong_kong_calendar_date(self):
        with patch("src.report.load_report_events", return_value=[event(published="2026-10-14")]):
            self.assertIn("共 1 則", generate_daily_report(as_of="2026-10-14"))

    def test_legacy_publication_date_is_used_but_saved_timestamp_is_not(self):
        dated = {"date": "2026-10-14", "news": "Original manual news", "source": "Manual",
                 "analysis": event()["analysis"]}
        saved = {"timestamp": "2026-10-14T00:00:00", "news": "Only a save date",
                 "analysis": event()["analysis"]}
        with patch("src.report.load_events", return_value=[dated, saved]), \
             patch("src.report.DEFAULT_DB_PATH") as path:
            path.exists.return_value = False
            loaded = load_report_events()
            report = generate_daily_report(as_of="2026-10-14")
        self.assertEqual(loaded[0]["published_at"], "2026-10-14")
        self.assertIsNone(loaded[1]["published_at"])
        self.assertIn("共 1 則", report)
        self.assertIn("日期未知 1 則", report)
        self.assertNotIn("Only a save date", report)

    def test_cross_source_syndication_merges_but_preserves_publishers(self):
        first = event("Revenue results")
        second = event("Revenue results", source="Publisher B", source_url="https://other.test/story")
        original = copy.deepcopy([first, second])
        with patch("src.report.load_report_events", return_value=[first, second]):
            report = generate_daily_report(as_of="2026-10-14")
        self.assertIn("共 1 則", report)
        self.assertIn("Publisher A, Publisher B", report)
        self.assertIn("合併重複 1 則", report)
        self.assertEqual([first, second], original)

    def test_tracking_variants_of_same_url_are_one_story(self):
        one = event("Initial story", source_url="https://example.test/story?utm_source=a")
        two = event("Updated headline", source_url="https://example.test/story?utm_source=b")
        with patch("src.report.load_report_events", return_value=[one, two]):
            self.assertIn("共 1 則", generate_daily_report(as_of="2026-10-14"))

    def test_url_and_text_bridge_merges_all_duplicate_groups(self):
        first = event("Early headline", source="Publisher A", source_url="https://example.test/story",
                      summary="Original first snapshot of the story.")
        second = event("Updated headline", source="Publisher B", source_url="https://other.test/syndicated",
                       summary="Updated original story with a revenue figure of 25 million.")
        bridge = event("Updated headline", source="Publisher C", source_url="https://example.test/story",
                       summary=second["summary"])
        with patch("src.report.load_report_events", return_value=[first, second, bridge]):
            report = generate_daily_report(as_of="2026-10-14")
        self.assertIn("共 1 則", report)
        self.assertIn("合併重複 2 則", report)
        self.assertIn("Publisher A, Publisher B, Publisher C", report)

    def test_identical_headline_with_different_evidence_remains_separate(self):
        one = event("Revenue results", summary="Revenue rose 5 percent.")
        two = event("Revenue results", source_url="https://other.test/story", summary="Revenue fell 8 percent.")
        with patch("src.report.load_report_events", return_value=[one, two]):
            self.assertIn("共 2 則", generate_daily_report(as_of="2026-10-14"))

    def test_market_scope_ranks_before_higher_model_confidence(self):
        company = event("Company product launch")
        company["analysis"]["overall_confidence"] = 0.99
        macro = event("Fed interest rate decision")
        macro["analysis"]["overall_confidence"] = 0.71
        with patch("src.report.load_report_events", return_value=[company, macro]):
            report = generate_daily_report(as_of="2026-10-14")
        self.assertLess(report.index("Fed interest rate decision"), report.index("Company product launch"))
        self.assertIn("未進行跨事件 AI 論說", report)

    def test_invalid_cutoff_is_a_safe_error(self):
        with self.assertRaises(ResearchError) as error:
            generate_daily_report(as_of="not-a-date")
        self.assertEqual(error.exception.reason, "invalid_dates")


class DailyEssayTests(unittest.TestCase):
    def test_essay_passes_only_today_original_text_to_shared_workflow(self):
        originals = [event(), event("Stale news", "2026-10-12T00:00:00Z"), event("Unknown", None)]
        progress, cancel = MagicMock(), MagicMock()
        with patch("src.report._load_daily_articles", return_value=originals), \
             patch("src.report.generate_research_workflow", return_value={"body": "Draft", "metadata": {}}) as workflow:
            result = generate_daily_essay(api_key="synthetic-key", provider="gemini", model="test-model",
                                         as_of="2026-10-14", on_progress=progress, register_cancel=cancel)
        topic, sources = workflow.call_args.args
        self.assertEqual(topic, DAILY_TOPIC)
        self.assertEqual(len(sources), 1)
        self.assertIn("Original news", sources[0]["content"])
        self.assertNotIn("OLD AI INTERPRETATION", sources[0]["content"])
        self.assertEqual(workflow.call_args.kwargs["writing_style"], "argumentative")
        self.assertEqual(workflow.call_args.kwargs["report_mode"], "argumentative")
        self.assertEqual(workflow.call_args.kwargs["provider"], "gemini")
        self.assertEqual(workflow.call_args.kwargs["model"], "test-model")
        self.assertIs(workflow.call_args.kwargs["on_progress"], progress)
        self.assertIs(workflow.call_args.kwargs["register_cancel"], cancel)
        self.assertEqual(result["metadata"]["daily_selection"]["unknown_date_excluded"], 1)
        self.assertEqual(result["metadata"]["daily_selection"]["outside_date_excluded"], 1)

    def test_pending_original_article_does_not_require_earlier_analysis(self):
        article = event(analysis=None, status="pending", source_name="Raw feed", source_id="feed")
        article["url"] = article.pop("source_url")
        article["sources"] = [{"source_id": "feed", "source_name": "Raw feed"}]
        inbox = MagicMock()
        inbox.export_articles.return_value = [article]
        with patch("src.report.load_events", return_value=[]), \
             patch("src.report.DEFAULT_DB_PATH") as path, \
             patch("src.report.NewsStore") as store, \
             patch("src.report.generate_research_workflow", return_value={"body": "Draft"}) as workflow:
            path.exists.return_value = True
            store.return_value.__enter__.return_value = inbox
            generate_daily_essay(api_key="synthetic-key", as_of="2026-10-14")
        self.assertEqual(workflow.call_count, 1)
        self.assertIn("Original news", workflow.call_args.args[1][0]["content"])

    def test_duplicate_originals_keep_multiple_attributions_in_one_snapshot(self):
        one = event("Revenue results")
        two = event("Revenue results", source="Publisher B", source_url="https://other.test/story", id="other")
        with patch("src.report._load_daily_articles", return_value=[one, two]), \
             patch("src.report.generate_research_workflow", return_value={}) as workflow:
            report = generate_daily_essay(api_key="synthetic-key", as_of="2026-10-14")
        sources = workflow.call_args.args[1]
        self.assertEqual(len(sources), 1)
        self.assertIn("Publisher B", [origin["publisher"] for origin in sources[0]["origins"]])
        self.assertEqual(report["metadata"]["daily_selection"]["duplicates_merged"], 1)

    def test_distinct_full_text_with_same_title_and_summary_is_not_dropped(self):
        one = event("Revenue results", id=None, content="Company A revenue rose 5 percent.")
        two = event("Revenue results", id=None, source_url="https://other.test/story",
                    content="Company B revenue fell 8 percent.")
        with patch("src.report._load_daily_articles", return_value=[one, two]), \
             patch("src.report.generate_research_workflow", return_value={}) as workflow:
            generate_daily_essay(api_key="synthetic-key", as_of="2026-10-14")
        self.assertEqual(len(workflow.call_args.args[1]), 2)

    def test_legacy_original_short_text_is_preserved_as_manual_evidence(self):
        original = {"date": "2026-10-14", "news": "Revenue rose 5 percent.", "source": "Manual"}
        with patch("src.report.load_events", return_value=[original]), \
             patch("src.report.DEFAULT_DB_PATH") as path, \
             patch("src.report.generate_research_workflow", return_value={}) as workflow:
            path.exists.return_value = False
            generate_daily_essay(api_key="synthetic-key", as_of="2026-10-14")
        source = workflow.call_args.args[1][0]
        self.assertEqual(source["content"], original["news"])
        self.assertEqual(source["source_type"], "manual")

    def test_empty_stale_unknown_and_title_only_do_not_call_workflow(self):
        for originals in ([], [event(published=None)], [event(published="2026-10-12")],
                          [event(summary="Company earnings", content_scope="title_only")]):
            with self.subTest(originals=originals), \
                 patch("src.report._load_daily_articles", return_value=originals), \
                 patch("src.report.generate_research_workflow") as workflow:
                with self.assertRaises(ResearchError) as error:
                    generate_daily_essay(api_key="synthetic-key", as_of="2026-10-14")
                self.assertEqual(error.exception.reason, "empty_sources")
                workflow.assert_not_called()

    def test_missing_key_stops_before_shared_retrieval_or_model(self):
        with patch("src.report._load_daily_articles", return_value=[event()]), \
             patch("src.report.generate_research_workflow") as workflow:
            with self.assertRaises(ProviderConfigurationError):
                generate_daily_essay(api_key="", as_of="2026-10-14")
        workflow.assert_not_called()

    def test_saved_writer_receives_daily_selection_and_final_augmented_sources_survive(self):
        saved = []
        final_sources = [{"id": "S1"}, {"id": "S2"}]

        def complete(*args, **kwargs):
            kwargs["on_draft"]({"title": "Daily draft", "body": "Draft", "metadata": {"stage": "writer"}})
            return {"body": "Reviewed", "sources": final_sources, "metadata": {"stage": "reviewed"}}

        with patch("src.report._load_daily_articles", return_value=[event()]), \
             patch("src.report.generate_research_workflow", side_effect=complete):
            report = generate_daily_essay(api_key="synthetic-key", as_of="2026-10-14", on_draft=saved.append)
        self.assertEqual(saved[0]["metadata"]["daily_selection"]["hong_kong_date"], "2026-10-14")
        self.assertEqual(report["sources"], final_sources)
        self.assertEqual(report["metadata"]["stage"], "reviewed")


if __name__ == "__main__":
    unittest.main()
