from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import requests

from src.notifications import (
    DeliveryError, NotificationConfig, TelegramSender, configure_route,
    enqueue_notification, format_notification, has_route, initialize_notifications,
    load_notification_config, notification_status, process_pending, prune_notifications,
)


TOKEN = "123456789:abcdefghijklmnopqrstuvwxyzABCDE1234"
CHANNEL = -1001234567899


def payload(**updates):
    value = {"title": "消息標題", "text": "完整內容", "summary": "原有摘要",
             "published_at": datetime.now(timezone.utc).isoformat(),
             "url": "https://example.test/news", "content_scope": "provided_text"}
    value.update(updates)
    return value


class FakeSender:
    def __init__(self, error=None, callback=None):
        self.error = error
        self.callback = callback
        self.calls = []

    def deliver(self, source, data, **kwargs):
        self.calls.append((source, data, kwargs))
        if self.callback:
            self.callback()
        if self.error:
            raise self.error
        return {"message_id": kwargs.get("message_id") or len(self.calls),
                "kind": "text"}


class NotificationsTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        initialize_notifications(self.connection)
        self.config = NotificationConfig(TOKEN, CHANNEL)

    def activate(self, source="investat"):
        configure_route(self.connection, source, self.config)

    def add(self, key="1", source="investat", is_new=True, **updates):
        with self.connection:
            return enqueue_notification(self.connection, source, key, payload(**updates), is_new=is_new)

    def row(self):
        return self.connection.execute("SELECT * FROM notification_outbox ORDER BY id LIMIT 1").fetchone()

    def test_configuration_has_no_receiver_token_fallback(self):
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": TOKEN}, clear=True):
            self.assertIsNone(load_notification_config())
        with patch.dict("os.environ", {"TELEGRAM_NOTIFY_BOT_TOKEN": TOKEN}, clear=True):
            with self.assertRaises(ValueError):
                load_notification_config()
        with patch.dict("os.environ", {"TELEGRAM_NOTIFY_BOT_TOKEN": TOKEN,
                                      "TELEGRAM_NOTIFY_CHAT_ID": str(CHANNEL),
                                      "TELEGRAM_CHAT_ID": str(CHANNEL)}, clear=True):
            with self.assertRaises(ValueError):
                load_notification_config()
        self.assertNotIn(TOKEN, repr(self.config))

    def test_only_whitelisted_sources_can_activate_or_enqueue(self):
        with self.assertRaises(ValueError):
            self.activate("rss")
        with self.assertRaises(ValueError):
            self.add(source="rss")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 0)

    def test_inactive_route_and_baseline_edits_are_silent(self):
        self.assertFalse(self.add())
        self.activate()
        self.assertTrue(has_route(self.connection, "investat", self.config))
        self.assertFalse(self.add(is_new=False))
        self.assertIsNone(self.row())
        self.assertTrue(self.add(key="new"))
        self.assertTrue(self.add(key="new", is_new=False, text="changed"))
        self.assertEqual(self.row()["revision"], 2)

    def test_enqueue_rolls_back_with_source_transaction(self):
        self.activate()
        with self.assertRaises(RuntimeError), self.connection:
            enqueue_notification(self.connection, "investat", "1", payload(), is_new=True)
            raise RuntimeError("source save failed")
        self.assertIsNone(self.row())

    def test_duplicates_and_timestamp_only_edits_do_not_requeue(self):
        self.activate()
        self.add()
        client = FakeSender()
        self.assertEqual(process_pending(self.connection, self.config, client)["sent"], 1)
        self.assertFalse(self.add(is_new=False, published_at="2030-01-01T00:00:00Z"))
        process_pending(self.connection, self.config, client)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.row()["state"], "sent")

    def test_source_change_edits_previously_sent_message(self):
        self.activate()
        self.add()
        client = FakeSender()
        process_pending(self.connection, self.config, client)
        self.add(is_new=False, text="修訂正文")
        process_pending(self.connection, self.config, client)
        self.assertEqual(client.calls[-1][2], {"message_id": 1, "previous_kind": "text"})
        self.assertEqual(client.calls[-1][1]["text"], "修訂正文")

    def test_same_route_configuration_does_not_clear_dispatch_lease(self):
        self.activate()
        with self.connection:
            self.connection.execute("UPDATE notification_routes SET dispatch_owner='busy',dispatch_until=?", (time.time() + 60,))
        self.activate()
        self.add()
        client = FakeSender()
        process_pending(self.connection, self.config, client)
        self.assertEqual(client.calls, [])

    def test_destination_switch_disables_previous_queue(self):
        self.activate()
        self.add()
        other = NotificationConfig(TOKEN, CHANNEL - 1)
        configure_route(self.connection, "investat", other)
        client = FakeSender()
        process_pending(self.connection, self.config, client)
        self.assertEqual(client.calls, [])
        self.assertFalse(has_route(self.connection, "investat", self.config))

    def test_retry_after_429_is_persisted_and_does_not_loop(self):
        self.activate()
        self.add()
        client = FakeSender(DeliveryError("rate_limit", retry_after=125))
        before = time.time()
        result = process_pending(self.connection, self.config, client, limit=10)
        self.assertEqual(result["retry"], 1)
        self.assertEqual(len(client.calls), 1)
        self.assertGreaterEqual(self.row()["next_attempt_at"], before + 125)
        client.error = None
        with self.connection:
            self.connection.execute("UPDATE notification_outbox SET next_attempt_at=0")
        process_pending(self.connection, self.config, client)
        self.assertEqual(self.row()["state"], "sent")

    def test_ambiguous_send_is_held_not_blindly_retried(self):
        self.activate()
        self.add()
        client = FakeSender(DeliveryError("uncertain", uncertain=True))
        self.assertEqual(process_pending(self.connection, self.config, client)["uncertain"], 1)
        self.add(is_new=False, text="new revision")
        process_pending(self.connection, self.config, client)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.row()["state"], "uncertain")

    def test_abandoned_send_lease_becomes_uncertain(self):
        self.activate()
        self.add()
        with self.connection:
            self.connection.execute("UPDATE notification_outbox SET state='sending',lease_owner='dead',lease_until=1")
        client = FakeSender()
        process_pending(self.connection, self.config, client)
        self.assertEqual(self.row()["state"], "uncertain")
        self.assertEqual(client.calls, [])

    def test_revision_arriving_during_send_is_not_lost(self):
        self.activate()
        self.add()
        client = FakeSender(callback=lambda: self.add(is_new=False, text="new during send"))
        process_pending(self.connection, self.config, client, limit=1)
        self.assertEqual(self.row()["state"], "pending")
        self.assertEqual(self.row()["sent_message_id"], 1)
        client.callback = None
        process_pending(self.connection, self.config, client, limit=1)
        self.assertEqual(client.calls[-1][1]["text"], "new during send")
        self.assertEqual(self.row()["state"], "sent")

    def test_preparation_outside_transaction_cached_across_retry(self):
        self.activate()
        self.add()
        calls = []
        def prepare(data):
            self.assertFalse(self.connection.in_transaction)
            calls.append(data)
            return {**data, "text": "繁體內容"}
        client = FakeSender(DeliveryError("permission", retry_after=30))
        process_pending(self.connection, self.config, client, prepare_payload=prepare)
        with self.connection:
            self.connection.execute("UPDATE notification_outbox SET next_attempt_at=0")
        client.error = None
        process_pending(self.connection, self.config, client, prepare_payload=prepare)
        self.assertEqual(len(calls), 1)
        self.assertEqual(client.calls[-1][1]["text"], "繁體內容")
        self.assertEqual(json.loads(self.row()["payload_json"])["text"], "完整內容")

    def test_preparation_failure_retries_without_sending_or_exposing_exception(self):
        self.activate()
        self.add()
        def prepare(data):
            raise RuntimeError("sensitive-token")
        client = FakeSender()
        process_pending(self.connection, self.config, client, prepare_payload=prepare)
        self.assertEqual(self.row()["state"], "retry")
        self.assertNotIn("sensitive-token", self.row()["last_error"])
        self.assertEqual(client.calls, [])

    def test_telegram_expiry_scrubs_pending_and_prepared_content(self):
        self.activate("telegram")
        original = datetime.now(timezone.utc) - timedelta(days=6)
        self.add(source="telegram", published_at=original.isoformat())
        with self.connection:
            self.connection.execute("UPDATE notification_outbox SET prepared_json='sensitive text'")
        self.assertEqual(prune_notifications(self.connection, original + timedelta(days=7)), 1)
        row = self.row()
        self.assertEqual(row["state"], "expired")
        self.assertEqual(row["payload_json"], "{}")
        self.assertIsNone(row["prepared_json"])
        self.assertTrue(row["payload_hash"])
        self.assertFalse(self.add(source="telegram", is_new=False, text="edited old story"))

    def test_investat_not_affected_by_telegram_retention(self):
        self.activate()
        self.add(published_at="2000-01-01T00:00:00Z")
        self.assertEqual(prune_notifications(self.connection), 0)
        self.assertEqual(notification_status(self.connection, self.config)["pending"], 1)

    def test_shared_sender_cooldown_blocks_other_source_queue(self):
        self.activate()
        self.add()
        other = sqlite3.connect(":memory:")
        self.addCleanup(other.close)
        initialize_notifications(other)
        configure_route(other, "telegram", self.config)
        with other:
            enqueue_notification(other, "telegram", "channel:1", payload(), is_new=True)
        session = Mock()
        response = Mock(status_code=429)
        response.json.return_value = {"ok": False, "error_code": 429, "parameters": {"retry_after": 300}}
        session.post.return_value = response
        sender = TelegramSender(self.config, session=session, min_interval=0)
        process_pending(self.connection, self.config, sender)
        process_pending(other, self.config, sender)
        self.assertEqual(session.post.call_count, 1)
        self.assertGreater(sender.cooldown_remaining, 290)
        self.assertEqual(notification_status(other, self.config)["pending"], 1)


