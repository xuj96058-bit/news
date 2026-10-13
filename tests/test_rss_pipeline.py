import contextlib
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
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

    def test_investat_cli_is_opt_in_and_uses_the_shared_store_without_ai(self):
        public = {"added": 1, "updated": 0, "duplicates": 0, "failed_sources": 0,
                  "sources": [{"id": "test", "status": "ok"}]}
        research = {"added": 2, "updated": 1, "duplicates": 3, "failed_sources": 0,
                    "sources": [{"id": "investat", "name": "Investat", "status": "ok"}]}
        for options, enabled in (([], False), (["--investat", "--investat-limit", "7"], True)):
            with self.subTest(options=options), \
                 patch("src.rss_pipeline.NewsStore", return_value=self.store), \
                 patch("src.rss_pipeline.load_sources", return_value=[source()]), \
                 patch("src.rss_pipeline.collect_sources", return_value=dict(public, sources=list(public["sources"]))) as feeds, \
                 patch("src.rss_pipeline.collect_investat", return_value=research) as investat, \
                 patch("src.rss_pipeline.analyze_pending") as analyze, \
                 patch("src.rss_pipeline.write_json") as write, \
                 contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(main(options), 0)
                feeds.assert_called_once()
                self.assertIs(feeds.call_args.args[1], self.store)
                if enabled:
                    investat.assert_called_once_with(self.store, limit=7, timeout=15)
                else:
                    investat.assert_not_called()
                analyze.assert_not_called()
                report = json.loads(stdout.getvalue())
                self.assertEqual(report["added"], 3 if enabled else 1)
                self.assertEqual(report["updated"], 1 if enabled else 0)
                self.assertEqual(report["duplicates"], 3 if enabled else 0)
                self.assertEqual([s["id"] for s in report["sources"]],
                                 ["test", "investat"] if enabled else ["test"])
                self.assertEqual(write.call_args.args[1], report)
            # main owns and closes its store, so give the next iteration a new one.
            if not enabled:
                self.store = NewsStore(":memory:")

    def test_investat_only_skips_public_sources_and_failure_is_reported(self):
        failure = {"added": 0, "updated": 0, "duplicates": 0, "failed_sources": 1,
                   "sources": [{"id": "investat", "name": "Investat", "status": "error",
                                "error": "Authentication required"}]}
        with patch("src.rss_pipeline.NewsStore", return_value=self.store), \
             patch("src.rss_pipeline.load_sources", return_value=[]), \
             patch("src.rss_pipeline.collect_sources") as feeds, \
             patch("src.rss_pipeline.collect_sec") as sec, \
             patch("src.rss_pipeline.collect_investat", return_value=failure) as investat, \
             patch("src.rss_pipeline.analyze_pending") as analyze, \
             patch("src.rss_pipeline.write_json"), \
             contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(main(["--investat-only", "--investat-limit", "4", "--timeout", "3"]), 1)
        investat.assert_called_once_with(self.store, limit=4, timeout=3)
        feeds.assert_not_called()
        sec.assert_not_called()
        analyze.assert_not_called()
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["failed_sources"], 1)
        self.assertEqual(report["sources"], failure["sources"])
        self.assertEqual(report["investat"], failure)

    def test_investat_failure_preserves_public_success_and_returns_partial_exit(self):
        public = {"added": 1, "updated": 0, "duplicates": 0, "failed_sources": 0,
                  "sources": [{"id": "test", "status": "ok"}]}
        failure = {"added": 0, "updated": 0, "duplicates": 0, "failed_sources": 1,
                   "sources": [{"id": "investat", "name": "Investat", "status": "error",
                                "error": "Request failed"}]}
        with patch("src.rss_pipeline.NewsStore", return_value=self.store), \
             patch("src.rss_pipeline.load_sources", return_value=[source()]), \
             patch("src.rss_pipeline.collect_sources", return_value=public), \
             patch("src.rss_pipeline.collect_investat", return_value=failure), \
             patch("src.rss_pipeline.write_json"), \
             contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(main(["--investat"]), 2)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["added"], 1)
        self.assertEqual(report["failed_sources"], 1)
        self.assertEqual([s["status"] for s in report["sources"]], ["ok", "error"])

    def test_investat_cli_conflicts_and_invalid_limits_fail_before_collection(self):
        options = (["--analysis-only", "--investat"],
                   ["--analysis-only", "--investat-only"],
                   ["--investat-only", "--source", "test"],
                   ["--investat-only", "--sec"],
                   ["--investat-only", "--investat-limit", "0"],
                   ["--investat", "--investat-limit", "51"])
        with patch("src.rss_pipeline.NewsStore") as store, \
             patch("src.rss_pipeline.collect_investat") as investat, \
             patch("src.rss_pipeline.collect_sources") as feeds:
            for argv in options:
                with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), \
                     self.assertRaises(SystemExit):
                    main(argv)
        store.assert_not_called()
        investat.assert_not_called()
        feeds.assert_not_called()

    def test_investat_only_runs_without_loading_missing_public_feed_config(self):
        research = {"added": 1, "updated": 0, "duplicates": 0, "failed_sources": 0,
                    "sources": [{"id": "investat", "name": "Investat", "status": "ok"}]}
        with tempfile.TemporaryDirectory() as directory:
            missing_config = Path(directory) / "missing-public-feeds.json"
            self.assertFalse(missing_config.exists())
            with patch("src.rss_pipeline.NewsStore", return_value=self.store), \
                 patch("src.rss_pipeline.load_sources", side_effect=AssertionError("Public config must not be read")) as load, \
                 patch("src.rss_pipeline.collect_sources") as feeds, \
                 patch("src.rss_pipeline.collect_investat", return_value=research) as investat, \
                 patch("src.rss_pipeline.analyze_pending") as analyze, \
                 patch("src.rss_pipeline.write_json"), \
                 patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected network call")) as request, \
                 contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(main(["--investat-only", "--config", str(missing_config),
                                       "--investat-limit", "3"]), 0)
        load.assert_not_called()
        feeds.assert_not_called()
        analyze.assert_not_called()
        request.assert_not_called()
        investat.assert_called_once_with(self.store, limit=3, timeout=15)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["added"], 1)
        self.assertEqual(report["sources"], research["sources"])

    def test_investat_only_list_sources_reports_mcp_source_without_collection(self):
        with patch("src.rss_pipeline.load_sources", side_effect=AssertionError("Public config must not be read")) as load, \
             patch("src.rss_pipeline.NewsStore") as store, \
             patch("src.rss_pipeline.collect_sources") as feeds, \
             patch("src.rss_pipeline.collect_investat") as investat, \
             patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected network call")) as request, \
             contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(main(["--investat-only", "--list-sources"]), 0)
        self.assertEqual(json.loads(stdout.getvalue()), [{
            "id": "investat", "name": "Investat", "kind": "research",
            "format": "mcp", "enabled": True,
        }])
        load.assert_not_called()
        store.assert_not_called()
        feeds.assert_not_called()
        investat.assert_not_called()
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()

