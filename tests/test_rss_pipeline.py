import contextlib
from datetime import datetime, timezone
import io
import json
import unittest
from unittest.mock import patch

from src.feeds import Article, FeedFetchError, Source
from src.news_store import NewsStore
from src.rss_pipeline import analyze_pending, collect_sources, main


def source(identifier="test"):
    return Source(identifier, identifier, "https://example.org/feed", "official", "us")


def article(identifier="test", title="Central bank decision", date="2026-10-13T01:00:00+00:00",
            body="The central bank announced its rate decision today."):
    return Article(identifier, identifier, "official", "us", title,
                   "https://example.org/release/" + identifier, date, body, "", "summary", identifier)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.store = NewsStore(":memory:")
        self.addCleanup(self.store.close)
        self.now = datetime(2026, 10, 13, tzinfo=timezone.utc)

    def test_failed_source_does_not_discard_other_source(self):
        def fetch(s, **kwargs):
            if s.id == "broken":
                raise FeedFetchError("HTTP 403")
            return [article(s.id)]
        result = collect_sources([source("good"), source("broken")], self.store,
                                 fetcher=fetch, now=self.now)
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["failed_sources"], 1)
        self.assertEqual(result["sources"][1]["error"], "HTTP 403")

    def test_dates_and_limits_are_applied_before_storage(self):
        values = [article("old", date="2026-01-01T00:00:00+00:00"),
                  article("recent"), article("unknown", date=None)]
        report = collect_sources([source()], self.store, fetcher=lambda *a, **k: values,
                                 now=self.now, limit_per_source=1)
        self.assertEqual(report["sources"][0]["older_skipped"], 1)
        self.assertEqual(report["sources"][0]["limited"], 1)
        self.assertEqual(self.store.export_articles()[0]["source_id"], "recent")

    def test_collect_never_calls_model_and_repeated_run_deduplicates(self):
        with patch("src.analyzer.analyze_news") as model:
            for _ in range(2):
                report = collect_sources([source()], self.store,
                                         fetcher=lambda *a, **k: [article()], now=self.now)
        model.assert_not_called()
        self.assertEqual(report["added"], 0)
        self.assertEqual(report["duplicates"], 1)

    def test_unknown_exception_does_not_expose_credentials(self):
        def fetch(*a, **k):
            raise RuntimeError("Authorization: Bearer secret-key-value")
        report = collect_sources([source()], self.store, fetcher=fetch, now=self.now)
        self.assertEqual(report["sources"][0]["error"], "RuntimeError")
        self.assertNotIn("secret-key", json.dumps(report))

    def test_analysis_runs_once_and_preserves_provenance(self):
        self.store.ingest([article().to_dict()])
        calls = []
        def analyze(text):
            calls.append(text)
            return {"overall_confidence": 0.8}
        self.assertEqual(analyze_pending(self.store, analyzer=analyze)["analyzed"], 1)
        self.assertEqual(analyze_pending(self.store, analyzer=analyze)["analyzed"], 0)
        self.assertIn("https://example.org/release/test", calls[0])
        self.assertIn("summary", calls[0])

    def test_failed_analysis_can_be_retried_without_leaking_error(self):
        self.store.ingest([article().to_dict()])
        def failed(text):
            raise RuntimeError("sk-this-value-must-not-be-stored")
        self.assertEqual(analyze_pending(self.store, analyzer=failed)["failed"], 1)
        self.assertEqual(self.store.pending()[0]["error"], "RuntimeError")
        self.assertEqual(analyze_pending(self.store, analyzer=lambda _: {})["analyzed"], 1)

    def test_title_only_is_not_sent_to_model(self):
        self.store.ingest([article(body="").to_dict()])
        with patch("src.analyzer.analyze_news") as model:
            result = analyze_pending(self.store)
        model.assert_not_called()
        self.assertEqual(result["analyzed"], 0)

    def test_invalid_source_fails_before_network(self):
        with patch("src.rss_pipeline.fetch_source") as fetch:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(["--source", "no_such_source"])
        fetch.assert_not_called()

    def test_sec_page_cap_is_partial_success_not_all_failed(self):
        partial = {"added": 3, "updated": 0, "duplicates": 0,
                   "sources": [{"source_id": "sec_8_k", "status": "partial"}]}
        with patch("src.rss_pipeline.NewsStore", return_value=self.store), \
             patch("src.rss_pipeline.collect_sec", return_value=partial), \
             patch("src.rss_pipeline.write_json"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--sec-only"]), 2)

    def test_selected_universe_is_forwarded_to_model_validation(self):
        self.store.ingest([article().to_dict()])
        with patch("src.analyzer.analyze_news", return_value={}) as model:
            analyze_pending(self.store, universe_path="custom-universe.json")
        self.assertEqual(model.call_args.kwargs["universe_path"], "custom-universe.json")


if __name__ == "__main__":
    unittest.main()