class FormattingTests(unittest.TestCase):
    def test_single_line_news_is_not_duplicated(self):
        content = format_notification("telegram", payload(title="同一行新聞", text="同一行新聞", summary=""))
        self.assertEqual(content["text"].count("同一行新聞"), 1)

    def test_short_text_has_full_body_and_plain_text_format(self):
        content = format_notification("telegram", payload(text="完整正文 & <tag> [literal]"))
        self.assertEqual(content["kind"], "text")
        self.assertIn("完整正文 & <tag> [literal]", content["text"])
        self.assertIn("香港時間", content["text"])

    def test_long_text_is_utf8_file_and_caption_is_bounded(self):
        text = "完整中文 😀 " * 1000
        content = format_notification("investat", payload(text=text, title="標題 / 無效:檔名", summary="摘要😀" * 1000))
        self.assertEqual(content["kind"], "document")
        self.assertIn(text.strip(), content["document"].decode("utf-8"))
        self.assertLessEqual(len(content["caption"].encode("utf-16-le")) // 2, 1024)
        self.assertNotIn("/", content["filename"])
        self.assertNotIn(":", content["filename"])

    def test_astral_characters_count_towards_threshold(self):
        content = format_notification("investat", payload(text="😀" * 1600))
        self.assertEqual(content["kind"], "document")

    def test_investat_markdown_is_stripped(self):
        text = "### 重點\n**加息** 預期 __升溫__\n- 第一點\n* 第二點\n`代碼` [來源](https://x.test/a)"
        content = format_notification("investat", payload(title="## **標題**", text=text, summary=""))
        body = content.get("text") or content["document"].decode("utf-8")
        for token in ("**", "__", "###", "`", "](", "* "):
            self.assertNotIn(token, body)
        self.assertIn("重點\n加息 預期 升溫\n- 第一點\n- 第二點\n代碼 來源", body)
        self.assertIn("標題", body)

    def test_over_250_chars_becomes_document(self):
        content = format_notification("investat", payload(text="字" * 260))
        self.assertEqual(content["kind"], "document")
        self.assertLessEqual(len(content["caption"]), 1024)

    def test_short_text_under_250_stays_text(self):
        content = format_notification("investat", payload(text="短訊" * 20))
        self.assertEqual(content["kind"], "text")

    def test_telegram_url_never_visible(self):
        for kind_text in ("短", "長" * 400):
            content = format_notification("telegram", payload(text=kind_text, url="https://t.me/c/1234567890/7"))
            visible = content.get("text", "") + content.get("caption", "") + content.get("document", b"").decode("utf-8")
            self.assertNotIn("t.me", visible)

    def test_text_link_rendered_as_escaped_anchor(self):
        text = "美股 <急跌> & 😀來源 綜述"
        # UTF-16 offsets: the emoji counts as 2 units.
        start = len("美股 <急跌> & 😀".encode("utf-16-le")) // 2
        links = [[start, 2, "https://example.test/a?x=1&y=\"2\""], [start + 3, 2, "https://example.test/b"]]
        content = format_notification("telegram", payload(title="頻道", text=text, links=links, url="https://t.me/c/1/2"))
        self.assertEqual(content["kind"], "text")
        self.assertEqual(content["parse_mode"], "HTML")
        self.assertIn('😀<a href="https://example.test/a?x=1&amp;y=&quot;2&quot;">來源</a> <a href="https://example.test/b">綜述</a>', content["text"])
        self.assertIn("&lt;急跌&gt; &amp;", content["text"])
        self.assertNotIn("t.me", content["text"])

    def test_long_linked_post_is_plain_document_with_html_caption(self):
        text = "來源" + "<字>" * 200
        content = format_notification("telegram", payload(title="頻道" * 400, text=text, links=[[0, 2, "https://example.test/a"]]))
        self.assertEqual(content["kind"], "document")
        self.assertNotIn("<a ", content["document"].decode("utf-8"))
        self.assertIn("<字>", content["document"].decode("utf-8"))
        self.assertEqual(content["parse_mode"], "HTML")
        self.assertLessEqual(len(content["caption"].encode("utf-16-le")) // 2, 1024)
        self.assertNotRegex(content["caption"], r"&[a-z]*$")

    def test_language_conversion_keeps_link_targets(self):
        from src.notification_language import prepare_payload
        prepared = format_notification("telegram", prepare_payload(
            {**payload(title="t", text="来源报道", url=""), "segments": [["来源", "https://example.test/简"], ["报道", None]]}))
        self.assertIn('<a href="https://example.test/简">來源</a>報道', prepared["text"])

    def test_partial_and_missing_time_are_honestly_labelled(self):
        result = format_notification("investat", payload(content_scope="partial_text", published_at=""))
        self.assertIn("部分正文", result["text"])
        self.assertIn("發布時間未提供", result["text"])

    def test_unsafe_url_not_emitted(self):
        result = format_notification("investat", payload(url="https://username:secret@example.test/a"))
        self.assertNotIn("secret", result["text"])


class SenderTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.sender = TelegramSender(NotificationConfig(TOKEN, CHANNEL), session=self.session, min_interval=0)

    def response(self, data, code=200):
        result = Mock(status_code=code)
        result.json.return_value = data
        self.session.post.return_value = result
        return result

    def test_send_text_uses_configured_destination_and_no_markup(self):
        response = self.response({"ok": True, "result": {"message_id": 12}})
        self.assertEqual(self.sender.deliver("investat", payload()), {"message_id": 12, "kind": "text"})
        kwargs = self.session.post.call_args.kwargs
        self.assertEqual(kwargs["json"]["chat_id"], CHANNEL)
        self.assertNotIn("parse_mode", kwargs["json"])
        self.assertFalse(kwargs["allow_redirects"])
        response.close.assert_called_once()

    def test_linked_telegram_post_sent_with_html_parse_mode(self):
        self.response({"ok": True, "result": {"message_id": 15}})
        self.sender.deliver("telegram", payload(text="來源", links=[[0, 2, "https://example.test/a"]]))
        self.assertEqual(self.session.post.call_args.kwargs["json"]["parse_mode"], "HTML")

    def test_document_is_uploaded_with_utf8_attachment(self):
        self.response({"ok": True, "result": {"message_id": 13}})
        result = self.sender.deliver("investat", payload(text="文" * 5000))
        self.assertEqual(result["kind"], "document")
        kwargs = self.session.post.call_args.kwargs
        self.assertIsInstance(kwargs["files"]["document"][1], bytes)
        self.assertEqual(kwargs["data"]["chat_id"], CHANNEL)

    def test_edit_document_remains_document_after_short_revision(self):
        self.response({"ok": True, "result": {"message_id": 13}})
        result = self.sender.deliver("investat", payload(), message_id=13, previous_kind="document")
        self.assertEqual(result["kind"], "document")
        self.assertTrue(self.session.post.call_args.args[0].endswith("/editMessageMedia"))

    def test_text_to_document_is_marked_update_and_replies_to_original(self):
        self.response({"ok": True, "result": {"message_id": 14}})
        self.sender.deliver("investat", payload(text="文" * 5000), message_id=13, previous_kind="text")
        kwargs = self.session.post.call_args.kwargs
        self.assertIn("【更新】", kwargs["data"]["caption"])
        self.assertEqual(json.loads(kwargs["data"]["reply_parameters"])["message_id"], 13)

    def test_known_rejection_and_retry_after_are_safe(self):
        self.response({"ok": False, "error_code": 429, "parameters": {"retry_after": 91}, "description": TOKEN}, 429)
        with self.assertRaises(DeliveryError) as error:
            self.sender.deliver("investat", payload())
        self.assertEqual(error.exception.retry_after, 91)
        self.assertFalse(error.exception.uncertain)
        self.assertNotIn(TOKEN, str(error.exception))

    def test_connection_timeout_safe_but_read_timeout_ambiguous(self):
        self.session.post.side_effect = requests.ConnectTimeout(TOKEN)
        with self.assertRaises(DeliveryError) as error:
            self.sender.deliver("investat", payload())
        self.assertFalse(error.exception.uncertain)
        self.session.post.side_effect = requests.ReadTimeout(TOKEN)
        with self.assertRaises(DeliveryError) as error:
            self.sender.deliver("investat", payload())
        self.assertTrue(error.exception.uncertain)
        self.assertNotIn(TOKEN, str(error.exception))

    def test_malformed_success_does_not_retry_duplicate(self):
        self.response({"ok": True, "result": True})
        with self.assertRaises(DeliveryError) as error:
            self.sender.deliver("investat", payload())
        self.assertTrue(error.exception.uncertain)

    def test_not_modified_edit_counts_as_success(self):
        self.response({"ok": False, "error_code": 400, "description": "Bad Request: message is not modified"}, 400)
        result = self.sender.deliver("investat", payload(), message_id=22, previous_kind="text")
        self.assertEqual(result["message_id"], 22)


if __name__ == "__main__":
    unittest.main()
