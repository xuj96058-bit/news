import json
import unittest
from unittest.mock import patch

from src.investat import InvestatError
from src.investat_pipeline import collect_investat
from src.news_store import NewsStore
from src.rss_pipeline import analyze_pending


TOKEN = "synthetic-investat-token"
SECRET = "synthetic-sensitive-provider-value"


def item(identifier=7, **overrides):
    value = {"id": identifier, "title": "Bank research on rates", "summary": "Rates may remain unchanged.",
             "institution": "Test Bank", "publishedAt": "2026-10-13T08:00:00Z"}
    value.update(overrides)
    return value


class Client:
    def __init__(self, items=None, details=None, *, list_error=None):
        self.items = [item()] if items is None else items
        self.details = details or {(7, 0): {"id": 7, "body": "The bank expects rates to remain unchanged."}}
        self.list_error = list_error
        self.calls = []

    def list_articles(self, **kwargs):
        if self.list_error:
            raise self.list_error
        return self.items

    def get_article(self, article_id, body_offset=0):
        self.calls.append((article_id, body_offset))
        result = self.details.get((article_id, body_offset), {"id": article_id})
        if isinstance(result, Exception):
            raise result
        return result


class InvestatPipelineTests(unittest.TestCase):
    def setUp(self):
        self.store = NewsStore(":memory:")
        self.addCleanup(self.store.close)

    def collect(self, client=None, **kwargs):
        client = client or Client()
        return collect_investat(self.store, token=TOKEN, client_factory=lambda **options: client, **kwargs)

    def test_normalizes_without_inventing_url_or_storing_provider_metadata(self):
        raw = item(_meta={"token": SECRET}, debug=SECRET, sessionId=SECRET)
        detail = {"id": 7, "body": "Bank research body.", "_meta": {"token": SECRET}, "authorization": SECRET}
        report = self.collect(Client([raw], {(7, 0): detail}))
        self.assertEqual(report["added"], 1)
        self.assertEqual(report["sources"][0]["status"], "ok")
        article = self.store.export_articles()[0]
        self.assertEqual(article["source_name"], "Investat / Test Bank")
        self.assertEqual(article["source_kind"], "research")
        self.assertEqual(article["region"], "global")
        self.assertEqual(article["external_id"], "7")
        self.assertEqual(article["url"], "")
        self.assertEqual(article["canonical_url"], "investat:7")
        self.assertEqual(article["content_scope"], "provided_text")
        self.assertEqual(article["published_at"], raw["publishedAt"])
        self.assertNotIn(SECRET, json.dumps(article))
        self.assertNotIn(SECRET, json.dumps(report))

    def test_real_source_url_is_preserved_and_bad_links_are_not_saved(self):
        self.collect(Client([item(sourceUrl="https://bank.example/research/7")],
                            {(7, 0): {"body": "text", "url": "https://user:password@bad.example/"}}))
        self.assertEqual(self.store.export_articles()[0]["url"], "https://bank.example/research/7")
        self.collect(Client([item(8, url="javascript:alert(1)")], {(8, 0): {"body": "other text"}}))
        rows = {article["external_id"]: article for article in self.store.export_articles()}
        self.assertEqual(rows["8"]["url"], "")

    def test_repeated_collection_deduplicates_and_never_calls_llm(self):
        with patch("src.analyzer.analyze_news") as model:
            self.assertEqual(self.collect()["added"], 1)
            record = self.store.pending()[0]
            self.store.mark_analyzed(record["id"], {})
            self.assertEqual(self.collect()["duplicates"], 1)
        model.assert_not_called()
        self.assertEqual(self.store.stats()["total"], 1)
        self.assertEqual(self.store.pending(), [])

    def test_chunks_follow_explicit_cursor_and_preserve_character_boundaries(self):
        client = Client(details={(7, 0): {"body": "Rate out", "nextBodyOffset": 8},
                                 (7, 8): {"body": "look.  ", "nextBodyOffset": 15},
                                 (7, 15): {"body": "Second paragraph.", "nextBodyOffset": None}})
        report = self.collect(client)
        self.assertEqual(client.calls, [(7, 0), (7, 8), (7, 15)])
        self.assertEqual(self.store.export_articles()[0]["content"], "Rate outlook.  Second paragraph.")
        self.assertEqual(report["sources"][0]["body_pages"], 3)
        self.assertEqual(report["failed_sources"], 0)

    def test_page_cap_retains_provided_fragment_as_partial(self):
        client = Client(details={(7, 0): {"body": "First fragment", "nextBodyOffset": 14}})
        report = self.collect(client, max_body_pages=1)
        self.assertEqual(client.calls, [(7, 0)])
        self.assertEqual(report["sources"][0]["status"], "partial")
        self.assertEqual(report["failed_sources"], 1)
        row = self.store.export_articles()[0]
        self.assertEqual(row["content"], "First fragment")
        self.assertEqual(row["content_scope"], "partial_text")

    def test_bad_cursors_are_bounded_and_partial(self):
        for cursor in (0, -1, True, "20", {}):
            with self.subTest(cursor=cursor), NewsStore(":memory:") as store:
                client = Client(details={(7, 0): {"body": "Fragment", "nextBodyOffset": cursor}})
                report = collect_investat(store, token=TOKEN, client_factory=lambda **_: client)
                self.assertEqual(client.calls, [(7, 0)])
                self.assertEqual(report["failed_sources"], 1)
                self.assertEqual(store.export_articles()[0]["content_scope"], "partial_text")

    def test_missing_cursor_does_not_guess_an_offset(self):
        client = Client(details={(7, 0): {"body": "Fragment", "hasMoreBody": True}})
        self.assertEqual(self.collect(client)["failed_sources"], 1)
        self.assertEqual(client.calls, [(7, 0)])
        self.assertEqual(self.store.export_articles()[0]["content_scope"], "partial_text")

    def test_repeated_or_decreasing_cursor_stops_after_supplied_chunks(self):
        for cursor in (10, 5):
            with self.subTest(cursor=cursor), NewsStore(":memory:") as store:
                client = Client(details={(7, 0): {"body": "First part", "nextBodyOffset": 10},
                                         (7, 10): {"body": " second part", "nextBodyOffset": cursor}})
                report = collect_investat(store, token=TOKEN, client_factory=lambda **_: client)
                self.assertEqual(client.calls, [(7, 0), (7, 10)])
                self.assertEqual(report["failed_sources"], 1)
                self.assertEqual(store.export_articles()[0]["content"], "First part second part")

    def test_blank_detail_metadata_retains_list_summary_and_institution(self):
        client = Client(details={(7, 0): {"id": 7, "title": "  ", "summary": " ", "institution": {}}})
        report = self.collect(client)
        self.assertEqual(report["added"], 1)
        row = self.store.export_articles()[0]
        self.assertEqual(row["title"], item()["title"])
        self.assertEqual(row["summary"], item()["summary"])
        self.assertEqual(row["source_name"], "Investat / Test Bank")
        self.assertEqual(row["content_scope"], "summary")

    def test_partial_detail_failure_saves_summary_and_available_chunks_safely(self):
        client = Client([item(7), item(8, title="Other research")],
                        {(7, 0): {"body": "First part", "nextBodyOffset": 10},
                         (7, 10): RuntimeError(SECRET), (8, 0): RuntimeError(SECRET)})
        report = self.collect(client)
        self.assertEqual(report["added"], 2)
        self.assertEqual(report["sources"][0]["partial_articles"], 2)
        rows = {article["external_id"]: article for article in self.store.export_articles()}
        self.assertEqual(rows["7"]["content"], "First part")
        self.assertEqual(rows["7"]["content_scope"], "partial_text")
        self.assertEqual(rows["8"]["content_scope"], "summary")
        self.assertNotIn(SECRET, json.dumps(report))
        self.assertNotIn(SECRET, json.dumps(list(rows.values())))

    def test_authentication_or_rate_limit_stops_later_detail_requests(self):
        for status in (401, 403, 429):
            with self.subTest(status=status), NewsStore(":memory:") as store:
                client = Client([item(7), item(8, title="Second research")],
                                {(7, 0): InvestatError("authentication", status_code=status)})
                report = collect_investat(store, token=TOKEN, client_factory=lambda **_: client)
                self.assertEqual(client.calls, [(7, 0)])
                self.assertEqual(report["added"], 2)
                self.assertEqual(report["sources"][0]["details_skipped"], 1)
                self.assertEqual(report["sources"][0]["status"], "partial")

    def test_detail_id_mismatch_is_not_saved_as_article_body(self):
        report = self.collect(Client(details={(7, 0): {"id": 8, "body": "Wrong article body"}}))
        self.assertEqual(report["failed_sources"], 1)
        self.assertEqual(self.store.export_articles()[0]["content"], "")

    def test_empty_and_failed_lists_return_safe_reports(self):
        self.assertEqual(self.collect(Client([]))["sources"][0]["status"], "empty")
        report = self.collect(Client(list_error=RuntimeError(SECRET)))
        self.assertEqual(report["failed_sources"], 1)
        self.assertEqual(report["sources"][0]["status"], "error")
        self.assertNotIn(SECRET, json.dumps(report))
        self.assertEqual(self.store.stats()["total"], 0)

    def test_invalid_articles_do_not_discard_valid_article_and_limits_apply(self):
        client = Client([{"summary": "Missing ID"}, item(), item(9, title="Over limit")])
        report = self.collect(client, limit=2)
        self.assertEqual(report["added"], 1)
        self.assertEqual(report["sources"][0]["skipped"], 1)
        self.assertEqual(client.calls, [(7, 0)])
        for options in ({"limit": 0}, {"limit": 51}, {"limit": True},
                        {"max_body_pages": 0}, {"max_body_pages": True}, {"timeout": 0}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.collect(**options)

    def test_factory_receives_session_token_and_timeout(self):
        options = []
        def factory(**kwargs):
            options.append(kwargs)
            return Client([])
        collect_investat(self.store, token=TOKEN, timeout=3, client_factory=factory)
        self.assertEqual(options, [{"token": TOKEN, "timeout": 3}])

    def test_collected_research_uses_shared_analysis_once_and_retains_provenance(self):
        self.collect()
        calls = []
        def analyze(text):
            calls.append(text)
            return {"overall_confidence": 0.8}
        self.assertEqual(analyze_pending(self.store, analyzer=analyze)["analyzed"], 1)
        self.assertEqual(analyze_pending(self.store, analyzer=analyze)["analyzed"], 0)
        self.assertIn("Investat / Test Bank (research)", calls[0])
        self.assertIn("provided_text", calls[0])
        self.assertIn("Bank research", calls[0])


if __name__ == "__main__":
    unittest.main()
