"""Paste-news event workflow, with a real temporary store and mocked providers."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

with patch("dotenv.load_dotenv"):
    from src.event_store import EventStore


def event_script():
    import streamlit as st
    from src.event_ui import render_news_analysis_tab
    render_news_analysis_tab(provider=st.session_state.get("test_provider", "deepseek"),
        model=st.session_state.get("test_model", "deepseek-test"),
        api_key=st.session_state.get("test_key", "synthetic-analysis-key"),
        analysis_available=bool(st.session_state.get("test_key", "synthetic-analysis-key")))


def statement(text):
    return {"text": text, "source_ids": ["S1"]}


def brief():
    return {"headline": "盈利改善是否可以持續", "summary": statement("盈利改善但指引仍有不確定性。"),
        "facts": [statement("本次公告披露盈利改善。")],
        "new_information": statement("指引是本次新增的資料。"), "judgement": "initial",
        "judgement_reason": statement("首次根據這份公告形成判斷。"),
        "impacts": [{"ticker": "EXAMPLE", "direction": "mixed", "horizon": "下次披露前",
                     "mechanism": statement("盈利支持估值，但指引限制上行解釋。") }],
        "counterarguments": [statement("盈利改善也可能只是一次性因素。")],
        "watchlist": [statement("下次披露若經常性盈利改善，將支持原判斷。")],
        "missing_evidence": ["缺少同口徑市場預期。"], "confidence": "medium"}


class EventUiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "events.db"
        self.patch("dotenv.load_dotenv")
        self.patch("src.event_ui.EventStore", side_effect=lambda _path=None: EventStore(self.path))
        self.analyze = self.patch("src.event_ui.analyze_event", side_effect=self.generate)
        self.adapter = self.patch("src.event_ui.brief_to_analysis", return_value={"summary": "daily brief"})
        self.save = self.patch("src.event_ui.save_event", return_value=True)
        self.patch("src.event_ui.provider_environment_key", return_value="")
        self.patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected network"))

    def patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def generate(self, articles, *, store, topic, event_id=None, **kwargs):
        event_id = event_id or store.create_event(topic)["id"]
        fingerprint = hashlib.sha256(json.dumps([articles, topic], sort_keys=True).encode()).hexdigest()
        cached = store.find_revision(event_id, fingerprint)
        if cached:
            cached["metadata"]["cached"] = True
            return cached
        sources = [{"id": "S1", "title": articles[0]["title"], "content": articles[0]["content"],
                    "url": articles[0].get("url", ""), "content_scope": "provided_text"}]
        return store.create_revision(event_id, article_ids=[articles[0]["id"]], sources=sources,
            brief=brief(), metadata={"review_status": "passed", "api_call_count": 3, "search_calls": 0,
                "validation_warnings": [], "review_findings": []}, input_hash=fingerprint)

    def app(self, **state):
        app = AppTest.from_function(event_script, default_timeout=10)
        for key, value in state.items():
            app.session_state[key] = value
        app.run()
        self.assertFalse(app.exception)
        return app

    def rendered(self, app):
        return "\n".join(str(item.value) for kind in ("caption", "text", "info", "success", "warning", "error", "markdown")
                         for item in app.get(kind))

    def test_primary_action_preserves_text_entry_and_has_no_implicit_requests(self):
        app = self.app()
        self.assertEqual(app.text_area[0].label, "貼上新聞內容")
        self.assertFalse(app.checkbox(key="event_search_enabled").value)
        self.assertEqual(app.button(key="event_analyze").label, "分析")
        app.button(key="event_analyze").click().run()
        self.assertIn("請先貼上", self.rendered(app))
        app.text_area(key="event_news_text").input("盈利公告\n收入及指引仍須核對。").run()
        self.analyze.assert_not_called()
        app.button(key="event_analyze").click().run()
        self.assertFalse(app.exception)
        self.analyze.assert_called_once()
        call = self.analyze.call_args
        self.assertEqual(call.kwargs["topic"], "盈利公告")
        self.assertEqual(call.kwargs["api_key"], "synthetic-analysis-key")
        self.assertFalse(call.kwargs["search_enabled"])
        self.assertEqual(call.kwargs["search_api_key"], "")
        self.assertEqual(call.args[0][0]["content"], "盈利公告\n收入及指引仍須核對。")
        self.assertIn("缺少同口徑市場預期", self.rendered(app))
        self.assertEqual(self.save.call_args.args, ("盈利公告\n收入及指引仍須核對。", {"summary": "daily brief"}))
        event_id = app.selectbox(key="event_selection").value
        self.assertIsNotNone(event_id)
        app.run()
        self.analyze.assert_called_once()
        app.button(key="event_analyze").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(self.analyze.call_args.kwargs["event_id"], event_id)
        with EventStore(self.path) as store:
            self.assertEqual(len(store.list_events()), 1)
            self.assertEqual(len(store.list_revisions(event_id)), 1)
        self.assertIn("沒有重複呼叫模型", self.rendered(app))

    def test_search_credentials_are_separate_and_missing_search_key_blocks_action(self):
        app = self.app()
        app.text_area(key="event_news_text").input("A new event").run()
        app.checkbox(key="event_search_enabled").check().run()
        self.assertTrue(app.button(key="event_analyze").disabled)
        field = app.text_input(key="event_search_api_key")
        self.assertEqual(field.value, "")
        field.input("  synthetic-search-key  ").run()
        app.checkbox(key="event_include_x").check().run()
        app.selectbox(key="event_search_rounds").select(2).run()
        app.selectbox(key="event_search_turns").select(1).run()
        self.analyze.assert_not_called()
        app.button(key="event_analyze").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(self.analyze.call_args.kwargs["api_key"], "synthetic-analysis-key")
        self.assertEqual(self.analyze.call_args.kwargs["search_api_key"], "synthetic-search-key")
        self.assertTrue(self.analyze.call_args.kwargs["include_x"])
        self.assertEqual(self.analyze.call_args.kwargs["max_search_rounds"], 2)
        self.assertEqual(self.analyze.call_args.kwargs["max_search_turns"], 1)
        self.assertNotIn("synthetic-search-key", self.rendered(app))

    def test_xai_analysis_reuses_only_its_own_selected_key_for_search(self):
        app = self.app(test_provider="xai", test_model="grok-test", test_key="synthetic-grok-key")
        app.checkbox(key="event_search_enabled").check().run()
        self.assertFalse(any(item.key == "event_search_api_key" for item in app.text_input))
        app.text_area(key="event_news_text").input("公司公告").run()
        app.button(key="event_analyze").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(self.analyze.call_args.kwargs["search_api_key"], "synthetic-grok-key")

    def test_failure_is_sanitized_and_never_saves_daily(self):
        self.analyze.side_effect = RuntimeError("Bearer synthetic-analysis-key provider raw body")
        app = self.app()
        app.text_area(key="event_news_text").input("公司公告").run()
        app.button(key="event_analyze").click().run()
        self.assertFalse(app.exception)
        self.assertIn("RuntimeError", self.rendered(app))
        self.assertNotIn("synthetic-analysis-key", self.rendered(app))
        self.assertNotIn("provider raw body", self.rendered(app))
        self.save.assert_not_called()

    def test_saved_history_is_available_without_new_input_and_model_links_stay_plain(self):
        with EventStore(self.path) as store:
            event = store.create_event("已有事件")
            for index in range(2):
                draft = brief()
                draft["headline"] = "[injected](https://untrusted.example)"
                draft["summary"]["text"] = "<script>bad()</script>"
                store.create_revision(event["id"], article_ids=[str(index)],
                    sources=[{"id": "S1", "title": "原文", "url": "https://example.org/source",
                              "content": "source", "content_scope": "search_summary"},
                             {"id": "S2", "title": "bad", "url": "javascript:alert(1)", "content": "bad"}],
                    brief=draft, metadata={"review_status": "failed"}, input_hash=str(index) * 64)
        app = self.app()
        app.selectbox(key="event_selection").select(event["id"]).run()
        self.assertFalse(app.exception)
        self.analyze.assert_not_called()
        self.assertIn("歷史判斷快照", self.rendered(app))
        self.assertIn("審稿未完成", self.rendered(app))
        self.assertIn("尚未獨立核對原文", self.rendered(app))
        self.assertEqual({item.url for item in app.link_button}, {"https://example.org/source"})
        self.assertTrue(any("[injected]" in item.value for item in app.text))
        self.assertFalse(any("[injected]" in item.value for item in app.markdown))

    def test_source_url_validation_and_keyless_state_never_call_provider(self):
        app = self.app(test_key="")
        self.assertTrue(app.button(key="event_analyze").disabled)
        app.session_state["test_key"] = "synthetic-analysis-key"
        app.run()
        app.text_area(key="event_news_text").input("公司公告").run()
        app.text_input(key="event_source_url").input("https://user:secret@example.org/source").run()
        app.button(key="event_analyze").click().run()
        self.assertIn("不含登入資訊", self.rendered(app))
        self.analyze.assert_not_called()

    def test_cache_of_older_inputs_displays_returned_revision_not_latest_revision(self):
        app = self.app()
        app.text_area(key="event_news_text").input("First release").run()
        app.button(key="event_analyze").click().run()
        first = app.session_state["event_result"]
        event_id = first["event_id"]
        # A separate later update exists before the user returns to the first input.
        with EventStore(self.path) as store:
            later = brief()
            later["summary"] = statement("LATER UPDATE MUST NOT REPLACE THE CACHED RESULT")
            store.create_revision(event_id, article_ids=["later"], sources=first["sources"],
                brief=later, metadata={"review_status": "passed"}, input_hash="later-input")
        app.button(key="event_analyze").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["event_result"]["id"], first["id"])
        # Both are inspectable, but the primary result before the history expander
        # is the exact snapshot returned by the analyzer.
        texts = [item.value for item in app.text]
        self.assertLess(texts.index("盈利改善但指引仍有不確定性。 [S1]"),
                        texts.index("LATER UPDATE MUST NOT REPLACE THE CACHED RESULT [S1]"))
        self.assertIn("沒有重複呼叫模型", self.rendered(app))


if __name__ == "__main__":
    unittest.main()
