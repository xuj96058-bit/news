"""Full-app report display/edit/delete flows against temporary SQLite stores.

The report renderer, editor, navigation and persistence are real. Network calls
and AI boundaries fail closed; generation returns a deterministic test draft.
"""

import copy
from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

with patch("dotenv.load_dotenv"):
    from src.feeds import Source
    from src.news_store import NewsStore
    from src.research_store import ResearchStore


APP = Path(__file__).resolve().parents[1] / "app.py"


class ReportGalleryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.report_path = Path(self.temp.name) / "reports.db"
        self.news_path = Path(self.temp.name) / "news.db"
        self.patch("dotenv.load_dotenv")
        self.start_patch(patch.dict(os.environ, {
            "LLM_PROVIDER": "deepseek", "DEEPSEEK_API_KEY": "", "OPENAI_API_KEY": "",
            "XAI_API_KEY": "", "ANTHROPIC_API_KEY": "", "GEMINI_API_KEY": "",
            "GOOGLE_API_KEY": "", "INVESTAT_MCP_TOKEN": "", "OPENAI_BASE_URL": "",
        }))
        # ResearchStore resolves this default on every construction, including
        # app.py, the editor and the new gallery's imported class references.
        self.patch("src.research_store.DEFAULT_DB_PATH", new=self.report_path)
        self.patch("src.news_store.NewsStore", side_effect=lambda: NewsStore(self.news_path))
        self.patch("src.research_ui.NewsStore", side_effect=lambda: NewsStore(self.news_path))
        sources = [Source("synthetic", "Synthetic public source", "https://example.org/feed", "media", "global")]
        self.patch("src.feeds.load_sources", return_value=sources)
        self.patch("src.research_ui.load_sources", return_value=sources)
        self.patch("src.report.load_report_events", return_value=[])
        self.patch("src.workbench_ui.render_reader_tab")
        self.patch("src.workbench_ui.render_source_operations")
        self.patch("src.chartbook_ui.render_chartbook_tab")
        self.patch("src.research_ui.export_pdf", return_value=b"synthetic-pdf-bytes")
        self.patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected network call"))
        self.patch("openai.OpenAI", side_effect=AssertionError("Unexpected provider call"))
        self.collect = self.patch("src.rss_pipeline.collect_sources", side_effect=AssertionError("Unexpected collection"))
        self.generator = self.patch("src.research_ui.generate_research_workflow", side_effect=AssertionError("Unexpected generation"))

    def start_patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def patch(self, target, **kwargs):
        return self.start_patch(patch(target, **kwargs))

    def create_report(self, title, body=None):
        with ResearchStore(self.report_path) as store:
            return store.create_report(topic=title, title=title, body=body or title + " saved body",
                                       sources=[], provider="deepseek", model="deepseek-v4-pro",
                                       as_of="2026-10-16")

    def app(self):
        app = AppTest.from_file(str(APP), default_timeout=15).run()
        self.assertFalse(app.exception)
        return app

    def navigate(self, app, label):
        app.session_state["main_navigation"] = label
        app.run()
        self.assertFalse(app.exception)

    def gallery(self, app):
        return next(tab for tab in app.get_by_key("main_navigation").children.values()
                    if tab.label == "報告展示")

    def gallery_text(self, app):
        gallery = self.gallery(app)
        return "\n".join(str(element.value) for kind in
                         ("markdown", "text", "caption", "info", "warning", "success", "subheader")
                         for element in gallery.get(kind))

    def read_report(self, app, report_id):
        self.navigate(app, "報告展示")
        app.button(key=f"report_gallery_read_{report_id}").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["report_gallery_selected"], report_id)

    def test_seventh_top_level_tab_displays_existing_report_without_ai(self):
        report = self.create_report("Existing gallery report", "Unique saved gallery body")
        app = self.app()
        labels = [tab.label for tab in app.get_by_key("main_navigation").children.values()]
        self.assertEqual(labels, ["新聞查詢", "日報", "新聞來源", "研究閱讀", "圖表庫", "研究報告", "報告展示"])
        self.read_report(app, report["id"])
        self.assertIn("Unique saved gallery body", self.gallery_text(app))
        self.generator.assert_not_called()
        self.collect.assert_not_called()

    def test_generation_opens_new_saved_report_in_gallery(self):
        self.create_report("Older report", "An older saved body")
        with NewsStore(self.news_path) as store:
            store.ingest([{"source_id": "synthetic", "source_name": "Synthetic public source",
                           "source_kind": "media", "region": "global", "title": "AI spending evidence",
                           "url": "https://example.org/synthetic-ai-evidence", "summary": "AI spending increased.",
                           "content_scope": "summary", "published_at": datetime.now(timezone.utc).isoformat()}])
        initial = {"title": "New gallery draft", "body": "# New gallery draft\n\nOriginal test body [S1].",
                   "metadata": {"workflow_stage": "writer_saved", "review_status": "pending"}}
        reviewed = {"title": "New gallery reviewed", "body": "# New gallery reviewed\n\nNew reviewed gallery body [S1].",
                    "metadata": {"workflow_stage": "review_complete", "review_status": "reviewed",
                                 "review_findings": [], "review_summary": "Deterministic test review"}}
        def generate(*args, on_draft=None, on_progress=None, **kwargs):
            on_draft(copy.deepcopy(initial))
            on_progress("reviewing", 0)
            return copy.deepcopy(reviewed)
        self.generator.side_effect = generate
        app = self.app()
        self.navigate(app, "研究報告")
        app.sidebar.text_input(key="ai_api_key").input("synthetic-session-key").run()
        app.radio(key="research_report_mode").set_value("資料缺口簡報").run()
        app.text_input(key="research_topic").input("AI spending").run()
        app.button(key="research_select_articles").click().run()
        app.button(key="research_generate").click().run()
        self.assertFalse(app.exception)
        with ResearchStore(self.report_path) as store:
            saved = store.list_reports()[0]
        self.assertEqual(saved["body"], reviewed["body"])
        self.assertEqual(app.session_state["main_navigation"], "報告展示")
        self.assertEqual(app.session_state["report_gallery_selected"], saved["id"])
        self.assertIn("New reviewed gallery body", self.gallery_text(app))
        self.generator.assert_called_once()

    def test_gallery_edit_handoff_and_saved_revision_refresh_display(self):
        report = self.create_report("Editable report", "Before manual editing")
        app = self.app()
        self.read_report(app, report["id"])
        app.button(key="report_gallery_detail_edit").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["main_navigation"], "研究報告")
        self.assertEqual(app.session_state["research_report_view"], "報告庫與編輯")
        self.assertEqual(app.session_state["research_active_report"], report["id"])
        app.text_input(key=f"research_editor_title_{report['id']}").input("Revised report title").run()
        app.text_area(key=f"research_editor_body_{report['id']}").input("Updated saved body visible in gallery").run()
        app.button(key="research_save_edit").click().run()
        self.assertFalse(app.exception)
        self.navigate(app, "報告展示")
        self.assertIn("Updated saved body visible in gallery", self.gallery_text(app))
        self.assertNotIn("Before manual editing", self.gallery_text(app))
        with ResearchStore(self.report_path) as store:
            saved = store.get_report(report["id"])
        self.assertEqual(saved["version"], 2)
        self.assertEqual(saved["original_body"], "Before manual editing")
        self.generator.assert_not_called()

    def test_imported_markdown_opens_saved_report_in_gallery_without_ai(self):
        app = self.app()
        self.navigate(app, "研究報告")
        app.file_uploader(key="research_import_file").set_value(
            ("imported-report.md", b"# Imported report\n\nImported saved gallery body.", "text/markdown")).run()
        app.button(key="research_import").click().run()
        self.assertFalse(app.exception)
        with ResearchStore(self.report_path) as store:
            saved = store.list_reports()[0]
        self.assertEqual(saved["provider"], "import")
        self.assertEqual(app.session_state["main_navigation"], "報告展示")
        self.assertEqual(app.session_state["report_gallery_selected"], saved["id"])
        self.assertIn("Imported saved gallery body", self.gallery_text(app))
        self.generator.assert_not_called()

    def test_confirmed_delete_clears_database_editor_draft_and_pdf_cache(self):
        report = self.create_report("Delete this report", "Body to remove from gallery")
        survivor = self.create_report("Keep this report", "Surviving report body")
        app = self.app()
        self.read_report(app, report["id"])
        app.button(key="report_gallery_detail_edit").click().run()
        app.text_area(key=f"research_editor_body_{report['id']}").input("Unsaved deleted draft").run()
        app.button(key="research_prepare_pdf").click().run()
        self.assertEqual(app.session_state["research_pdf"]["id"], report["id"])
        self.assertIn(report["id"], app.session_state["research_editor_drafts"])
        app.session_state["daily_essay_report_id"] = report["id"]
        app.session_state["daily_essay"] = {"body": "Cached deleted daily essay"}
        self.navigate(app, "報告展示")
        app.button(key="report_gallery_detail_delete").click().run()
        self.assertFalse(app.exception)
        with ResearchStore(self.report_path) as store:
            self.assertIsNotNone(store.get_report(report["id"]))
        app.button(key="report_gallery_confirm_delete").click().run()
        self.assertFalse(app.exception)
        with ResearchStore(self.report_path) as store:
            self.assertIsNone(store.get_report(report["id"]))
            self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM research_revisions WHERE report_id=?", (report["id"],)).fetchone()[0], 0)
            self.assertIsNotNone(store.get_report(survivor["id"]))
        self.assertNotIn(report["id"], app.session_state["research_editor_drafts"])
        for key in ("research_pdf", "daily_essay", "daily_essay_report_id"):
            self.assertNotIn(key, app.session_state)
        self.assertNotEqual(app.session_state["research_active_report"], report["id"])
        self.assertNotIn("Body to remove from gallery", self.gallery_text(app))
        self.navigate(app, "研究報告")
        self.assertNotEqual(app.selectbox(key="research_active_report").value, report["id"])
        self.assertNotIn("Delete this report", " ".join(app.selectbox(key="research_active_report").options))
        self.generator.assert_not_called()

    def test_gallery_can_open_and_edit_report_older_than_library_page(self):
        oldest = self.create_report("Unique oldest report", "Old report outside latest hundred")
        for number in range(105):
            self.create_report(f"Recent report {number:03d}")
        app = self.app()
        self.navigate(app, "報告展示")
        app.text_input(key="report_gallery_query").input("Unique oldest report")
        app.button(key="report_gallery_search_submit").click().run()
        app.button(key=f"report_gallery_read_{oldest['id']}").click().run()
        app.button(key="report_gallery_detail_edit").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["research_active_report"], oldest["id"])
        self.assertEqual(app.text_area(key=f"research_editor_body_{oldest['id']}").value, "Old report outside latest hundred")
        app.text_area(key=f"research_editor_body_{oldest['id']}").input("Edited oldest report body").run()
        app.button(key="research_save_edit").click().run()
        self.assertFalse(app.exception)
        with ResearchStore(self.report_path) as store:
            self.assertEqual(store.get_report(oldest["id"])["body"], "Edited oldest report body")
        self.generator.assert_not_called()


if __name__ == "__main__":
    unittest.main()
