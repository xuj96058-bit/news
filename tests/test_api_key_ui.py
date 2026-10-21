"""Exercise session credentials without reading .env or contacting providers."""

import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from streamlit.proto.TextInput_pb2 import TextInput
from streamlit.testing.v1 import AppTest

# src imports its environment loader; never load a developer's local secrets.
with patch("dotenv.load_dotenv"):
    from src.analyzer import AnalysisResult, EventExtraction
    from src.feeds import Source
    from src.rss_pipeline import analyze_pending as run_pending


APP = Path(__file__).resolve().parents[1] / "app.py"
SESSION_KEY = "synthetic-session-credential"
SERVER_KEY = "synthetic-server-credential"
INVESTAT_TOKEN = "synthetic-investat-session-token"
SERVER_INVESTAT_TOKEN = "synthetic-investat-server-token"
SERVER_URL = "https://synthetic-provider.example/v1"
RSS_BUTTON = "AI 分析待處理新聞（最多 5 則）"
PROVIDERS = ("deepseek", "openai", "xai", "anthropic", "gemini", "openrouter")
PROVIDER_KEY_VARIABLES = (
    "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY", "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY",
)


class ApiKeyUiTests(unittest.TestCase):
    def setUp(self):
        self.start_patch(patch("dotenv.load_dotenv"))
        self.start_patch(patch.dict(os.environ, {
            **{name: "" for name in PROVIDER_KEY_VARIABLES},
            "OPENAI_BASE_URL": SERVER_URL, "LLM_PROVIDER": "deepseek",
            "LLM_MODEL": "synthetic-other-provider-model",
            "INVESTAT_MCP_TOKEN": "",
        }))
        self.result = AnalysisResult(
            extraction=EventExtraction(
                event_type="earnings", entities=[], fact="Synthetic earnings release",
                direction="neutral", confidence=0.8, reasoning="Synthetic test data",
            ),
            impacts=[], summary="Synthetic analysis", overall_confidence=0.8,
        )
        self.analyze = self.start_patch(patch("src.analyzer.analyze_news", return_value=self.result))
        self.save = self.start_patch(patch("src.collector.save_event", return_value=True))
        self.event_revision = {"id": "revision-1", "event_id": "event-1", "created_at": "2026-10-21T01:00:00Z",
            "brief": {"headline": "Synthetic event brief", "summary": {"text": "Synthetic analysis", "source_ids": []}},
            "sources": [], "metadata": {"review_status": "passed"}}
        self.event_analyze = self.start_patch(patch("src.event_ui.analyze_event", return_value=self.event_revision))
        self.start_patch(patch("src.event_ui.brief_to_analysis", return_value=self.result))
        self.start_patch(patch("src.event_ui.save_event", new=self.save))
        event_store = MagicMock()
        event_store.list_events.return_value = [{"id": "event-1", "title": "Synthetic event"}]
        event_store.get_event.return_value = {"id": "event-1", "title": "Synthetic event"}
        event_store.list_revisions.return_value = [self.event_revision]
        self.start_patch(patch("src.event_ui.EventStore")).return_value.__enter__.return_value = event_store
        self.start_patch(patch("src.collector.load_events", return_value=[]))
        self.start_patch(patch("src.report.load_report_events", return_value=[]))
        self.start_patch(patch("src.report.generate_daily_report", return_value="Synthetic report"))
        # The research workflow has its own integration tests and database boundary.
        self.start_patch(patch("src.research_ui.render_research_tab"))
        self.start_patch(patch("src.workbench_ui.render_reader_tab"))
        self.start_patch(patch("src.workbench_ui.render_source_operations"))
        self.start_patch(patch("src.chartbook_ui.render_chartbook_tab"))
        self.start_patch(patch("src.report_gallery_ui.render_report_gallery"))
        self.start_patch(patch("src.feeds.load_sources", return_value=[
            Source("test", "Synthetic feed", "https://example.org/feed", "official", "us"),
        ]))
        self.store = MagicMock()
        self.store.stats.return_value = {"total": 0, "analyzed": 0}
        self.store.export_articles.return_value = []
        self.store.search_articles.side_effect = lambda **kwargs: {
            "items": self.store.export_articles.return_value, "total": len(self.store.export_articles.return_value)}
        self.store.pending.return_value = [{
            "id": 1, "title": "Synthetic headline", "summary": "Synthetic RSS article body.",
            "content": "", "sources": [{"source_name": "Synthetic feed", "source_kind": "official"}],
            "source_kind": "official", "url": "https://example.org/article", "content_scope": "summary",
            "published_at": None, "content_hash": "synthetic-hash",
        }]
        store_factory = self.start_patch(patch("src.news_store.NewsStore"))
        store_factory.return_value.__enter__.return_value = self.store
        self.pending = self.start_patch(patch("src.rss_pipeline.analyze_pending", wraps=run_pending))
        self.collect = self.start_patch(patch("src.rss_pipeline.collect_sources", return_value={
            "added": 0, "updated": 0, "duplicates": 0, "sources": [],
        }))
        self.investat = self.start_patch(patch("src.investat_pipeline.collect_investat", return_value={
            "added": 0, "updated": 0, "duplicates": 0, "failed_sources": 0,
            "sources": [{"id": "investat", "name": "Investat", "status": "empty"}],
        }))
        self.start_patch(patch("src.rss_pipeline.collect_sec"))
        self.start_patch(patch("src.sec_universe.refresh_universe"))
        # Fail immediately if a future UI change bypasses the mocked boundaries.
        self.start_patch(patch("openai.OpenAI", side_effect=AssertionError("Unexpected provider call")))
        self.start_patch(patch("sqlite3.connect", side_effect=AssertionError("Unexpected database access")))
        self.start_patch(patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected network call")))

    def start_patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def app(self):
        app = AppTest.from_file(str(APP), default_timeout=10).run()
        self.assertEqual(len(app.exception), 0)
        return app

    def button(self, app, label):
        return next(button for button in app.button if button.label == label)

    def rendered_text(self, app):
        return "\n".join(str(element.value) for kind in (
            "caption", "info", "success", "warning", "error", "markdown", "text",
        ) for element in app.get(kind))

    def select_provider(self, app, provider):
        app.sidebar.selectbox(key="ai_provider").select(provider).run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.sidebar.selectbox(key="ai_provider").value, provider)
        return app.sidebar.selectbox(key="ai_model").value

    def assert_analysis_disabled(self, app, disabled=True):
        self.assertEqual(self.button(app, "分析").disabled, disabled)
        self.assertEqual(self.button(app, RSS_BUTTON).disabled, disabled)

    def test_password_field_and_keyless_collection(self):
        app = self.app()
        provider = app.sidebar.selectbox(key="ai_provider")
        self.assertEqual(provider.label, "AI 服務商")
        self.assertEqual(provider.value, "deepseek")
        self.assertEqual(len(provider.options), len(PROVIDERS))
        model = app.sidebar.selectbox(key="ai_model")
        self.assertEqual(model.label, "AI 模型")
        self.assertNotEqual(model.value, os.environ["LLM_MODEL"])
        self.assertIn("自訂模型 ID", model.options)
        field = app.sidebar.text_input(key="ai_api_key")
        self.assertEqual(field.label, "API key")
        self.assertEqual(field.proto.type, TextInput.PASSWORD)
        self.assertEqual(field.value, "")
        self.assert_analysis_disabled(app)
        self.assertFalse(self.button(app, "抓取最新新聞").disabled)
        self.button(app, "抓取最新新聞").click().run()
        self.collect.assert_called_once()
        self.analyze.assert_not_called()
        self.pending.assert_not_called()
        self.assertEqual(len(app.exception), 0)

    def test_manual_analysis_routes_typed_key_and_model_for_all_providers(self):
        os.environ["DEEPSEEK_API_KEY"] = SERVER_KEY
        app = self.app()
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                self.event_analyze.reset_mock()
                self.save.reset_mock()
                model = self.select_provider(app, provider)
                app.sidebar.text_input(key="ai_api_key").input("  " + SESSION_KEY + "  ").run()
                app.text_area[0].input("Synthetic manual news").run()
                self.button(app, "分析").click().run()
                self.assertEqual(len(app.exception), 0)
                self.event_analyze.assert_called_once()
                self.assertEqual(self.event_analyze.call_args.args[0][0]["content"], "Synthetic manual news")
                self.assertEqual(self.event_analyze.call_args.kwargs["model"], model)
                self.assertEqual(self.event_analyze.call_args.kwargs["api_key"], SESSION_KEY)
                self.assertEqual(self.event_analyze.call_args.kwargs["provider"], provider)
                self.assertEqual(self.event_analyze.call_args.kwargs["search_api_key"], "")
                self.analyze.assert_not_called()
                self.save.assert_called_once_with("Synthetic manual news", self.result.model_dump(), source="web")
                self.assertNotIn(SESSION_KEY, self.rendered_text(app))
                self.assertNotIn(SERVER_KEY, self.rendered_text(app))
                self.assertEqual(os.environ["DEEPSEEK_API_KEY"], SERVER_KEY)
                self.assertEqual(os.environ["OPENAI_BASE_URL"], SERVER_URL)
                self.assertEqual(os.environ["LLM_PROVIDER"], "deepseek")

    def test_rss_analysis_routes_typed_key_and_model_for_all_providers(self):
        app = self.app()
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                self.analyze.reset_mock()
                self.pending.reset_mock()
                self.store.mark_analyzed.reset_mock()
                model = self.select_provider(app, provider)
                app.sidebar.text_input(key="ai_api_key").input(SESSION_KEY).run()
                self.button(app, RSS_BUTTON).click().run()
                self.assertEqual(len(app.exception), 0)
                self.pending.assert_called_once()
                self.assertEqual(self.pending.call_args.kwargs["limit"], 5)
                self.assertTrue(callable(self.pending.call_args.kwargs["analyzer"]))
                self.analyze.assert_called_once()
                self.assertIn("Synthetic RSS article body.", self.analyze.call_args.args[0])
                self.assertEqual(self.analyze.call_args.kwargs, {
                    "model": model, "api_key": SESSION_KEY, "provider": provider,
                })
                self.store.mark_analyzed.assert_called_once_with(
                    1, self.result.model_dump(), expected_content_hash="synthetic-hash",
                )

    def test_clear_restores_hidden_server_fallback(self):
        os.environ["DEEPSEEK_API_KEY"] = SERVER_KEY
        app = self.app()
        model = app.sidebar.selectbox(key="ai_model").value
        self.assertEqual(app.sidebar.text_input(key="ai_api_key").value, "")
        self.assert_analysis_disabled(app, False)
        self.assertNotIn(SERVER_KEY, self.rendered_text(app))
        app.sidebar.text_input(key="ai_api_key").input(SESSION_KEY).run()
        self.button(app, "清除輸入的 key").click().run()
        self.assertEqual(app.sidebar.text_input(key="ai_api_key").value, "")
        self.assertEqual(app.session_state["ai_api_key"], "")
        self.assert_analysis_disabled(app, False)
        app.text_area[0].input("Synthetic fallback news").run()
        self.button(app, "分析").click().run()
        self.event_analyze.assert_called_once()
        self.assertEqual(self.event_analyze.call_args.args[0][0]["content"], "Synthetic fallback news")
        self.assertEqual(self.event_analyze.call_args.kwargs["model"], model)
        self.assertEqual(self.event_analyze.call_args.kwargs["api_key"], SERVER_KEY)
        self.assertEqual(self.event_analyze.call_args.kwargs["provider"], "deepseek")
        self.assertNotIn(SERVER_KEY, self.rendered_text(app))
        self.assertEqual(os.environ["DEEPSEEK_API_KEY"], SERVER_KEY)

    def test_environment_credentials_are_not_shared_between_providers(self):
        os.environ["OPENAI_API_KEY"] = SERVER_KEY
        app = self.app()
        self.assert_analysis_disabled(app)
        for endpoint in (SERVER_URL, "https://api.openai.com/v1"):
            os.environ["OPENAI_BASE_URL"] = endpoint
            for provider in PROVIDERS:
                with self.subTest(endpoint=endpoint, provider=provider):
                    self.select_provider(app, provider)
                    self.assert_analysis_disabled(app, not (
                        provider == "openai" and endpoint == "https://api.openai.com/v1"
                    ))
                    self.assertEqual(app.sidebar.text_input(key="ai_api_key").value, "")
                    self.assertNotIn(SERVER_KEY, self.rendered_text(app))
        self.analyze.assert_not_called()

    def test_switching_provider_clears_key_and_resets_custom_model(self):
        app = self.app()
        self.select_provider(app, "openai")
        openai_default = app.sidebar.selectbox(key="ai_model").value
        self.select_provider(app, "deepseek")
        deepseek_default = app.sidebar.selectbox(key="ai_model").value
        app.sidebar.text_input(key="ai_api_key").input(SESSION_KEY).run()
        app.sidebar.selectbox(key="ai_model").select("自訂模型 ID").run()
        app.sidebar.text_input(key="ai_custom_model").input("deepseek-custom-test").run()
        self.assert_analysis_disabled(app, False)
        self.select_provider(app, "openai")
        self.assertEqual(app.sidebar.text_input(key="ai_api_key").value, "")
        self.assertEqual(app.sidebar.selectbox(key="ai_model").value, openai_default)
        self.assert_analysis_disabled(app)
        app.sidebar.selectbox(key="ai_model").select("自訂模型 ID").run()
        self.assertEqual(app.sidebar.text_input(key="ai_custom_model").value, "")
        self.select_provider(app, "deepseek")
        self.assertEqual(app.sidebar.selectbox(key="ai_model").value, deepseek_default)
        self.assertEqual(app.sidebar.text_input(key="ai_api_key").value, "")
        self.assert_analysis_disabled(app)

    def test_custom_model_requires_valid_id_and_forwards_it(self):
        app = self.app()
        app.sidebar.text_input(key="ai_api_key").input(SESSION_KEY).run()
        app.sidebar.selectbox(key="ai_model").select("自訂模型 ID").run()
        self.assertEqual(app.sidebar.text_input(key="ai_custom_model").label, "自訂模型 ID")
        self.assert_analysis_disabled(app)
        for invalid_model in ("   ", "gpt-cross-provider-test", "deepseek-invalid model", "deepseek-invalid\tmodel"):
            with self.subTest(model=repr(invalid_model)):
                app.sidebar.text_input(key="ai_custom_model").input(invalid_model).run()
                self.assert_analysis_disabled(app)
                self.analyze.assert_not_called()
        app.sidebar.text_input(key="ai_custom_model").input("  deepseek-custom-test  ").run()
        self.assert_analysis_disabled(app, False)
        app.text_area[0].input("Synthetic custom model news").run()
        self.button(app, "分析").click().run()
        self.event_analyze.assert_called_once()
        self.assertEqual(self.event_analyze.call_args.args[0][0]["content"], "Synthetic custom model news")
        self.assertEqual(self.event_analyze.call_args.kwargs["model"], "deepseek-custom-test")
        self.assertEqual(self.event_analyze.call_args.kwargs["api_key"], SESSION_KEY)
        self.assertEqual(self.event_analyze.call_args.kwargs["provider"], "deepseek")

    def test_valid_environment_provider_can_select_initial_provider(self):
        os.environ["LLM_PROVIDER"] = "anthropic"
        app = self.app()
        self.assertEqual(app.sidebar.selectbox(key="ai_provider").value, "anthropic")
        self.assertNotEqual(app.sidebar.selectbox(key="ai_model").value, os.environ["LLM_MODEL"])

    def test_openrouter_environment_key_is_available_only_for_router(self):
        os.environ.update(LLM_PROVIDER="openrouter", OPENROUTER_API_KEY=SERVER_KEY)
        app = self.app()
        self.assertEqual(app.sidebar.selectbox(key="ai_provider").value, "openrouter")
        self.assertIn("OpenRouter", app.sidebar.text_input(key="ai_api_key").proto.placeholder)
        self.assert_analysis_disabled(app, False)
        self.assertNotIn(SERVER_KEY, self.rendered_text(app))
        self.select_provider(app, "deepseek")
        self.assert_analysis_disabled(app)
        self.select_provider(app, "openrouter")
        app.text_area[0].input("Synthetic router news").run()
        self.button(app, "分析").click().run()
        self.assertEqual(self.event_analyze.call_args.kwargs["api_key"], SERVER_KEY)
        self.assertEqual(self.event_analyze.call_args.kwargs["provider"], "openrouter")

    def test_openrouter_custom_model_routes_news_and_rss_and_clears_on_switch(self):
        app = self.app()
        self.select_provider(app, "openrouter")
        app.sidebar.text_input(key="ai_api_key").input(SESSION_KEY).run()
        app.sidebar.selectbox(key="ai_model").select("自訂模型 ID").run()
        for invalid in ("gpt-custom", "https://example.org/model", "publisher/bad model"):
            app.sidebar.text_input(key="ai_custom_model").input(invalid).run()
            self.assert_analysis_disabled(app)
        model = "anthropic/claude-custom:free"
        app.sidebar.text_input(key="ai_custom_model").input("  " + model + "  ").run()
        self.assert_analysis_disabled(app, False)
        app.text_area[0].input("Synthetic namespaced model news").run()
        self.button(app, "分析").click().run()
        self.assertEqual(self.event_analyze.call_args.kwargs["model"], model)
        self.assertEqual(self.event_analyze.call_args.kwargs["provider"], "openrouter")
        self.button(app, RSS_BUTTON).click().run()
        self.assertEqual(self.analyze.call_args.kwargs, {
            "model": model, "api_key": SESSION_KEY, "provider": "openrouter",
        })
        self.select_provider(app, "anthropic")
        self.assertEqual(app.sidebar.text_input(key="ai_api_key").value, "")
        self.assertEqual(app.session_state["ai_custom_model"], "")
        self.assert_analysis_disabled(app)
        self.assertEqual(len(app.exception), 0)

    def test_sessions_are_isolated_and_clear_without_fallback_disables_analysis(self):
        first = self.app()
        self.select_provider(first, "gemini")
        first.sidebar.text_input(key="ai_api_key").input(SESSION_KEY).run()
        second = self.app()
        self.assertEqual(second.sidebar.selectbox(key="ai_provider").value, "deepseek")
        self.assertEqual(second.sidebar.text_input(key="ai_api_key").value, "")
        self.assert_analysis_disabled(second)
        first.run()
        self.assertEqual(first.sidebar.selectbox(key="ai_provider").value, "gemini")
        self.assertEqual(first.sidebar.text_input(key="ai_api_key").value, SESSION_KEY)
        self.assert_analysis_disabled(first, False)
        self.button(first, "清除輸入的 key").click().run()
        self.assertEqual(first.session_state["ai_api_key"], "")
        self.assert_analysis_disabled(first)
        for name in PROVIDER_KEY_VARIABLES:
            self.assertEqual(os.environ[name], "")

    def test_manual_provider_error_does_not_echo_credentials_or_response(self):
        self.event_analyze.side_effect = RuntimeError("Authorization: Bearer " + SESSION_KEY + " raw-provider-body")
        app = self.app()
        app.sidebar.text_input(key="ai_api_key").input(SESSION_KEY).run()
        app.text_area[0].input("Synthetic failing news").run()
        self.button(app, "分析").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.error), 1)
        self.assertIn("RuntimeError", app.error[0].value)
        self.assertNotIn(SESSION_KEY, self.rendered_text(app))
        self.assertNotIn("raw-provider-body", self.rendered_text(app))
        self.save.assert_not_called()

    def test_rss_provider_error_is_sanitized_before_storage_and_display(self):
        self.analyze.side_effect = RuntimeError("Authorization: Bearer " + SESSION_KEY + " raw-provider-body")
        app = self.app()
        app.sidebar.text_input(key="ai_api_key").input(SESSION_KEY).run()
        self.button(app, RSS_BUTTON).click().run()
        self.assertEqual(len(app.exception), 0)
        self.store.mark_error.assert_called_once_with(1, "RuntimeError")
        self.store.mark_analyzed.assert_not_called()
        self.assertIn("失敗 1 則", self.rendered_text(app))
        self.assertNotIn(SESSION_KEY, self.rendered_text(app))
        self.assertNotIn("raw-provider-body", self.rendered_text(app))

    def test_stale_rss_analyzer_signature_shows_restart_hint_without_calling_provider(self):
        old_analyzer = lambda text, model=None, *, api_key=None, base_url=None: self.analyze(text, model=model)
        with patch("src.analyzer.analyze_news", new=old_analyzer):
            app = self.app()
            app.sidebar.text_input(key="ai_api_key").input(SESSION_KEY).run()
            self.button(app, RSS_BUTTON).click().run()
        self.assertEqual(len(app.exception), 0)
        self.store.mark_error.assert_called_once_with(1, "AnalyzerReloadRequired")
        self.assertNotIn(SESSION_KEY, self.rendered_text(app))
        self.analyze.assert_not_called()
        self.save.assert_not_called()

    def test_investat_is_password_protected_and_opt_in(self):
        app = self.app()
        field = app.sidebar.text_input(key="investat_token")
        self.assertEqual(field.label, "Investat MCP token")
        self.assertEqual(field.proto.type, TextInput.PASSWORD)
        self.assertEqual(field.value, "")
        self.assertEqual(app.multiselect(key="news_sources").value, ["test"])
        self.assertTrue(app.number_input[0].disabled)
        self.button(app, "抓取最新新聞").click().run()
        self.collect.assert_called_once()
        self.investat.assert_not_called()
        self.analyze.assert_not_called()

    def test_investat_collection_uses_its_token_and_limit_and_merges_source_outcomes(self):
        self.collect.return_value = {
            "added": 1, "updated": 1, "duplicates": 2,
            "sources": [{"id": "test", "name": "Synthetic feed", "status": "error",
                         "error": "HTTP 403"}],
        }
        self.investat.return_value = {
            "added": 2, "updated": 3, "duplicates": 4, "failed_sources": 0,
            "sources": [{"id": "investat", "name": "Investat", "status": "partial",
                         "error": "Only provider fragments were available"}],
        }
        app = self.app()
        app.sidebar.text_input(key="investat_token").input("  " + INVESTAT_TOKEN + "  ").run()
        app.multiselect(key="news_sources").set_value(["test", "investat"]).run()
        self.assertFalse(app.number_input[0].disabled)
        app.number_input[0].set_value(7).run()
        self.assert_analysis_disabled(app)
        self.button(app, "抓取最新新聞").click().run()
        self.assertEqual(len(app.exception), 0)
        self.collect.assert_called_once()
        self.assertEqual([s.id for s in self.collect.call_args.args[0]], ["test"])
        self.assertIs(self.collect.call_args.args[1], self.store)
        self.investat.assert_called_once_with(self.store, token=INVESTAT_TOKEN, limit=7)
        self.assertIn("新增 3 則，更新 4 則，重複 6 則", self.rendered_text(app))
        self.assertIn("Synthetic feed：HTTP 403", self.rendered_text(app))
        self.assertIn("Investat：Only provider fragments were available", self.rendered_text(app))
        self.assertNotIn(INVESTAT_TOKEN, self.rendered_text(app))
        self.analyze.assert_not_called()
        self.pending.assert_not_called()
        self.assertEqual(os.environ["INVESTAT_MCP_TOKEN"], "")
        for name in PROVIDER_KEY_VARIABLES:
            self.assertEqual(os.environ[name], "")

    def test_investat_token_is_separate_from_llm_credentials_and_provider_changes(self):
        app = self.app()
        app.sidebar.text_input(key="investat_token").input(INVESTAT_TOKEN).run()
        self.assert_analysis_disabled(app)
        app.sidebar.text_input(key="ai_api_key").input(SESSION_KEY).run()
        self.select_provider(app, "openai")
        self.assertEqual(app.sidebar.text_input(key="ai_api_key").value, "")
        self.assertEqual(app.sidebar.text_input(key="investat_token").value, INVESTAT_TOKEN)
        self.assert_analysis_disabled(app)
        app.sidebar.text_input(key="ai_api_key").input(SESSION_KEY).run()
        self.button(app, "清除輸入的 Investat token").click().run()
        self.assertEqual(app.sidebar.text_input(key="investat_token").value, "")
        self.assertEqual(app.sidebar.text_input(key="ai_api_key").value, SESSION_KEY)
        app.multiselect(key="news_sources").set_value(["investat"]).run()
        self.button(app, "抓取最新新聞").click().run()
        self.investat.assert_called_once_with(self.store, token="", limit=20)
        self.analyze.assert_not_called()

    def test_collection_forwards_explicit_investat_search_filters(self):
        app = self.app()
        app.sidebar.text_input(key="investat_token").input(INVESTAT_TOKEN).run()
        app.multiselect(key="news_sources").set_value(["investat"]).run()
        app.text_input(key="investat_collect_search").input("AI capex").run()
        app.text_input(key="investat_collect_ticker").input("NVDA").run()
        app.text_input(key="investat_collect_institution").input("Morgan Stanley").run()
        self.investat.assert_not_called()
        self.button(app, "抓取最新新聞").click().run()
        self.assertEqual(len(app.exception), 0)
        self.investat.assert_called_once_with(self.store, token=INVESTAT_TOKEN, limit=20,
            search="AI capex", ticker="NVDA", institution="Morgan Stanley")
        self.assertNotIn(INVESTAT_TOKEN, self.rendered_text(app))

    def test_investat_sessions_are_isolated_and_clear_restores_hidden_environment_fallback(self):
        os.environ["INVESTAT_MCP_TOKEN"] = SERVER_INVESTAT_TOKEN
        first = self.app()
        first.sidebar.text_input(key="investat_token").input(INVESTAT_TOKEN).run()
        second = self.app()
        self.assertEqual(second.sidebar.text_input(key="investat_token").value, "")
        first.run()
        self.assertEqual(first.sidebar.text_input(key="investat_token").value, INVESTAT_TOKEN)
        first.multiselect(key="news_sources").set_value(["investat"]).run()
        self.button(first, "抓取最新新聞").click().run()
        self.investat.assert_called_once_with(self.store, token=INVESTAT_TOKEN, limit=20)
        self.investat.reset_mock()
        self.button(first, "清除輸入的 Investat token").click().run()
        self.assertEqual(first.session_state["investat_token"], "")
        self.button(first, "抓取最新新聞").click().run()
        self.investat.assert_called_once_with(self.store, token=SERVER_INVESTAT_TOKEN, limit=20)
        self.assertNotIn(SERVER_INVESTAT_TOKEN, self.rendered_text(first))
        self.assertNotIn(INVESTAT_TOKEN, self.rendered_text(first))
        self.assert_analysis_disabled(first)
        self.assertEqual(os.environ["INVESTAT_MCP_TOKEN"], SERVER_INVESTAT_TOKEN)

    def test_investat_without_article_url_links_only_to_provider_homepage(self):
        self.store.export_articles.return_value = [{
            "title": "Institutional research", "source_id": "investat",
            "external_id": "research_1", "url": None, "published_at": None,
            "summary": "A provider supplied excerpt.", "content": "",
            "content_scope": "fragment", "sources": [{"source_name": "Investat"}],
        }]
        app = self.app()
        links = [(link.label, link.url) for link in app.get("link_button")]
        self.assertIn(("前往 Investat", "https://investat.ai/"), links)
        self.assertFalse(any(label == "閱讀原文" for label, _ in links))
        self.assertIn("research_1（接口未提供文章連結）", self.rendered_text(app))


if __name__ == "__main__":
    unittest.main()

