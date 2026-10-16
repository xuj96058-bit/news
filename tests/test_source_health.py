from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from src.feeds import Article
from src.news_store import NewsStore
from src.rss_pipeline import collect_sec
from src.sec_universe import SecConfigError
from src.source_health import health_summary, record_collection, usable_text_count


class HealthTests(unittest.TestCase):
    def test_window_counts_empty_error_partial_and_all_time_last_success(self):
        now = datetime(2026, 10, 16, tzinfo=timezone.utc)
        with NewsStore(":memory:") as store:
            record_collection(store, {"id": "a", "status": "ok", "added": 10}, now - timedelta(days=31))
            record_collection(store, {"id": "a", "status": "empty"}, now - timedelta(days=3))
            record_collection(store, {"id": "a", "status": "error", "error": "HTTP 403"}, now - timedelta(days=2))
            record_collection(store, {"id": "a", "status": "partial", "selected": 4, "fetched": 9,
                                      "usable_text": 2, "added": 3, "limited": 5}, now - timedelta(days=1))
            health = health_summary(store, source_ids=["a", "never"], now=now)
            self.assertEqual(health[0]["runs"], 3)
            self.assertEqual(health[0]["success_rate"], 1 / 3)
            self.assertEqual(health[0]["empty_runs"], 1)
            self.assertEqual(health[0]["partial_runs"], 1)
            self.assertEqual(health[0]["errors"], 1)
            self.assertEqual(health[0]["added"], 3)
            self.assertEqual(health[0]["usable_text_ratio"], 0.5)
            self.assertEqual(health[0]["last_success"], (now - timedelta(days=3)).isoformat())
            self.assertEqual(health[1]["last_status"], "never_run")
            self.assertIsNone(health[1]["success_rate"])

    def test_title_and_metadata_are_not_counted_as_usable_body(self):
        items = [{"title": "A", "summary": "A", "content_scope": "summary"},
                 {"title": "B", "summary": "Actual description", "content_scope": "summary"},
                 {"title": "C", "summary": "Document category", "content_scope": "announcement_metadata"},
                 {"title": "D", "summary": "SEC filing notice metadata", "content_scope": "filing_notice"}]
        self.assertEqual(usable_text_count(items), 1)

    def test_arbitrary_error_details_never_enter_history(self):
        with NewsStore(":memory:") as store:
            record_collection(store, {"id": "test", "status": "error", "error": "failed bearer ift__sensitive-token"})
            self.assertNotIn("sensitive", str(health_summary(store)))
            self.assertEqual(health_summary(store)[0]["last_error"], "Collection error (details omitted)")

    def test_sec_success_records_one_aggregate_and_keeps_form_metrics(self):
        universe = {"counts": {}, "fetched_at": "2026-10-16T00:00:00+00:00", "sources": []}
        article = Article("sec_8_k", "SEC", "official", "us", "8-K - Example",
                          "https://www.sec.gov/Archives/edgar/data/1/report.htm", None,
                          "Filing notice metadata", "", "filing_notice", "filing-1")
        diagnostics = [{"source_id": "sec_8_k", "status": "ok", "scanned": 10, "pages": 1},
                       {"source_id": "sec_10_q", "status": "ok", "scanned": 5, "pages": 1}]
        with NewsStore(":memory:") as store, patch("src.rss_pipeline.load_universe", return_value=universe), \
                patch("src.rss_pipeline.collect_sec_filings", return_value=([article], diagnostics)):
            result = collect_sec(store)
            aggregate = health_summary(store, source_ids=["sec"])[0]
            self.assertEqual(aggregate["runs"], 1)
            self.assertEqual(aggregate["success_rate"], 1)
            self.assertEqual(aggregate["added"], result["added"])
            self.assertEqual(aggregate["fetched"], 15)
            self.assertEqual(aggregate["selected"], 1)
            self.assertEqual(aggregate["usable_text_ratio"], 0)
            self.assertEqual(health_summary(store, source_ids=["sec_8_k"])[0]["runs"], 1)

    def test_sec_access_limit_sentinel_does_not_duplicate_aggregate(self):
        universe = {"counts": {}, "fetched_at": "2026-10-16T00:00:00+00:00", "sources": []}
        diagnostics = [{"source_id": "sec_8_k", "status": "ok", "scanned": 10},
                       {"source_id": "sec_10_q", "status": "error", "error": "HTTP 403"},
                       {"source_id": "sec", "status": "error", "error": "SEC access limited; remaining feeds skipped"}]
        with NewsStore(":memory:") as store, patch("src.rss_pipeline.load_universe", return_value=universe), \
                patch("src.rss_pipeline.collect_sec_filings", return_value=([], diagnostics)):
            collect_sec(store)
            aggregate = health_summary(store, source_ids=["sec"])[0]
            self.assertEqual(aggregate["runs"], 1)
            self.assertEqual(aggregate["last_status"], "partial")
            self.assertEqual(aggregate["last_error"], "HTTP 403")

    def test_sec_configuration_failure_records_exactly_one_aggregate(self):
        with NewsStore(":memory:") as store, patch("src.rss_pipeline.load_universe", side_effect=SecConfigError("Missing universe")):
            collect_sec(store)
            aggregate = health_summary(store, source_ids=["sec"])[0]
            self.assertEqual(aggregate["runs"], 1)
            self.assertEqual(aggregate["last_status"], "error")


if __name__ == "__main__":
    unittest.main()
