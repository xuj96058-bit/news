import unittest
from unittest.mock import MagicMock, patch

from src.analyzer import AnalysisResult, EventExtraction, ImpactItem, post_process_impacts
from src.report import generate_daily_report


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
                    "url": "https://example.org/article", "content_scope": "summary"}
        pending = {"status": "pending", "analysis": None}
        inbox = MagicMock()
        inbox.export_articles.return_value = [complete, pending]
        with patch("src.report.load_events", return_value=[]), \
             patch("src.report.DEFAULT_DB_PATH") as db_path, \
             patch("src.report.NewsStore") as store:
            db_path.exists.return_value = True
            store.return_value.__enter__.return_value = inbox
            report = generate_daily_report()
        self.assertIn("共 1 則", report)
        self.assertIn("Test source", report)
        self.assertIn("https://example.org/article", report)


if __name__ == "__main__":
    unittest.main()
