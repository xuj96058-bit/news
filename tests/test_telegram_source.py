from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests

from src.telegram_source import (TelegramClient, TelegramConfig, TelegramError,
                                 TelegramStore, load_config, run_worker, sync_once)


TOKEN = "123456789:abcdefghijklmnopqrstuvwxyzABCDE1234"
CHANNEL = -1001234567890
NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)


def post(update_id, message_id=1, text="News text", when=NOW, chat_id=CHANNEL, edited=None, **fields):
    message = {"message_id": message_id, "date": int(when.timestamp()),
               "chat": {"id": chat_id, "type": "channel", "title": "News"}, "text": text}
    message.update(fields)
    if edited:
        message["edit_date"] = int(edited.timestamp())
    return {"update_id": update_id, "edited_channel_post" if edited else "channel_post": message}


class FakeClient:
    def __init__(self, *batches, webhook=False, error=None):
        self.batches = list(batches)
        self.offsets = []
        self.webhook = webhook
        self.error = error

    def has_webhook(self):
        return self.webhook

    def get_updates(self, offset):
        self.offsets.append(offset)
        if self.error:
            raise self.error
        return self.batches.pop(0) if self.batches else []


class TelegramSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "telegram.sqlite3"
        self.config = TelegramConfig(TOKEN, CHANNEL)

    def sync(self, client, now=NOW, **kwargs):
        return sync_once(self.config, self.path, now=now, client=client, **kwargs)

    def rows(self, now=NOW, **kwargs):
        with TelegramStore(self.path) as store:
            return store.list_posts(now=now, **kwargs)

    def status(self):
        with TelegramStore(self.path) as store:
            return store.get_status()

    def test_target_channel_only_and_offset_survives_restart(self):
        result = self.sync(FakeClient([post(10), post(11, 2, chat_id=-1009999999999), {"update_id": 12, "message": {"text": "private"}}]))
        self.assertEqual((result["added"], result["ignored"]), (1, 2))
        self.assertEqual(self.status()["next_update_id"], 13)
        client = FakeClient([])
        self.sync(client)
        self.assertEqual(client.offsets, [13])
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["url"], "https://t.me/c/1234567890/1")

    def test_duplicate_and_stale_edit_do_not_replace_newer_text(self):
        published = NOW - timedelta(hours=1)
        self.sync(FakeClient([post(10, when=published)]))
        self.sync(FakeClient([post(11, text="new", when=published, edited=NOW)]))
        result = self.sync(FakeClient([post(12, text="old", when=published, edited=NOW - timedelta(minutes=1)), post(13, when=published)]))
        self.assertEqual(result["updated"], 0)
        item = self.rows()[0]
        self.assertEqual(item["text"], "new")
        self.assertEqual(item["published_at"], published.isoformat())
        self.assertEqual(item["edited_at"], NOW.isoformat())

    def test_retention_exact_boundary_and_edit_does_not_extend(self):
        original = NOW - timedelta(days=6, hours=23, minutes=59)
        self.sync(FakeClient([post(1, when=original)]))
        self.sync(FakeClient([post(2, text="edited", when=original, edited=NOW)]))
        self.assertEqual(self.rows()[0]["text"], "edited")
        self.assertEqual(len(self.rows(now=NOW + timedelta(seconds=59))), 1)
        self.assertEqual(self.rows(now=NOW + timedelta(minutes=1)), [])
        # A late edit does not resurrect an expired original message.
        self.sync(FakeClient([post(3, text="late edit", when=original, edited=NOW + timedelta(minutes=2))]), now=NOW + timedelta(minutes=2))
        self.assertEqual(self.rows(now=NOW + timedelta(minutes=2)), [])

    def test_stale_pending_updates_are_acknowledged_without_being_saved(self):
        result = self.sync(FakeClient([post(5, when=NOW - timedelta(days=7)), post(6, 2, when=NOW - timedelta(days=8))]))
        self.assertEqual((result["added"], result["ignored"]), (0, 2))
        self.assertEqual(self.status()["next_update_id"], 7)

    def test_prune_runs_even_when_network_fails(self):
        self.sync(FakeClient([post(1, when=NOW - timedelta(days=6, hours=23))]))
        result = self.sync(FakeClient(error=RuntimeError("https://api.telegram.org/bot" + TOKEN)), now=NOW + timedelta(hours=1))
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["pruned"], 1)
        self.assertNotIn(TOKEN, str(result))
        self.assertNotIn(TOKEN, str(self.status()))

    def test_invalid_batch_rolls_back_rows_and_cursor(self):
        result = self.sync(FakeClient([post(1), {"update_id": "bad"}]))
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.status()["next_update_id"], 0)

    def test_failure_during_batch_save_is_atomic(self):
        with TelegramStore(self.path) as store:
            store.connection.execute("""CREATE TRIGGER reject_second BEFORE INSERT ON telegram_posts WHEN NEW.message_id=2
                BEGIN SELECT RAISE(ABORT,'forced failure'); END""")
            store.connection.commit()
        result = self.sync(FakeClient([post(1, 1), post(2, 2)]))
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.status()["next_update_id"], 0)
        self.assertEqual(self.rows(), [])

    def test_full_batch_drains_and_later_failure_preserves_committed_cursor(self):
        batch = [post(i, i + 1) for i in range(100)]
        class FailingSecond(FakeClient):
            def get_updates(self, offset):
                if self.offsets:
                    raise RuntimeError("network failed")
                return super().get_updates(offset)
        client = FailingSecond(batch)
        self.assertEqual(self.sync(client)["status"], "error")
        self.assertEqual(self.status()["next_update_id"], 100)
        self.assertEqual(len(self.rows()), 100)
        client = FakeClient([post(100, 101)])
        self.assertEqual(self.sync(client)["added"], 1)
        self.assertEqual(client.offsets, [100])

    def test_batch_cap_reports_backlog(self):
        result = self.sync(FakeClient([post(i, i + 1) for i in range(100)]), max_batches=1)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(self.status()["next_update_id"], 100)

    def test_active_lease_blocks_another_receiver(self):
        with TelegramStore(self.path) as store:
            self.assertTrue(store.claim(self.config, "other"))
        client = FakeClient([post(1)])
        self.assertEqual(self.sync(client)["status"], "busy")
        self.assertEqual(client.offsets, [])

    def test_expired_lease_can_be_reclaimed_but_old_owner_cannot_save(self):
        with TelegramStore(self.path) as store:
            self.assertTrue(store.claim(self.config, "old", now=datetime.now(timezone.utc) - timedelta(minutes=3)))
            with TelegramStore(self.path) as other:
                self.assertTrue(other.claim(self.config, "new"))
            with self.assertRaises(TelegramError):
                store.apply_batch([post(1)], self.config, "old", NOW)
        self.assertEqual(self.rows(), [])

    def test_webhook_is_detected_without_polling_or_deleting(self):
        client = FakeClient(webhook=True)
        result = self.sync(client)
        self.assertEqual(result["status"], "error")
        self.assertIn("Webhook", result["error"])
        self.assertEqual(client.offsets, [])

    def test_caption_and_media_only_and_service_messages(self):
        result = self.sync(FakeClient([post(1, text=None, caption="Caption", photo=[{}]), post(2, 2, text=None, photo=[{}]), post(3, 3, text=None)]))
        self.assertEqual(result["added"], 2)
        self.assertEqual(self.rows()[0]["media_type"], "photo")
        self.assertEqual(self.rows()[1]["text"], "Caption")

    def test_local_day_filter_and_channel_filter(self):
        before = datetime(2026, 10, 21, 15, 59, tzinfo=timezone.utc)
        after = datetime(2026, 10, 21, 16, 0, tzinfo=timezone.utc)
        self.sync(FakeClient([post(1, 1, when=before), post(2, 2, when=after)]))
        self.assertEqual(len(self.rows(day=date(2026, 10, 22))), 1)
        self.assertEqual(len(self.rows(day=date(2026, 10, 21))), 1)
        self.assertEqual(self.rows(chat_id=-1009999999999), [])
        with TelegramStore(self.path) as store:
            self.assertEqual(store.count_posts(now=NOW, day=date(2026, 10, 22), chat_id=CHANNEL), 1)

    def test_week_keeps_previous_three_day_boundary_and_older_local_date(self):
        self.sync(FakeClient([post(1, 1, when=NOW - timedelta(days=3)),
                              post(2, 2, when=NOW - timedelta(days=6))]))
        self.assertEqual([row["message_id"] for row in self.rows()], [1, 2])
        self.assertEqual([row["message_id"] for row in self.rows(day=date(2026, 10, 16))], [2])

    def test_search_is_case_insensitive_literal_text_and_combines_with_date(self):
        self.sync(FakeClient([post(1, 1, text=r"Revenue 25%_growth\annual Alpha"),
                              post(2, 2, text="Revenue 25xZgrowth annual ALPHA"),
                              post(3, 3, text="Alpha", when=NOW - timedelta(days=1))]))
        self.assertEqual([row["message_id"] for row in self.rows(query=r"25%_growth\annual")], [1])
        self.assertEqual([row["message_id"] for row in self.rows(query="aLpHa")], [2, 1, 3])
        self.assertEqual(self.rows(query="News"), [])  # Channel title is not article text.
        self.assertEqual(self.rows(query="' OR 1=1 --"), [])
        with TelegramStore(self.path) as store:
            self.assertEqual(store.count_posts(now=NOW, query="alpha", day=date(2026, 10, 22), chat_id=CHANNEL), 2)
            self.assertEqual(store.count_posts(now=NOW, query="alpha", chat_id=-1009999999999), 0)

    def test_anchor_stabilizes_pagination_and_counts_new_matching_posts(self):
        self.sync(FakeClient([post(1, 1, text="Alpha"), post(2, 2, text="Alpha"),
                              post(3, 3, text="Beta", when=NOW - timedelta(minutes=1))]))
        anchor = (NOW.timestamp(), 2)
        before_rows = self.rows(before=anchor, query="alpha")
        self.sync(FakeClient([post(4, 4, text="Alpha"),
                              post(5, 5, text="Alpha", when=NOW + timedelta(seconds=1))]))
        self.assertEqual(self.rows(before=anchor, query="alpha"), before_rows)
        self.assertEqual([row["message_id"] for row in self.rows(before=anchor, query="alpha", limit=1, offset=1)], [1])
        with TelegramStore(self.path) as store:
            self.assertEqual(store.count_posts(now=NOW, query="alpha", before=anchor), 2)
            self.assertEqual(store.count_posts(now=NOW, query="alpha"), 4)
            for invalid in ((float("nan"), 2), (NOW.timestamp(), True), (NOW.timestamp(), -1), (NOW.timestamp(),)):
                with self.subTest(anchor=invalid), self.assertRaises(ValueError):
                    store.list_posts(now=NOW, before=invalid)

    def test_config_errors_and_repr_never_contain_token(self):
        self.assertNotIn(TOKEN, repr(self.config))
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "not-a-number"}):
            with self.assertRaises(ValueError) as raised:
                load_config()
        self.assertNotIn(TOKEN, str(raised.exception))

    def test_token_rotation_keeps_cursor_and_new_bot_resets_it(self):
        self.sync(FakeClient([post(50)]))
        rotated = TelegramConfig("123456789:zyxwvutsrqponmlkjihgfedcbaABCDE1234", CHANNEL)
        client = FakeClient([])
        sync_once(rotated, self.path, now=NOW, client=client)
        self.assertEqual(client.offsets, [51])
        new_bot = TelegramConfig("987654321:zyxwvutsrqponmlkjihgfedcbaABCDE1234", CHANNEL)
        client = FakeClient([])
        sync_once(new_bot, self.path, now=NOW, client=client)
        self.assertEqual(client.offsets, [0])

    def test_secure_delete_overwrites_expired_text(self):
        marker = "EXPIRED_PRIVATE_BODY_very_specific_marker"
        self.sync(FakeClient([post(1, text=marker)]))
        self.assertIn(marker.encode(), self.path.read_bytes())
        self.assertEqual(self.rows(now=NOW + timedelta(days=7)), [])
        self.assertNotIn(marker.encode(), self.path.read_bytes())

    def test_transport_redacts_url_and_does_not_follow_redirects(self):
        client = TelegramClient(self.config)
        with patch("src.telegram_source.requests.post", side_effect=requests.ConnectionError("failed " + TOKEN)) as mocked:
            with self.assertRaises(TelegramError) as caught:
                client.get_updates(0)
            self.assertNotIn(TOKEN, str(caught.exception))
            self.assertFalse(mocked.call_args.kwargs["allow_redirects"])
            self.assertEqual(mocked.call_args.kwargs["timeout"], (5, 35))

    def test_409_safe_message(self):
        class Response:
            status_code = 409
            def json(self):
                return {"ok": False, "error_code": 409, "description": "unsafe " + TOKEN}
        with patch("src.telegram_source.requests.post", return_value=Response()):
            with self.assertRaises(TelegramError) as caught:
                TelegramClient(self.config).get_updates(0)
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertIn("其他接收程式", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
