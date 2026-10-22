"""Source boundaries and atomic notification delivery records, without network."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from src.news_store import NewsStore
from src.notifications import NotificationConfig, configure_route
from src.telegram_source import TelegramConfig, TelegramStore


INPUT_TOKEN = "123456789:abcdefghijklmnopqrstuvwxyzABCDE1234"
OUTPUT_TOKEN = "987654321:abcdefghijklmnopqrstuvwxyzABCDE1234"
CHANNEL = -1001234567890
DESTINATION = -1009876543210
NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)


def research(identifier="one", **overrides):
    article = {
        "source_id": "investat", "external_id": identifier,
        "source_name": "Investat / Example Research", "source_kind": "research",
        "title": "An outlook for rates", "summary": "The original summary.",
        "content": "The provided research text.", "content_scope": "provided_text",
        "published_at": NOW.isoformat(), "url": "",
    }
    article.update(overrides)
    return article


def channel_update(update_id, message_id=1, *, text="A news headline\nFull original body.",
                   chat_id=CHANNEL, published=NOW, edited=None):
    message = {
        "chat": {"id": chat_id, "type": "channel", "title": "News"},
        "message_id": message_id, "date": int(published.timestamp()), "text": text,
    }
    if edited is not None:
        message["edit_date"] = int(edited.timestamp())
    return {"update_id": update_id,
            "edited_channel_post" if edited is not None else "channel_post": message}


def queued(connection):
    return [dict(row) for row in connection.execute(
        "SELECT * FROM notification_outbox ORDER BY source,source_key").fetchall()]


def payload(connection):
    return json.loads(queued(connection)[0]["payload_json"])


class InvestatNotificationSourceTests(unittest.TestCase):
    def setUp(self):
        self.store = NewsStore(":memory:")
        self.addCleanup(self.store.close)
        self.config = NotificationConfig(OUTPUT_TOKEN, DESTINATION)

    def activate(self):
        configure_route(self.store.connection, "investat", self.config)

    def test_initial_baseline_and_later_baseline_edits_stay_silent(self):
        self.store.ingest([research("old")])
        self.assertEqual(queued(self.store.connection), [])
        self.activate()
        self.store.ingest([research("old", content="A changed old article.")])
        self.assertEqual(queued(self.store.connection), [])
        self.store.ingest([research("new")])
        rows = queued(self.store.connection)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["source"], rows[0]["source_key"]), ("investat", "new"))

    def test_repeated_fetch_uses_one_stable_provider_record(self):
        self.activate()
        self.store.ingest([research("stable", url="https://research.example/first")])
        self.store.ingest([research("stable", url="https://research.example/first")])
        self.assertEqual(len(queued(self.store.connection)), 1)
        self.assertEqual(payload(self.store.connection)["text"], "The provided research text.")
        self.store.ingest([research("stable", content="New supplied text.")])
        self.assertEqual(len(queued(self.store.connection)), 1)
        self.assertEqual(payload(self.store.connection)["text"], "New supplied text.")

    def test_rss_and_other_sources_never_queue(self):
        self.activate()
        self.store.ingest([
            research(source_id="cnbc_top", url="https://news.example/rss"),
            research(source_id="sec", title="A filing", url="https://sec.example/filing"),
        ])
        self.assertEqual(queued(self.store.connection), [])
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0], 2)

    def test_merged_rss_metadata_cannot_trigger_or_replace_investat_delivery(self):
        self.activate()
        self.store.ingest([research()])
        initial = payload(self.store.connection)
        # RSS may share a fingerprint with an existing Investat article.
        rss = research(source_id="rss", url="https://news.example/syndicated")
        self.store.ingest([rss])
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0], 1)
        rss["content"] = "An RSS-only update to the syndicated story."
        self.store.ingest([rss])
        current = json.loads(self.store.connection.execute("SELECT metadata_json FROM articles").fetchone()[0])
        self.assertEqual(current["source_id"], "rss")
        self.assertEqual(payload(self.store.connection), initial)
        # The next Investat revision comes from the provider source record.
        self.store.ingest([research(content="The actual Investat revision.")])
        self.assertEqual(payload(self.store.connection)["text"], "The actual Investat revision.")

    def test_partial_scope_and_richer_body_are_preserved(self):
        self.activate()
        self.store.ingest([research(content="First part", content_scope="partial_text")])
        self.assertEqual(payload(self.store.connection)["content_scope"], "partial_text")
        self.store.ingest([research(content="First part plus the rest", content_scope="provided_text")])
        self.store.ingest([research(content="First part", content_scope="partial_text")])
        self.assertEqual(payload(self.store.connection)["text"], "First part plus the rest")
        self.assertEqual(payload(self.store.connection)["content_scope"], "provided_text")

    def test_failed_ingest_rolls_back_both_articles_and_notifications(self):
        self.activate()
        with self.assertRaises(ValueError):
            self.store.ingest([research(), research("bad", title="")])
        self.assertEqual(queued(self.store.connection), [])
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0], 0)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM article_sources").fetchone()[0], 0)


class TelegramNotificationSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = TelegramStore(Path(self.temp.name) / "telegram.sqlite3")
        self.addCleanup(self.store.connection.close)
        self.input_config = TelegramConfig(INPUT_TOKEN, CHANNEL)
        self.output_config = NotificationConfig(OUTPUT_TOKEN, DESTINATION)
        self.owner = "test-consumer"
        self.assertTrue(self.store.claim(self.input_config, self.owner))

    def activate(self):
        configure_route(self.store.connection, "telegram", self.output_config)

    def apply(self, updates):
        return self.store.apply_batch(updates, self.input_config, self.owner, NOW)

    def test_only_input_channel_is_queued_and_duplicates_do_not_repeat(self):
        self.activate()
        result = self.apply([channel_update(1), channel_update(2, chat_id=DESTINATION),
                             channel_update(3, chat_id=-1001111111111)])
        self.assertEqual((result["added"], result["ignored"]), (1, 2))
        self.apply([channel_update(4)])
        rows = queued(self.store.connection)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["source"], rows[0]["source_key"]), ("telegram", f"{CHANNEL}:1"))
        self.assertEqual(payload(self.store.connection)["text"], "A news headline\nFull original body.")
        self.assertEqual(self.store.get_status()["next_update_id"], 5)

    def test_baseline_edits_do_not_publish_old_messages(self):
        self.apply([channel_update(1)])
        self.activate()
        self.apply([channel_update(2, text="An old message corrected", edited=NOW)])
        self.assertEqual(queued(self.store.connection), [])
        self.apply([channel_update(3, 2)])
        self.assertEqual(len(queued(self.store.connection)), 1)
        self.assertEqual(queued(self.store.connection)[0]["source_key"], f"{CHANNEL}:2")

    def test_edits_preserve_publication_and_ignore_stale_or_timestamp_only_updates(self):
        self.activate()
        original = NOW - timedelta(hours=1)
        self.apply([channel_update(1, published=original)])
        first_payload = payload(self.store.connection)
        self.apply([channel_update(2, published=original, edited=NOW)])
        self.assertEqual(payload(self.store.connection), first_payload)
        self.apply([channel_update(3, published=original, text="Corrected text", edited=NOW)])
        self.assertEqual(payload(self.store.connection)["text"], "Corrected text")
        self.assertEqual(payload(self.store.connection)["published_at"], original.isoformat())
        self.apply([channel_update(4, published=original, text="Stale text", edited=NOW - timedelta(minutes=1))])
        self.assertEqual(payload(self.store.connection)["text"], "Corrected text")

    def test_failed_batch_rolls_back_posts_cursor_and_notifications(self):
        self.activate()
        self.store.connection.execute("""CREATE TRIGGER reject_second BEFORE INSERT ON telegram_posts
            WHEN NEW.message_id=2 BEGIN SELECT RAISE(ABORT, 'forced failure'); END""")
        self.store.connection.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.apply([channel_update(1), channel_update(2, 2)])
        self.assertEqual(queued(self.store.connection), [])
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM telegram_posts").fetchone()[0], 0)
        self.assertEqual(self.store.get_status()["next_update_id"], 0)

    def test_inbox_retention_scrubs_queued_telegram_body(self):
        self.activate()
        self.apply([channel_update(1)])
        self.assertIn("Full original body.", queued(self.store.connection)[0]["payload_json"])
        self.assertEqual(self.store.prune(NOW + timedelta(days=7)), 1)
        row = queued(self.store.connection)[0]
        self.assertEqual(row["state"], "expired")
        self.assertEqual(row["payload_json"], "{}")


if __name__ == "__main__":
    unittest.main()
