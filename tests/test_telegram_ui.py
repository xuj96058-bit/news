"""Daily Telegram feed behavior without network calls or real credentials."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from streamlit.testing.v1 import AppTest


NOW = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)
CHAT_ID = -1001234567890
CONFIG = SimpleNamespace(token="synthetic-token-never-render", chat_id=CHAT_ID)


def feed_script():
    from src.telegram_ui import render_telegram_feed
    render_telegram_feed()


class FeedStore:
    def __init__(self, posts):
        self.posts = posts
        self.status = {"last_success_at": NOW.isoformat(), "last_error": None}
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def get_status(self):
        return self.status

    def _matching(self, day, now, chat_id):
        matched = []
        for post in self.posts:
            stamp = datetime.fromisoformat(post["published_at"])
            if (post["chat_id"] != chat_id or stamp <= now - timedelta(hours=72)
                    or (day and stamp.astimezone(ZoneInfo("Asia/Hong_Kong")).date() != day)):
                continue
            matched.append(post)
        return sorted(matched, key=lambda post: post["published_at"], reverse=True)

    def count_posts(self, *, day, now, chat_id):
        self.queries.append((day, now, chat_id))
        return len(self._matching(day, now, chat_id))

    def list_posts(self, *, day, now, chat_id, limit, offset):
        return self._matching(day, now, chat_id)[offset:offset + limit]


def post(index, *, stamp=None, text=None, chat_id=CHAT_ID):
    return {"chat_id": chat_id, "message_id": index,
            "published_at": (stamp or NOW - timedelta(minutes=index)).isoformat(),
            "title": "News 測試頻道", "text": text or f"頻道新聞第 {index} 則",
            "url": "https://untrusted.invalid/never-open", "media_type": None}


class TelegramUiTests(unittest.TestCase):
    def setUp(self):
        self.store = FeedStore([])
        self.config = self.patch("src.telegram_ui.load_config", return_value=CONFIG)
        self.start = self.patch("src.telegram_ui.ensure_telegram_receiver", return_value=None)
        self.patch("src.telegram_ui._utc_now", return_value=NOW)
        self.open_store = self.patch("src.telegram_ui.TelegramStore", return_value=self.store)
        self.patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected network"))

    def patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def app(self):
        app = AppTest.from_function(feed_script, default_timeout=10).run()
        self.assertFalse(app.exception)
        return app

    def rendered(self, app):
        return "\n".join(str(item.value) for kind in ("text", "caption", "info", "warning", "error")
                         for item in app.get(kind))

    def test_missing_config_explains_setup_without_collecting_or_opening_store(self):
        self.config.return_value = None
        app = self.app()
        self.assertIn("TELEGRAM_BOT_TOKEN", self.rendered(app))
        self.assertIn("重新啟動 News", self.rendered(app))
        self.start.assert_not_called()
        self.open_store.assert_not_called()

    def test_plain_text_attachments_and_generated_original_links(self):
        unsafe = "<script>alert('no')</script>\n![tracking](https://evil.invalid/pixel)"
        item = post(4, text=unsafe)
        item["media_type"] = "photo"
        item["edited_at"] = NOW.isoformat()
        self.store.posts = [item, post(5, chat_id=-1009999999999)]
        app = self.app()
        self.assertEqual(len(app.link_button), 1)
        self.assertEqual(app.link_button[0].url, "https://t.me/c/1234567890/4")
        self.assertIn(unsafe, [element.value for element in app.text])
        self.assertFalse(app.html)
        self.assertIn("2026-10-22 18:00:00", self.rendered(app))
        self.assertIn("含附件", self.rendered(app))
        self.assertNotIn(CONFIG.token, self.rendered(app))
        self.assertNotIn("頻道新聞第 5 則", self.rendered(app))

    def test_pagination_reaches_every_post_and_date_change_returns_to_first_page(self):
        self.store.posts = [post(index) for index in range(1, 46)]
        app = self.app()
        self.assertEqual(len(app.link_button), 20)
        self.assertIn("共 45 則 · 第 1／3 頁", self.rendered(app))
        app.button(key="telegram_feed_next").click().run()
        self.assertIn("第 2／3 頁", self.rendered(app))
        app.button(key="telegram_feed_next").click().run()
        self.assertEqual(len(app.link_button), 5)
        self.assertIn("頻道新聞第 45 則", self.rendered(app))
        self.assertTrue(app.button(key="telegram_feed_next").disabled)
        day = NOW.astimezone(ZoneInfo("Asia/Hong_Kong")).date()
        app.selectbox(key="telegram_feed_day").select(day).run()
        self.assertFalse(app.exception)
        self.assertIn("第 1／3 頁", self.rendered(app))
        self.assertEqual(self.store.queries[-1], (day, NOW, CHAT_ID))

    def test_rolling_window_includes_four_local_dates_and_expiry_clamps_page(self):
        self.store.posts = [post(index) for index in range(1, 25)]
        app = self.app()
        self.assertEqual(app.selectbox(key="telegram_feed_day").options,
                         ["最近 72 小時", "2026-10-22", "2026-10-21", "2026-10-20", "2026-10-19"])
        app.button(key="telegram_feed_next").click().run()
        self.store.posts = [post(1)]
        app.run()
        self.assertIn("共 1 則 · 第 1／1 頁", self.rendered(app))
        self.assertEqual(app.session_state["telegram_feed_page"], 0)

    def test_selected_date_empty_state_does_not_claim_receiver_is_unconfigured(self):
        self.store.posts = [post(1)]
        app = self.app()
        day = (NOW - timedelta(days=1)).date()
        app.selectbox(key="telegram_feed_day").select(day).run()
        self.assertIn("這個日期沒有仍在保留期限內", self.rendered(app))
        self.assertNotIn("TELEGRAM_BOT_TOKEN", self.rendered(app))

    def test_no_posts_explains_new_post_and_history_limit_and_reports_sync_error(self):
        self.store.status = {"last_success_at": None, "last_error": "請先把 Bot 加入目標頻道。"}
        app = self.app()
        self.assertIn("發布一則新訊息", self.rendered(app))
        self.assertIn("不會自動補抓", self.rendered(app))
        self.assertIn("正在等待首次同步", self.rendered(app))
        self.assertIn("請先把 Bot 加入目標頻道", self.rendered(app))

    def test_configuration_and_database_errors_never_render_exception_secrets(self):
        self.config.side_effect = ValueError(CONFIG.token)
        app = self.app()
        self.assertIn("Telegram 設定未完成", self.rendered(app))
        self.assertNotIn(CONFIG.token, self.rendered(app))
        self.config.side_effect = None
        self.open_store.side_effect = RuntimeError("https://api.telegram.org/bot" + CONFIG.token)
        app.run()
        self.assertIn("暫時無法讀取頻道新聞", self.rendered(app))
        self.assertNotIn(CONFIG.token, self.rendered(app))


class DummyThread:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.alive = False

    def start(self):
        self.alive = True

    def is_alive(self):
        return self.alive


class ReceiverLifetimeTests(unittest.TestCase):
    def test_reruns_reuse_worker_and_switch_waits_for_old_request_to_exit(self):
        from src.telegram_ui import ReceiverManager
        threads = []

        def make_thread(**kwargs):
            thread = DummyThread(**kwargs)
            threads.append(thread)
            return thread

        manager = ReceiverManager()
        with patch("src.telegram_ui.threading.Thread", side_effect=make_thread):
            self.assertTrue(manager.ensure(CONFIG, Path("synthetic.db")))
            self.assertTrue(manager.ensure(CONFIG, Path("synthetic.db")))
            self.assertEqual(len(threads), 1)
            replacement = SimpleNamespace(token="different-synthetic-token", chat_id=CHAT_ID)
            self.assertFalse(manager.ensure(replacement, Path("synthetic.db")))
            self.assertTrue(threads[0].kwargs["args"][2].is_set())
            self.assertEqual(len(threads), 1)
            threads[0].alive = False
            self.assertTrue(manager.ensure(replacement, Path("synthetic.db")))
            self.assertEqual(len(threads), 2)
            manager.stop()
            self.assertTrue(threads[1].kwargs["args"][2].is_set())
            self.assertNotIn(CONFIG.token, threads[0].kwargs["name"])

    def test_uncaught_worker_error_is_sanitized(self):
        from src.telegram_ui import ReceiverManager
        manager = ReceiverManager()
        with patch("src.telegram_ui.run_worker", side_effect=RuntimeError(CONFIG.token)):
            manager._run(CONFIG, Path("synthetic.db"), None)
        self.assertNotIn(CONFIG.token, manager.last_error)
        self.assertIn("暫時停止", manager.last_error)


if __name__ == "__main__":
    unittest.main()
