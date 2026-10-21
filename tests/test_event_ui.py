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


def revision_script():
    import streamlit as st
    from src.event_ui import render_event_revision
    render_event_revision(st.session_state["revision"])


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
        self.assertNotIn("缺少同口徑市場預期", self.rendered(app))
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
        self.assertFalse(any("[injected]" in item.value for item in app.text))
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
        self.assertLess(texts.index("盈利改善但指引仍有不確定性。"),
                        texts.index("LATER UPDATE MUST NOT REPLACE THE CACHED RESULT"))
        self.assertIn("沒有重複呼叫模型", self.rendered(app))

    def revision_app(self, *, source=None, status="needs_review", version=None, scores=None):
        from src.event_analysis import WORKFLOW_VERSION
        revision = {"id": 1, "created_at": "2026-10-21T15:00:00+00:00", "brief": brief(),
            "sources": [source or {"id": "S1", "title": "新聞", "content_scope": "provided_text"}],
            "metadata": {"review_status": status,
                "workflow_version": WORKFLOW_VERSION if version is None else version,
                "review_findings": ["尚未確認指引比較口徑。"]}}
        if scores is not None:
            revision["brief"]["impact_scores"] = scores
        app = AppTest.from_function(revision_script)
        app.session_state["revision"] = revision
        app.run()
        self.assertFalse(app.exception)
        return app

    def test_compact_brief_omits_research_sections_and_keeps_honest_manual_source(self):
        app = self.revision_app()
        self.assertEqual([item.label for item in app.expander], ["來源"])
        self.assertTrue(all(not item.proto.expanded for item in app.expander))
        self.assertFalse(app.warning)
        self.assertFalse(app.link_button)
        self.assertIn("貼上文字（未附原文連結）", self.rendered(app))
        self.assertIn("有待核對項目", self.rendered(app))
        for removed in ("本次新增資訊", "事實與來源", "反方因素與成立條件", "下一個驗證點", "尚缺資料",
                        "尚未確認指引比較口徑", "缺少同口徑市場預期", "下次披露若經常性盈利改善"):
            self.assertNotIn(removed, self.rendered(app))
        # The whole summary appears once, outside source provenance.
        self.assertEqual(sum(item.value == brief()["summary"]["text"] for item in app.text), 1)
        self.assertFalse(any(item.value == brief()["summary"]["text"]
                             for section in app.expander for item in section.text))

    def test_source_links_use_snapshot_labels_and_do_not_imply_pasted_link_was_fetched(self):
        app = self.revision_app(source={"id": "S1", "publisher": "Pretend Official",
            "url": "https://example.org/release", "content_scope": "provided_text"})
        self.assertEqual(app.link_button[0].label, "所附原文 · example.org")
        self.assertEqual(app.link_button[0].url, "https://example.org/release")
        self.assertIn("不表示已讀取或核實原文", self.rendered(app))

    def test_publisher_markdown_is_inert_and_legacy_snapshot_gets_regeneration_hint(self):
        app = self.revision_app(source={"id": "S1", "publisher": "[Fake](https://evil.example)",
            "url": "https://example.org/release", "content_scope": "full_text"}, version=2)
        self.assertIn("\\[Fake\\]", app.link_button[0].label)
        self.assertEqual({item.url for item in app.link_button}, {"https://example.org/release"})
        self.assertIn("這是舊版保存內容", self.rendered(app))
        self.assertIn("重新生成新版快訊", self.rendered(app))

    def test_scores_and_reasons_are_immediately_visible_and_never_inferred_for_legacy(self):
        scores = [{"target": "示例晶片公司", "bullish_score": 8, "bearish_score": 3,
                   "reason": statement("可能擴大晶片需求，但仍受供應約束。")},
                  {"target": "示例傳統設備商", "bullish_score": 1, "bearish_score": 6,
                   "reason": statement("新架構可能取代部分傳統設備。")}]
        app = self.revision_app(scores=scores)
        rendered = self.rendered(app)
        for text in ("示例晶片公司", "利好 8/10", "利空 3/10", "利好 1/10", "利空 6/10",
                     "可能擴大晶片需求", "不是股價漲跌機率"):
            self.assertIn(text, rendered)
        for section in app.expander:
            self.assertFalse(any("8/10" in item.value or "示例晶片公司" in item.value for item in section.text))
        self.analyze.assert_not_called()
        legacy = self.revision_app(version=3)
        self.assertIn("未提供利好／利空評分", self.rendered(legacy))
        self.assertFalse(any("/10" in item.value for item in legacy.text))
        self.analyze.assert_not_called()

    def test_scored_target_and_reason_stay_plain_and_reason_sources_are_displayed(self):
        target = "[fake](https://evil.example)"
        reason = "<script>alert(1)</script> [injected](https://evil.example)"
        app = self.revision_app(source={"id": "S2", "publisher": "[Publisher](https://evil.example)",
            "url": "https://example.org/source2", "content_scope": "full_text"},
            scores=[{"target": target, "bullish_score": 5, "bearish_score": 2,
                     "reason": {"text": reason, "source_ids": ["S2"]}}])
        self.assertIn(target, [item.value for item in app.text])
        self.assertTrue(any(reason in item.value for item in app.text))
        self.assertFalse(any("evil.example" in item.value for item in app.markdown))
        self.assertEqual(app.link_button[0].url, "https://example.org/source2")
        self.assertIn("\\[Publisher\\]", app.link_button[0].label)
        self.analyze.assert_not_called()


if __name__ == "__main__":
    unittest.main()
