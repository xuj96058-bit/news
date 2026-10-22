"""Real-time reader behavior without network calls or real credentials."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo
from streamlit.testing.v1 import AppTest
from src.telegram_source import RETENTION

NOW = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)
CHAT_ID = -1001234567890
CONFIG = SimpleNamespace(token="synthetic-token-never-render", chat_id=CHAT_ID)

def feed_script():
    import streamlit as st
    from src.telegram_ui import _dismiss_article, render_telegram_feed
    if st.session_state.pop("_test_dismiss_article", False):
        _dismiss_article()
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
    def _matching(self, day, now, chat_id, query="", before=None):
        result = []
        for item in self.posts:
            stamp = datetime.fromisoformat(item["published_at"])
            if (item["chat_id"] != chat_id or stamp <= now - RETENTION
                or (day and stamp.astimezone(ZoneInfo("Asia/Hong_Kong")).date() != day)
                or query.casefold() not in item["text"].casefold()
                or (before and (stamp.timestamp(), item["message_id"]) > tuple(before))):
                continue
            result.append(item)
        return sorted(result, key=lambda p: (p["published_at"], p["message_id"]), reverse=True)
    def count_posts(self, *, day, now, chat_id, query="", before=None):
        return len(self._matching(day, now, chat_id, query, before))
    def list_posts(self, *, day, now, chat_id, limit, offset=0, query="", before=None):
        self.queries.append(dict(day=day, query=query, before=before, limit=limit, offset=offset))
        if limit > 1000:
            raise ValueError("Invalid pagination")
        return self._matching(day, now, chat_id, query, before)[offset:offset + limit]

def post(index, *, stamp=None, text=None, chat_id=CHAT_ID):
    return {"chat_id": chat_id, "message_id": index,
            "published_at": (stamp or NOW - timedelta(minutes=index)).isoformat(),
            "title": "News 測試頻道", "text": text or f"新聞標題第 {index} 則\n第 {index} 則新聞的原始內文。",
            "url": "https://untrusted.invalid/never-open", "media_type": None}

class TelegramUiTests(unittest.TestCase):
    def setUp(self):
        self.store = FeedStore([])
        self.config = self.patch("src.telegram_ui.load_config", return_value=CONFIG)
        self.start = self.patch("src.telegram_ui.ensure_telegram_receiver", return_value=None)
        self.now = self.patch("src.telegram_ui._utc_now", return_value=NOW)
        self.open_store = self.patch("src.telegram_ui.TelegramStore", return_value=self.store)
        self.patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected network"))
    def patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result
    def app(self):
        app = AppTest.from_function(feed_script, default_timeout=30).run()
        self.assertFalse(app.exception)
        return app
    def rendered(self, app):
        return "\n".join(str(item.value) for kind in ("text", "caption", "info", "warning", "error", "subheader")
                         for item in app.get(kind))
    def rows(self, app):
        return [b for b in app.button if b.key.startswith("telegram_open_")]
    def test_missing_configuration_is_simple_and_does_not_start_collection(self):
        self.config.return_value = None
        app = self.app()
        self.assertIn("尚未連接新聞來源", self.rendered(app))
        self.assertNotIn("TELEGRAM_BOT_TOKEN", self.rendered(app))
        self.start.assert_not_called()
        self.open_store.assert_not_called()
    def test_clicked_article_is_literal_and_has_no_original_link(self):
        unsafe = "<script>alert('no')</script>\n![tracking](https://evil.invalid/pixel) :red[no] $x$"
        item = post(4, text=unsafe)
        item["media_type"] = "photo"
        item["edited_at"] = NOW.isoformat()
        self.store.posts = [item, post(5, chat_id=-1009999999999)]
        app = self.app()
        self.assertFalse(app.link_button)
        self.assertNotIn(unsafe, [element.value for element in app.text])
        app.button(key="telegram_open_4").click().run()
        self.assertFalse(app.exception)
        self.assertFalse(app.link_button)
        self.assertEqual([element.value for element in app.text].count(unsafe), 1)
        self.assertIn(r"\<script\>", app.button(key="telegram_open_4").label)
        self.assertNotIn("<script>", "".join(element.value for element in app.html))
        self.assertIn("2026-10-22 18:00", self.rendered(app))
        self.assertIn("此則新聞包含附件", self.rendered(app))
        self.assertNotIn(CONFIG.token, self.rendered(app))
        self.assertNotIn("新聞標題第 5 則", self.rendered(app))
        self.assertNotIn("News 測試頻道", self.rendered(app))
        self.assertNotIn("上次成功同步", self.rendered(app))
        self.assertNotIn("查看原文", self.rendered(app))
    def test_dialog_opens_only_on_click_and_can_be_dismissed_and_reopened(self):
        self.store.posts = [post(1), post(2)]
        app = self.app()
        self.assertNotIn("telegram_feed_dialog_id", app.session_state)
        app.button(key="telegram_open_1").click().run()
        self.assertEqual(app.session_state["telegram_feed_dialog_id"], 1)
        app.session_state["_test_dismiss_article"] = True
        app.run()
        self.assertNotIn("telegram_feed_dialog_id", app.session_state)
        self.assertNotIn(self.store.posts[0]["text"], [item.value for item in app.text])
        app.button(key="telegram_open_1").click().run()
        self.assertEqual(app.session_state["telegram_feed_dialog_id"], 1)
        app.button(key="telegram_open_2").click().run()
        self.assertEqual(app.session_state["telegram_feed_dialog_id"], 2)
        self.assertIn(self.store.posts[1]["text"], [item.value for item in app.text])
        self.assertFalse(app.exception)
    def test_selection_and_load_more(self):
        self.store.posts = [post(index) for index in range(1, 46)]
        app = self.app()
        self.assertEqual(len(self.rows(app)), 20)
        app.button(key="telegram_open_12").click().run()
        self.assertEqual(app.session_state["telegram_feed_selected"], 12)
        self.assertIn("第 12 則新聞的原始內文。", app.text[-1].value)
        app.button(key="telegram_feed_more").click().run()
        self.assertEqual(app.session_state["telegram_feed_selected"], 12)
        app.button(key="telegram_feed_more").click().run()
        self.assertEqual(len(self.rows(app)), 45)
        self.assertFalse([b for b in app.button if b.key == "telegram_feed_more"])
        app.button(key="telegram_open_45").click().run()
        self.assertIn("第 45 則新聞的原始內文。", app.text[-1].value)
        self.assertFalse(app.exception)
    def test_search_finds_older_week_items_and_resets_window(self):
        old = post(60, stamp=NOW - timedelta(days=6), text="ABC 公司業績\n公司公布收入。")
        self.store.posts = [post(index) for index in range(1, 25)] + [old]
        app = self.app()
        app.button(key="telegram_feed_more").click().run()
        app.text_input(key="telegram_feed_search").set_value("abc").run()
        self.assertEqual(app.session_state["telegram_feed_limit"], 20)
        self.assertNotIn("telegram_feed_selected", app.session_state)
        self.assertEqual(app.button(key="telegram_open_60").label, "ABC 公司業績")
        self.assertEqual(self.store.queries[-1]["query"], "abc")
        app.text_input(key="telegram_feed_search").set_value("does not exist").run()
        self.assertIn("沒有符合條件", self.rendered(app))
        self.assertFalse(app.link_button)

    def test_long_first_paragraph_has_compact_heading_without_losing_content(self):
        from src.telegram_ui import HEADLINE_LIMIT
        paragraph = "公司發布市場消息，補充供應鏈與業務安排。" * 8
        self.store.posts = [post(1, text=paragraph + "\n\n來源資料")]
        app = self.app()
        self.assertEqual(app.button(key="telegram_open_1").label,
                         paragraph[:HEADLINE_LIMIT] + "…")
        self.assertNotIn(paragraph + "\n\n來源資料", [item.value for item in app.text])
        self.assertTrue(any(item.value.startswith(paragraph[HEADLINE_LIMIT:HEADLINE_LIMIT + 20])
                            for item in app.text))
        app.button(key="telegram_open_1").click().run()
        self.assertEqual([item.value for item in app.text].count(paragraph + "\n\n來源資料"), 1)

    def test_dates_include_partial_eighth_day_and_scope_reader(self):
        yesterday = NOW.astimezone(ZoneInfo("Asia/Hong_Kong")).date() - timedelta(days=1)
        self.store.posts = [post(1), post(30, stamp=NOW - timedelta(days=1))]
        app = self.app()
        self.assertEqual(app.selectbox(key="telegram_feed_day").options,
                         ["最近 7 天", "今天", "昨天", "10月20日", "10月19日", "10月18日", "10月17日", "10月16日", "10月15日"])
        app.selectbox(key="telegram_feed_day").select(yesterday).run()
        self.assertNotIn("telegram_feed_selected", app.session_state)
        self.assertNotIn("telegram_feed_dialog_id", app.session_state)
        self.assertEqual(app.button(key="telegram_open_30").label, "新聞標題第 30 則")
        self.assertEqual(self.store.queries[-1]["day"], yesterday)
        self.assertFalse([b for b in app.button if b.key == "telegram_open_1"])
    def test_new_posts_wait_for_acceptance_and_keep_current_article(self):
        self.store.posts = [post(1), post(2)]
        app = self.app()
        app.button(key="telegram_open_2").click().run()
        self.store.posts.append(post(100, stamp=NOW, text="最新消息\n新的內容。"))
        app.run()
        self.assertIn("有 1 則新消息", app.button(key="telegram_feed_latest").label)
        self.assertEqual(app.session_state["telegram_feed_selected"], 2)
        self.assertFalse([b for b in app.button if b.key == "telegram_open_100"])
        app.button(key="telegram_feed_latest").click().run()
        self.assertEqual(app.button(key="telegram_open_100").label, "最新消息")
        self.assertEqual(app.session_state["telegram_feed_selected"], 2)
        self.assertFalse([b for b in app.button if b.key == "telegram_feed_latest"])
    def test_expiry_removes_selected_body_from_reader_and_session(self):
        self.store.posts = [post(1), post(2, stamp=NOW - RETENTION + timedelta(minutes=1), text="即將過期\n到期不應留在閱讀欄。")]
        app = self.app()
        app.button(key="telegram_open_2").click().run()
        self.now.return_value = NOW + timedelta(minutes=1)
        app.run()
        self.assertNotIn("telegram_feed_selected", app.session_state)
        self.assertNotIn("telegram_feed_dialog_id", app.session_state)
        self.assertNotIn("到期不應留在閱讀欄", self.rendered(app))
        self.assertFalse([b for b in app.button if b.key == "telegram_open_2"])
    def test_load_more_beyond_backend_batch_limit(self):
        self.store.posts = [post(index) for index in range(1, 1006)]
        app = self.app()
        app.session_state["telegram_feed_limit"] = 1005
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(len(self.rows(app)), 1005)
        self.assertTrue(any(q["offset"] == 1000 for q in self.store.queries))
        self.assertTrue(all(q["limit"] <= 1000 for q in self.store.queries))
    def test_errors_never_expose_secrets(self):
        self.config.side_effect = ValueError(CONFIG.token)
        app = self.app()
        self.assertIn("新聞連線設定未完成", self.rendered(app))
        self.assertNotIn(CONFIG.token, self.rendered(app))
        self.config.side_effect = None
        self.open_store.side_effect = RuntimeError("https://api.telegram.org/bot" + CONFIG.token)
        app.run()
        self.assertIn("暫時無法讀取新聞", self.rendered(app))
        self.assertNotIn(CONFIG.token, self.rendered(app))
        self.open_store.side_effect = None
        self.store.status["last_error"] = CONFIG.token
        self.start.return_value = CONFIG.token
        app.run()
        self.assertIn("新聞更新暫時中斷", self.rendered(app))
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
