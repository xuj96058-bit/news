"""Interactive reader/chart workflows using temporary stores and no external APIs."""
from datetime import date
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

with patch("dotenv.load_dotenv"):
    from src.news_store import NewsStore
    from src.chartbook import ChartbookStore
    from src.investat import InvestatError
    from src.research_store import ResearchStore


def reader_script():
    from src.workbench_ui import render_reader_tab
    render_reader_tab(provider="deepseek", model="deepseek-test", api_key="synthetic-key", analysis_available=True)


def chartbook_script():
    from src.chartbook_ui import render_chartbook_tab
    render_chartbook_tab()


def investat_script():
    from src.workbench_ui import render_investat_filters
    render_investat_filters("synthetic-token", True)


def operations_script():
    from src.workbench_ui import render_source_operations
    from src.feeds import Source
    render_source_operations([Source("test", "Test source", "https://example.org/feed", "media", "us")],
                             investat_token="synthetic-token")


class WorkbenchUiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.news_path = Path(self.temp.name) / "news.db"
        self.chart_path = Path(self.temp.name) / "charts.db"
        self.patch("dotenv.load_dotenv")
        self.patch("src.workbench_ui.NewsStore", side_effect=lambda: NewsStore(self.news_path))
        self.patch("src.chartbook_ui.NewsStore", side_effect=lambda: NewsStore(self.news_path))
        self.patch("src.chartbook_ui.ChartbookStore", side_effect=lambda: ChartbookStore(self.chart_path))
        self.ai = self.patch("src.workbench.organize_comparison")
        self.patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected network call"))

    def patch(self, target, **kwargs):
        item = patch(target, **kwargs)
        value = item.start()
        self.addCleanup(item.stop)
        return value

    def seed(self, count=1):
        with NewsStore(self.news_path) as store:
            store.ingest([{"source_id": "institution", "source_name": "Institution feed", "source_kind": "research",
                "title": f"AI spending view {i}", "url": f"https://example.org/{i}", "summary": "AI spending remains elevated.",
                "content_scope": "summary", "published_at": "2026-09-20T00:00:00+00:00",
                "institution": "Morgan Stanley", "publisher": "Morgan Stanley", "provider": "Investat",
                "tickers": ["NVDA"], "asset_classes": ["equities"], "topics": ["AI"]} for i in range(count)])

    def app(self, script):
        result = AppTest.from_function(script, default_timeout=10).run()
        self.assertFalse(result.exception)
        return result

    def rendered(self, app):
        return "\n".join(str(item.value) for kind in ("caption", "info", "success", "warning", "error", "markdown")
                         for item in app.get(kind))

    def test_alias_filters_watchlist_survive_new_session(self):
        self.seed(2)
        app = self.app(reader_script)
        app.text_input(key="reader_query").input("AI")
        app.text_input(key="reader_institution").input("摩根士丹利")
        app.text_input(key="reader_ticker").input("nvda")
        app.text_input(key="reader_asset_class").input("equities")
        app.text_input(key="reader_topic").input("AI")
        app.date_input(key="reader_start_date").set_value(date(2026, 9, 1))
        app.date_input(key="reader_end_date").set_value(date(2026, 9, 30))
        app.button(key="reader_search").click().run()
        self.assertFalse(app.exception)
        self.assertIn("共 2 則", self.rendered(app))
        app.text_input(key="reader_search_name").input("AI 半導體")
        app.button(key="reader_save_search").click().run()
        self.assertFalse(app.exception)
        second = self.app(reader_script)
        second.button(key="reader_apply_search").click().run()
        self.assertFalse(second.exception)
        self.assertEqual(second.text_input(key="reader_institution").value, "Morgan Stanley")
        self.assertEqual(second.text_input(key="reader_ticker").value, "NVDA")
        self.assertEqual(second.date_input(key="reader_start_date").value, date(2026, 9, 1))
        self.assertIn("共 2 則", self.rendered(second))
        second.button(key="reader_delete_search").click().run()
        with NewsStore(self.news_path) as store:
            self.assertEqual(store.list_saved_searches(), [])
        self.ai.assert_not_called()

    def test_history_pagination_keeps_comparison_basket_without_calling_ai(self):
        self.seed(61)
        app = self.app(reader_script)
        self.assertIn("共 61 則", self.rendered(app))
        # AppTest options are displayed labels; choose the underlying stored ID.
        with NewsStore(self.news_path) as store:
            first = store.search_articles(limit=1)["items"][0]["id"]
        app.multiselect(key="reader_page_selection_0").set_value([first]).run()
        app.button(key="reader_add_comparison").click().run()
        self.assertFalse(app.exception)
        app.button(key="reader_next").click().run()
        app.button(key="reader_next").click().run()
        self.assertFalse(app.exception)
        self.assertIn("第 3／3 頁", self.rendered(app))
        self.assertEqual(app.session_state["reader_basket"], [first])
        app.button(key="reader_to_report").click().run()
        self.assertEqual(app.session_state["research_pending_article_ids"], [first])
        self.assertTrue(app.button(key="reader_next").disabled)
        self.assertEqual(len(app.multiselect(key="reader_page_selection_2").options), 11)
        app.text_input(key="reader_query").input("notfound")
        app.button(key="reader_search").click().run()
        self.assertFalse(app.exception)
        self.assertIn("沒有符合條件", self.rendered(app))
        self.assertEqual(app.session_state["reader_basket"], [first])
        self.ai.assert_not_called()

    def test_invalid_ticker_keeps_previous_search_and_does_not_crash(self):
        self.seed(2)
        app = self.app(reader_script)
        app.text_input(key="reader_query").input("AI")
        app.button(key="reader_search").click().run()
        previous = dict(app.session_state["reader_filters"])
        app.text_input(key="reader_ticker").input("not a ticker")
        app.button(key="reader_search").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["reader_filters"], previous)
        self.assertIn("已保留上次有效搜尋", self.rendered(app))
        self.assertIn("共 2 則", self.rendered(app))

    def test_comparison_calls_ai_only_on_explicit_button_and_persists(self):
        self.seed()
        with NewsStore(self.news_path) as store:
            article = store.search_articles()["items"][0]
        self.ai.return_value = [{"article_id": article["id"], "content_hash": article["content_hash"],
            "institution": "Morgan Stanley", "title": article["title"], "published_at": article["published_at"],
            "judgement": "AI spending remains elevated", "evidence": "AI spending remains elevated.",
            "conditions": "", "change": "", "source_url": article["url"], "status": "已提取，待人工核對"}]
        app = self.app(reader_script)
        app.multiselect(key="reader_page_selection_0").set_value([article["id"]]).run()
        app.button(key="reader_add_comparison").click().run()
        app.text_input(key="reader_comparison_topic").input("AI spending").run()
        self.ai.assert_not_called()
        app.button(key="reader_organize").click().run()
        self.assertFalse(app.exception)
        self.ai.assert_called_once()
        self.assertEqual(self.ai.call_args.kwargs["api_key"], "synthetic-key")
        app.run()
        self.ai.assert_called_once()
        self.assertEqual(app.dataframe[0].value.iloc[0]["核心判斷"], "AI spending remains elevated")

    def test_investat_filters_are_explicit_and_errors_never_echo_token(self):
        client = self.patch("src.workbench_ui.InvestatClient")
        client.return_value.get_filters.return_value = {"institutions": [{"name": "Morgan Stanley"}], "tickers": ["NVDA"]}
        app = self.app(investat_script)
        app.text_input(key="investat_collect_search").input("AI").run()
        client.assert_not_called()
        app.button(key="investat_load_filters").click().run()
        client.assert_called_once_with(token="synthetic-token")
        self.assertIn("Morgan Stanley", self.rendered(app))
        app.run()
        client.assert_called_once()
        client.return_value.get_filters.side_effect = RuntimeError("Bearer synthetic-token raw-response-secret")
        app.button(key="investat_load_filters").click().run()
        self.assertFalse(app.exception)
        self.assertNotIn("synthetic-token", self.rendered(app))
        self.assertNotIn("raw-response-secret", self.rendered(app))

    def _chart_fields(self, app):
        for key, value in {"title": "AI revenue", "publisher": "Synthetic source", "source_url": "https://example.org/data",
                           "metric": "Revenue", "unit": "USD billion", "csv": "date,value\n2026-08-01,10\n2026-09-01,12"}.items():
            widget = app.text_area if key == "csv" else app.text_input
            widget(key="chartbook_" + key).input(value)

    def test_chart_create_evidence_handoff_and_archive(self):
        app = self.app(chartbook_script)
        self._chart_fields(app)
        app.button(key="chartbook_save").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(len(app.dataframe), 1)
        with ChartbookStore(self.chart_path) as store:
            chart = store.list_charts()["items"][0]
        self.assertEqual(chart["observation_start"], "2026-08-01")
        self.assertEqual(chart["verification"], "unverified")
        app.run()
        app.button(key="chartbook_to_report").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(app.session_state["research_use_evidence"])
        self.assertEqual(app.session_state["research_manual_evidence"][0]["chart_id"], chart["id"])
        app.button(key="chartbook_to_report").click().run()
        self.assertEqual(len(app.session_state["research_manual_evidence"]), 1)
        app.button(key="chartbook_archive").click().run()
        self.assertFalse(app.exception)
        with ChartbookStore(self.chart_path) as store:
            self.assertEqual(store.list_charts()["total"], 0)
            self.assertTrue(store.get_chart(chart["id"])["archived"])
        self.ai.assert_not_called()

    def test_invalid_csv_does_not_create_chart(self):
        app = self.app(chartbook_script)
        self._chart_fields(app)
        app.text_area(key="chartbook_csv").input("date,value\n2026-08-01,nan")
        app.button(key="chartbook_save").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(app.warning)
        with ChartbookStore(self.chart_path) as store:
            self.assertEqual(store.list_charts()["total"], 0)

    def test_schedule_is_saved_without_credentials_and_runs_only_on_click(self):
        from src.scheduler import list_schedules
        runner = self.patch("src.scheduler.run_due", return_value=[])
        app = self.app(operations_script)
        self.assertEqual(len(app.dataframe[0].value), 3)
        self.assertTrue((app.dataframe[0].value["最後狀態"] == "尚無紀錄").all())
        app.selectbox(key="schedule_source").select("investat").run()
        app.text_input(key="schedule_investat_search").input("AI")
        app.text_input(key="schedule_investat_ticker").input("NVDA")
        app.number_input(key="schedule_interval_investat").set_value(120)
        app.button(key="schedule_save").click().run()
        self.assertFalse(app.exception)
        runner.assert_not_called()
        with NewsStore(self.news_path) as store:
            rows = list_schedules(store)
        self.assertEqual(rows[0]["interval_minutes"], 120)
        self.assertEqual(rows[0]["options"]["ticker"], "NVDA")
        self.assertNotIn("synthetic-token", str(rows))
        app.run()
        runner.assert_not_called()
        app.button(key="schedule_run_due").click().run()
        self.assertFalse(app.exception)
        runner.assert_called_once()
        self.assertEqual(runner.call_args.kwargs["investat_token"], "synthetic-token")

    def test_full_app_opens_empty_and_offline_without_fetch_or_model_calls(self):
        report_path = Path(self.temp.name) / "reports.db"
        self.patch("src.news_store.NewsStore", side_effect=lambda: NewsStore(self.news_path))
        self.patch("src.research_ui.NewsStore", side_effect=lambda: NewsStore(self.news_path))
        self.patch("src.research_ui.ResearchStore", side_effect=lambda: ResearchStore(report_path))
        self.patch("src.research_store.ResearchStore", side_effect=lambda: ResearchStore(report_path))
        self.patch("src.report.load_report_events", return_value=[])
        self.patch("src.feeds.load_sources", return_value=[])
        self.patch("src.research_ui.load_sources", return_value=[])
        collect = self.patch("src.rss_pipeline.collect_sources")
        client = self.patch("src.workbench_ui.InvestatClient")
        with patch.dict(os.environ, {"LLM_PROVIDER": "deepseek", "DEEPSEEK_API_KEY": "", "INVESTAT_MCP_TOKEN": ""}):
            app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=15).run()
            self.assertFalse(app.exception)
            app.run()
            self.assertFalse(app.exception)
        self.assertEqual([tab.label for tab in app.tabs][:6], ["新聞查詢", "日報", "新聞來源", "研究閱讀", "圖表庫", "研究報告"])
        client.assert_not_called()
        collect.assert_not_called()
        self.ai.assert_not_called()


if __name__ == "__main__":
    unittest.main()
