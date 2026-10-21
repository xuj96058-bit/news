"""Event memory persistence uses temporary local databases and no network."""

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from src.event_store import EventStore, EventStoreError, MAX_LIST_LIMIT, MAX_SNAPSHOT_CHARS
from src.news_store import NewsStore


class EventStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "news.sqlite3"
        self.store = EventStore(self.db)
        self.event = self.store.create_event("公司訂單與交付進展")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def revision(self, *, event_id=None, digest="input-a", brief=None, metadata=None, **extra):
        return self.store.create_revision(
            event_id or self.event["id"], article_ids=extra.get("article_ids", ["article-a"]),
            sources=extra.get("sources", [{"id": "S1", "content": "訂單增長，交付尚未披露。"}]),
            brief=brief or {"summary": "需求改善仍需交付證據。"},
            metadata=metadata or {"review_status": "pending"}, input_hash=digest)

    def test_snapshots_survive_reopen_and_review_preserves_original(self):
        first = self.revision()
        reviewed = self.store.update_revision(first["id"],
            brief={"summary": "訂單改善；尚不能判斷盈利。"}, metadata={"review_status": "review_complete"})
        second = self.revision(digest="input-b", brief={"summary": "交付改善支持原判斷。"})
        with EventStore(self.db) as reopened:
            history = reopened.list_revisions(self.event["id"])
            self.assertEqual([row["id"] for row in history], [second["id"], first["id"]])
            self.assertEqual([row["version"] for row in history], [2, 1])
            saved = reopened.find_revision(self.event["id"], "input-a")
            self.assertEqual(saved["brief"], reviewed["brief"])
            self.assertEqual(saved["original_brief"], first["brief"])
            self.assertEqual(saved["original_metadata"], {"review_status": "pending"})
            self.assertEqual(saved["sources"], first["sources"])
            self.assertEqual(saved["article_ids"], ["article-a"])
            self.assertEqual(datetime.fromisoformat(saved["created_at"]).utcoffset(), timezone.utc.utcoffset(None))

    def test_same_input_is_idempotent_and_does_not_overwrite(self):
        first = self.revision()
        duplicate = self.revision(brief={"summary": "不應取代初稿"})
        self.assertEqual(duplicate, first)
        self.assertEqual(len(self.store.list_revisions(self.event["id"])), 1)
        other = self.store.create_event("另一家公司")
        separate = self.revision(event_id=other["id"])
        self.assertNotEqual(separate["id"], first["id"])
        self.assertEqual(separate["version"], 1)

    def test_shared_news_database_and_dynamic_default_path(self):
        with NewsStore(self.db) as news:
            news.ingest([{"source_id": "example", "source_name": "Example", "source_kind": "official",
                         "title": "原有新聞", "url": "https://example.test/news", "summary": "原文"}])
            before = news.export_articles()
        self.revision()
        with patch("src.news_store.DEFAULT_DB_PATH", self.db), EventStore() as default_store:
            self.assertEqual(default_store.get_event(self.event["id"])["title"], self.event["title"])
        with NewsStore(self.db) as news:
            self.assertEqual(news.export_articles(), before)

    def test_failed_event_update_rolls_back_revision_insert(self):
        before = self.store.get_event(self.event["id"])
        self.store.connection.executescript("""
            CREATE TRIGGER fail_event_write BEFORE UPDATE ON news_events
            BEGIN SELECT RAISE(ABORT, 'sensitive internal detail'); END;
        """)
        with self.assertRaises(EventStoreError) as caught:
            self.revision()
        self.assertNotIn("sensitive", str(caught.exception))
        self.assertEqual(self.store.list_revisions(self.event["id"]), [])
        self.assertEqual(self.store.get_event(self.event["id"]), before)
        self.store.connection.execute("DROP TRIGGER fail_event_write")
        self.assertEqual(self.revision()["version"], 1)

    def test_failed_review_update_retains_prior_brief(self):
        first = self.revision()
        self.store.connection.executescript("""
            CREATE TRIGGER fail_event_write BEFORE UPDATE ON news_events
            BEGIN SELECT RAISE(ABORT, 'sensitive internal detail'); END;
        """)
        with self.assertRaises(EventStoreError):
            self.store.update_revision(first["id"], brief={"summary": "新稿"}, metadata={})
        self.assertEqual(self.store.find_revision(self.event["id"], "input-a"), first)

    def test_invalid_event_and_revision_never_create_orphans(self):
        with self.assertRaises(EventStoreError):
            self.revision(event_id="missing")
        with self.assertRaises(EventStoreError):
            self.store.update_revision("missing", brief={}, metadata={})
        self.assertIsNone(self.store.get_event("missing"))
        self.assertIsNone(self.store.find_revision("missing", "input-a"))
        self.assertEqual(self.store.list_revisions("missing"), [])
        self.assertEqual(self.store.list_revisions(self.event["id"]), [])

    def test_limits_are_bounded_and_zero_is_empty(self):
        for number in range(MAX_LIST_LIMIT + 2):
            self.store.create_event(f"事件 {number}")
        self.assertEqual(len(self.store.list_events(limit=10_000)), MAX_LIST_LIMIT)
        self.assertEqual(self.store.list_events(limit=0), [])
        self.revision()
        self.assertEqual(self.store.list_revisions(self.event["id"], limit=0), [])
        for bad in (-1, True, "5", None):
            with self.subTest(limit=bad):
                with self.assertRaises(EventStoreError):
                    self.store.list_events(limit=bad)
                with self.assertRaises(EventStoreError):
                    self.store.list_revisions(self.event["id"], limit=bad)

    def test_snapshot_validation_and_credential_cleaning(self):
        metadata = {"review_status": "pending", "api_key": "synthetic-secret",
                    "nested": {"authorization": "Bearer sensitive", "usage": {"input_tokens": 30}}}
        sources = [{"id": "S1", "url": "https://example.test/source?token=private&report=one",
                    "content": "訂單改善", "headers": {"Authorization": "secret"}}]
        saved = self.revision(metadata=metadata, sources=sources, article_ids=["one", "one"])
        raw = json.dumps(saved)
        for secret in ("synthetic-secret", "Bearer sensitive", "token=private", "Authorization"):
            self.assertNotIn(secret, raw)
        self.assertEqual(saved["sources"][0]["url"], "https://example.test/source?report=one")
        self.assertEqual(saved["article_ids"], ["one"])
        self.assertEqual(saved["metadata"]["nested"]["usage"]["input_tokens"], 30)
        self.assertIn("api_key", metadata)  # Caller inputs stay untouched.
        for invalid in ({"bad": float("nan")}, {"bad": object()}, {"large": "x" * MAX_SNAPSHOT_CHARS}):
            with self.subTest(invalid_type=type(invalid[next(iter(invalid))])):
                with self.assertRaises(EventStoreError):
                    self.revision(digest="invalid", brief=invalid)
        self.assertIsNone(self.store.find_revision(self.event["id"], "invalid"))

    def test_read_errors_do_not_expose_database_details(self):
        self.store.connection.execute("DROP TABLE event_revisions")
        with self.assertRaises(EventStoreError) as caught:
            self.store.list_revisions(self.event["id"])
        self.assertNotIn("no such table", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
