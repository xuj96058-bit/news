import json
import unittest
from unittest.mock import patch

from src.investat import InvestatError
from src.investat_pipeline import collect_investat, DETAIL_REFRESH_SECONDS, DETAIL_RETRY_SECONDS
from src.news_store import NewsStore
from tests.test_investat_pipeline import Client, item


class IncrementalInvestatTests(unittest.TestCase):
    def setUp(self):
        self.store = NewsStore(":memory:")
        self.addCleanup(self.store.close)
        self.now = 1_800_000_000.0
        clock = patch("src.investat_pipeline._incremental_now", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def collect(self, client=None, **options):
        client = client or Client()
        return collect_investat(self.store, client_factory=lambda **_: client,
                                **{"incremental": True, **options})

    def source(self, identifier=7):
        row = self.store.connection.execute(
            "SELECT metadata_json FROM article_sources WHERE source_id='investat' AND canonical_url=?",
            (f"investat:{identifier}",)).fetchone()
        return json.loads(row[0]) if row else None

    def cache(self, identifier=7):
        return self.store.connection.execute(
            "SELECT * FROM investat_incremental_cache WHERE external_id=?", (str(identifier),)).fetchone()

    def test_second_poll_skips_unchanged_detail_and_ingest(self):
        first = Client()
        self.assertEqual(self.collect(first)["added"], 1)
        self.assertEqual(first.calls, [(7, 0)])
        self.now += 300
        second = Client()
        with patch.object(self.store, "ingest", wraps=self.store.ingest) as ingest:
            report = self.collect(second)
        ingest.assert_not_called()
        self.assertEqual(second.calls, [])
        self.assertEqual(report["duplicates"], 1)
        self.assertEqual(report["sources"][0]["usable_text"], 1)
        self.assertEqual(report["sources"][0]["details_skipped"], 1)

    def test_new_id_only_fetches_new_detail(self):
        self.collect()
        self.now += 300
        client = Client([item(), item(8, title="New research")],
                        {(8, 0): {"body": "New report body"}})
        report = self.collect(client)
        self.assertEqual(client.calls, [(8, 0)])
        self.assertEqual((report["added"], report["duplicates"]), (1, 1))

    def test_first_cache_adoption_skips_existing_full_details(self):
        self.collect(incremental=False)
        client = Client()
        report = self.collect(client)
        self.assertEqual(client.calls, [])
        self.assertEqual(report["duplicates"], 1)
        self.assertEqual(self.cache()["next_detail_at"], self.now + DETAIL_REFRESH_SECONDS)

    def test_first_cache_adoption_does_not_hide_changed_list_metadata(self):
        self.collect(incremental=False)
        client = Client([item(title="Revised rates outlook")])
        report = self.collect(client)
        self.assertEqual(client.calls, [(7, 0)])
        self.assertEqual(report["updated"], 1)
        self.assertEqual(self.source()["title"], "Revised rates outlook")

    def test_changed_title_and_revision_fetch_details(self):
        self.collect()
        for listed in (item(title="Updated outlook"), item(title="Updated outlook", version=2)):
            self.now += 300
            client = Client([listed], {(7, 0): {"body": f"Body at {self.now}"}})
            self.assertEqual(self.collect(client)["updated"], 1)
            self.assertEqual(client.calls, [(7, 0)])

    def test_daily_refresh_discovers_silent_body_change(self):
        self.collect()
        self.now += DETAIL_REFRESH_SECONDS
        client = Client(details={(7, 0): {"body": "Updated without list metadata changes"}})
        self.assertEqual(self.collect(client)["updated"], 1)
        self.assertEqual(client.calls, [(7, 0)])
        self.now += 300
        another = Client()
        self.collect(another)
        self.assertEqual(another.calls, [])

    def test_partial_body_is_throttled_then_retried(self):
        partial = Client(details={(7, 0): {"body": "First part", "hasMoreBody": True}})
        self.assertEqual(self.collect(partial)["sources"][0]["status"], "partial")
        due = self.cache()["next_detail_at"]
        self.now += 300
        waiting = Client()
        report = self.collect(waiting)
        self.assertEqual(waiting.calls, [])
        self.assertEqual(report["sources"][0]["status"], "partial")
        self.assertEqual(report["sources"][0]["partial_articles"], 1)
        self.now = due
        complete = Client(details={(7, 0): {"body": "First part and the rest"}})
        self.collect(complete)
        self.assertEqual(complete.calls, [(7, 0)])
        self.assertEqual(self.source()["content_scope"], "provided_text")
        self.assertEqual(self.cache()["detail_state"], "complete")

    def test_changed_metadata_during_partial_wait_does_not_postpone_retry(self):
        self.collect(Client(details={(7, 0): InvestatError("timeout")}))
        due = self.cache()["next_detail_at"]
        self.now += 300
        client = Client([item(title="Updated headline")])
        report = self.collect(client)
        self.assertEqual(client.calls, [])
        self.assertEqual(report["updated"], 1)
        self.assertEqual(self.source()["title"], "Updated headline")
        self.assertEqual(self.cache()["next_detail_at"], due)

    def test_429_stops_all_detail_calls_across_polls_including_new_ids(self):
        throttled = InvestatError("rate_limit", status_code=429)
        # Simulate the transport's sanitized numeric Retry-After contract.
        throttled.retry_after = 3600
        client = Client([item(), item(8, title="Second report")], {(7, 0): throttled})
        report = self.collect(client)
        self.assertEqual(client.calls, [(7, 0)])
        self.assertEqual(report["added"], 2)
        self.assertNotIn("retry_after", report["sources"][0])
        self.assertEqual(report["sources"][0]["detail_retry_after_seconds"], 3600)
        self.assertEqual(self.cache(8)["next_detail_at"], self.now + 3600)
        self.now += 300
        new = Client([item(), item(8, title="Second report"), item(9, title="Third report")])
        report = self.collect(new)
        self.assertEqual(new.calls, [])
        self.assertEqual(report["added"], 1)
        self.assertEqual(self.source(9)["content_scope"], "summary")
        self.now += 3300
        retry = Client()
        self.collect(retry)
        self.assertEqual(retry.calls, [(7, 0)])

    def test_list_429_exposes_global_retry_after(self):
        failure = InvestatError("rate_limit", status_code=429)
        failure.retry_after = 4000
        report = self.collect(Client(list_error=failure))
        self.assertEqual(report["sources"][0]["retry_after"], 4000)
        self.assertEqual(report["sources"][0]["provider_status_code"], 429)
        self.assertEqual(report["failed_sources"], 1)

    def test_manual_default_keeps_fetching_bodies_without_cache(self):
        for _ in range(2):
            client = Client()
            collect_investat(self.store, client_factory=lambda **_: client)
            self.assertEqual(client.calls, [(7, 0)])
        self.assertIsNone(self.store.connection.execute(
            "SELECT name FROM sqlite_master WHERE name='investat_incremental_cache'").fetchone())

    def test_rolled_back_ingest_never_advances_cache(self):
        with patch("src.news_store.enqueue_notification", side_effect=RuntimeError("synthetic failure")):
            report = self.collect()
        self.assertEqual(report["sources"][0]["skipped"], 1)
        self.assertIsNone(self.source())
        self.assertIsNone(self.cache())
        retry = Client()
        self.assertEqual(self.collect(retry)["added"], 1)
        self.assertEqual(retry.calls, [(7, 0)])

    def test_missing_source_is_not_hidden_by_orphan_cache(self):
        from src.investat_pipeline import _incremental_schema, _list_signature
        _incremental_schema(self.store)
        with self.store.connection:
            self.store.connection.execute("INSERT INTO investat_incremental_cache VALUES(?,?,?,?,?)",
                                          ("7", _list_signature(item(), "7"),
                                           self.now + 86400, "complete", self.now))
        client = Client()
        self.collect(client)
        self.assertEqual(client.calls, [(7, 0)])
        self.assertIsNotNone(self.source())

    def test_cache_drops_provider_secrets_and_ignores_unrecognized_metadata(self):
        client = Client([item(_meta={"token": "private-value"}, version={"token": "private-value"})])
        self.collect(client)
        cache = dict(self.cache())
        self.assertEqual(len(cache["list_signature"]), 64)
        self.assertNotIn("private-value", json.dumps(cache))
        self.now += 300
        client = Client([item(_meta={"token": "different-private-value"})])
        self.collect(client)
        self.assertEqual(client.calls, [])

    def test_new_summary_after_throttle_preserves_existing_full_body(self):
        self.collect()
        previous = self.source()["content"]
        self.now += 300
        client = Client([item(title="Revised headline", summary="Revised short summary")],
                        {(7, 0): InvestatError("rate_limit", status_code=429)})
        self.collect(client)
        self.assertEqual(self.source()["content"], previous)
        self.assertEqual(self.source()["content_scope"], "provided_text")
        self.assertEqual(self.source()["title"], "Revised headline")


if __name__ == "__main__":
    unittest.main()
