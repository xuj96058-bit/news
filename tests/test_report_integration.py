import unittest
from pathlib import Path
import tempfile
from unittest.mock import MagicMock, patch

from src.analyzer import AnalysisResult, EventExtraction, ImpactItem, post_process_impacts
from src.report import generate_daily_report, load_report_events
from src.news_store import NewsStore
from src.rss_pipeline import analyze_pending


def result(ticker="BRK.B"):
    return AnalysisResult(
        extraction=EventExtraction(event_type="earnings", entities=[], fact="Reported earnings",
                                   direction="positive", confidence=0.8, reasoning="Reported growth"),
        impacts=[ImpactItem(ticker=ticker, direction="positive", strength="low",
                            path="direct", reason="Reported growth")],
        summary="Source-based summary", overall_confidence=0.8)


class IntegrationTests(unittest.TestCase):
    def test_watchlist_ticker_not_in_small_original_set_is_kept(self):
        with patch("src.sec_universe.load_universe", return_value={"members": [{"symbol": "UNH"}]}) as load:
            processed = post_process_impacts(result("UNH"), universe_path="watchlist.json")
        self.assertEqual([item.ticker for item in processed.impacts], ["UNH"])
        load.assert_called_once_with("watchlist.json")

    def test_share_class_is_not_discarded(self):
        processed = post_process_impacts(result())
        self.assertEqual([item.ticker for item in processed.impacts], ["BRK-B"])

    def test_report_includes_only_completed_rss_analysis_with_source(self):
        complete = {"status": "analyzed", "analysis": result().model_dump(),
                    "sources": [{"source_name": "Test source"}],
                    "url": "https://example.org/article", "content_scope": "summary",
                    "title": "Earnings update", "summary": "Original earnings summary",
                    "published_at": "2026-10-14T00:00:00Z"}
        pending = {"status": "pending", "analysis": None}
        inbox = MagicMock()
        inbox.export_articles.return_value = [complete, pending]
        with patch("src.report.load_events", return_value=[]), \
             patch("src.report.DEFAULT_DB_PATH") as db_path, \
             patch("src.report.NewsStore") as store:
            db_path.exists.return_value = True
            store.return_value.__enter__.return_value = inbox
            events = load_report_events()
            report = generate_daily_report(as_of="2026-10-14")
        self.assertEqual(events[0]["published_at"], complete["published_at"])
        self.assertEqual(events[0]["summary"], complete["summary"])
        self.assertIn("共 1 則", report)
        self.assertIn("Test source", report)
        self.assertIn("https://example.org/article", report)

    def test_analyzed_investat_fragment_flows_from_shared_inbox_into_daily_report(self):
        research = {"source_id": "investat", "source_name": "Investat institutional research",
                    "source_kind": "research", "region": "global", "external_id": "research_123",
                    "title": "A provider supplied earnings excerpt", "url": None,
                    "published_at": "2026-10-13T08:00:00Z", "summary": "Earnings grew during the reported period.",
                    "content": "", "content_scope": "fragment"}
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "news.sqlite3"
            with NewsStore(database) as inbox:
                inbox.ingest([research, dict(research, external_id="pending_456",
                                             title="Unanalyzed research", summary="Pending evidence.",
                                             published_at="2026-10-12T08:00:00Z")])
                with patch("src.analyzer.analyze_news", return_value=result()) as model:
                    outcome = analyze_pending(inbox, limit=1)
                self.assertEqual(outcome["analyzed"], 1)
                self.assertIn("Investat institutional research (research)", model.call_args.args[0])
                self.assertIn("fragment", model.call_args.args[0])
                self.assertIn("Earnings grew during the reported period.", model.call_args.args[0])
            with patch("src.report.load_events", return_value=[]), \
                 patch("src.report.DEFAULT_DB_PATH", database), \
                 patch("src.report.NewsStore", side_effect=lambda: NewsStore(database)):
                report = generate_daily_report(as_of="2026-10-13")
        self.assertIn("共 1 則", report)
        self.assertIn("Investat institutional research", report)
        self.assertIn("Source-based summary", report)
        self.assertNotIn("來源：None", report)
        self.assertNotIn("investat:research_123", report)


if __name__ == "__main__":
    unittest.main()

