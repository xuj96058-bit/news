"""Native report gallery interactions against temporary local report libraries."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from src.research_store import ResearchStore
from src.research_export import export_markdown


def gallery_script():
    from src.report_gallery_ui import render_report_gallery
    render_report_gallery()


class ReportGalleryUiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "reports.db"
        self.patch("src.report_gallery_ui.ResearchStore", side_effect=lambda: ResearchStore(self.path))
        self.patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected network request"))
        self.export = self.patch("src.report_gallery_ui.export_markdown", wraps=export_markdown)

    def patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def create(self, title="AI 研究報告", body="已保存的研究正文。", metadata=None, sources=None):
        with ResearchStore(self.path) as store:
            return store.create_report(topic=title, title=title, body=body, sources=sources or [],
                provider="deepseek", model="synthetic-model", as_of="2026-10-16", metadata=metadata)

    def app(self):
        app = AppTest.from_function(gallery_script, default_timeout=10).run()
        self.assertFalse(app.exception)
        return app

    def rendered(self, app):
        return "\n".join(str(item.value) for kind in ("caption", "info", "success", "warning", "markdown", "text")
                         for item in app.get(kind))

    def state(self, app):
        return app.session_state.to_dict()

    def test_empty_gallery_has_working_generation_and_import_navigation(self):
        app = self.app()
        self.assertIn("尚未保存報告", self.rendered(app))
        app.button(key="report_gallery_new").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["main_navigation"], "研究報告")
        self.assertEqual(app.session_state["research_report_view"], "生成新報告")
        app.button(key="report_gallery_import").click().run()
        self.assertEqual(app.session_state["research_report_view"], "報告庫與編輯")
        self.assertEqual(len(app.get("download_button")), 0)

    def test_saved_reports_appear_automatically_and_read_latest_saved_version(self):
        original = self.create(sources=[{"id": "S1", "title": "原始資料", "publisher": "Synthetic source",
            "content": "來源證據原文。", "url": "https://example.org/evidence"}])
        app = self.app()
        self.assertIn("共 1 份報告", self.rendered(app))
        self.create(title="另一份報告", metadata={"report_mode": "gap"})
        with ResearchStore(self.path) as store:
            saved = store.save_revision(original["id"], title="修訂後報告", body="最新保存正文。<script>unsafe()</script>", expected_version=1)
        app.session_state["research_editor_drafts"] = {original["id"]: {"body": "尚未保存的私人編輯", "base": 2}}
        app.run()
        self.assertIn("共 2 份報告", self.rendered(app))
        app.button(key="report_gallery_read_" + original["id"]).click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["report_gallery_selected"], original["id"])
        self.assertIn("最新保存正文", self.rendered(app))
        self.assertIn("來源證據原文", self.rendered(app))
        self.assertNotIn("尚未保存的私人編輯", self.rendered(app))
        self.assertEqual(self.export.call_args.args[0]["body"], saved["body"])
        self.assertEqual(len(app.get("download_button")), 2)
        self.assertTrue(all(not item.proto.allow_html for item in app.markdown))
        app.button(key="report_gallery_detail_edit").click().run()
        self.assertEqual(app.session_state["main_navigation"], "研究報告")
        self.assertEqual(app.session_state["research_report_view"], "報告庫與編輯")
        self.assertEqual(app.session_state["research_active_report"], original["id"])

    def test_full_library_search_and_pagination_reaches_reports_older_than_one_hundred(self):
        oldest = self.create(title="最早的獨立報告", body="historicalneedle")
        for index in range(105):
            self.create(title=f"近期報告 {index}")
        app = self.app()
        self.assertEqual(len(app.checkbox), 12)
        self.assertIn("共 106 份報告", self.rendered(app))
        app.button(key="report_gallery_next").click().run()
        self.assertIn("第 2／9 頁", self.rendered(app))
        app.selectbox(key="report_gallery_page_size").select(6).run()
        self.assertIn("第 1／18 頁", self.rendered(app))
        app.text_input(key="report_gallery_query").input("historicalneedle")
        app.button(key="report_gallery_search_submit").click().run()
        self.assertFalse(app.exception)
        self.assertIn("共 1 份報告", self.rendered(app))
        app.button(key="report_gallery_edit_" + oldest["id"]).click().run()
        self.assertEqual(app.session_state["research_active_report"], oldest["id"])
        self.assertTrue(app.button(key="report_gallery_next").disabled)

    def test_cancel_and_new_selection_do_not_replace_pending_delete_snapshot(self):
        first = self.create(title="第一份", body="firstbody")
        second = self.create(title="第二份", body="secondbody")
        app = self.app()
        app.button(key="report_gallery_delete_" + first["id"]).click().run()
        snapshot = [dict(row) for row in app.session_state["report_gallery_delete_plan"]]
        self.assertEqual(snapshot, [{"id": first["id"], "title": "第一份", "version": 1}])
        self.assertIn("來源快照及全部歷史版本", self.rendered(app))
        self.assertIn("已下載至其他位置的檔案不受影響", self.rendered(app))
        app.checkbox(key="report_gallery_check_" + second["id"]).check().run()
        app.button(key="report_gallery_delete_selected").click().run()
        self.assertEqual(app.session_state["report_gallery_delete_plan"], snapshot)
        app.text_input(key="report_gallery_query").input("secondbody")
        app.button(key="report_gallery_search_submit").click().run()
        self.assertEqual(app.session_state["report_gallery_delete_plan"], snapshot)
        app.button(key="report_gallery_cancel_delete").click().run()
        self.assertNotIn("report_gallery_delete_plan", self.state(app))
        with ResearchStore(self.path) as store:
            self.assertEqual(store.search_reports()["total"], 2)
        app.button(key="report_gallery_delete_" + second["id"]).click().run()
        self.assertEqual(app.session_state["report_gallery_delete_plan"][0]["id"], second["id"])

    def test_batch_conflict_keeps_every_report_revision_and_session_draft(self):
        first, second = self.create(title="A"), self.create(title="B")
        app = self.app()
        app.session_state["research_editor_drafts"] = {first["id"]: {"body": "unsaved draft"}}
        app.button(key="report_gallery_select_all").click().run()
        app.button(key="report_gallery_delete_selected").click().run()
        snapshot = [dict(row) for row in app.session_state["report_gallery_delete_plan"]]
        with ResearchStore(self.path) as store:
            store.save_revision(second["id"], title="B 新版", body="new body", expected_version=1)
        app.button(key="report_gallery_confirm_delete").click().run()
        self.assertFalse(app.exception)
        self.assertIn("本次完全未刪除", self.rendered(app))
        self.assertEqual(app.session_state["report_gallery_delete_plan"], snapshot)
        self.assertIn(first["id"], app.session_state["research_editor_drafts"])
        with ResearchStore(self.path) as store:
            self.assertEqual(store.search_reports()["total"], 2)
            self.assertEqual(len(store.list_revisions(first["id"])), 1)
            self.assertEqual(len(store.list_revisions(second["id"])), 2)

    def test_search_after_staging_still_deletes_only_the_original_plan(self):
        first = self.create(title="第一份", body="firstbody")
        second = self.create(title="第二份", body="secondbody")
        app = self.app()
        app.button(key="report_gallery_delete_" + first["id"]).click().run()
        app.text_input(key="report_gallery_query").input("secondbody")
        app.button(key="report_gallery_search_submit").click().run()
        self.assertEqual(len(app.checkbox), 1)
        app.checkbox(key="report_gallery_check_" + second["id"]).check().run()
        app.button(key="report_gallery_confirm_delete").click().run()
        self.assertFalse(app.exception)
        with ResearchStore(self.path) as store:
            self.assertIsNone(store.get_report(first["id"]))
            self.assertIsNotNone(store.get_report(second["id"]))

    def test_confirm_delete_clears_local_history_and_only_related_editor_caches(self):
        doomed, kept = self.create(title="刪除目標"), self.create(title="保留報告")
        with ResearchStore(self.path) as store:
            doomed = store.save_revision(doomed["id"], title="刪除目標最新版", body="version two", expected_version=1)
        exported = Path(self.temp.name) / "downloaded-report.md"
        exported.write_text("already downloaded", encoding="utf-8")
        app = self.app()
        for field in ("base", "title", "body"):
            app.session_state[f"research_editor_{field}_{doomed['id']}"] = "temporary"
        app.session_state["research_editor_drafts"] = {doomed["id"]: {"body": "remove"}, kept["id"]: {"body": "keep"}}
        app.session_state["research_active_report"] = doomed["id"]
        app.session_state["research_pdf"] = {"id": doomed["id"], "data": b"cached-pdf"}
        app.session_state["daily_essay"] = {"body": "cached daily essay"}
        app.session_state["daily_essay_report_id"] = doomed["id"]
        app.button(key="report_gallery_read_" + doomed["id"]).click().run()
        app.button(key="report_gallery_detail_delete").click().run()
        app.button(key="report_gallery_confirm_delete").click().run()
        self.assertFalse(app.exception)
        self.assertIn("已永久刪除 1 份", self.rendered(app))
        self.assertNotIn("report_gallery_selected", self.state(app))
        self.assertNotIn("research_active_report", self.state(app))
        self.assertNotIn("research_pdf", self.state(app))
        self.assertNotIn("daily_essay", self.state(app))
        self.assertNotIn("daily_essay_report_id", self.state(app))
        self.assertEqual(app.session_state["research_editor_drafts"], {kept["id"]: {"body": "keep"}})
        for field in ("base", "title", "body"):
            self.assertNotIn(f"research_editor_{field}_{doomed['id']}", self.state(app))
        with ResearchStore(self.path) as store:
            self.assertIsNone(store.get_report(doomed["id"]))
            self.assertEqual(store.list_revisions(doomed["id"]), [])
            self.assertIsNotNone(store.get_report(kept["id"]))
        self.assertEqual(exported.read_text(encoding="utf-8"), "already downloaded")

    def test_batch_delete_last_page_returns_to_valid_page(self):
        for index in range(13):
            self.create(title=f"報告 {index}")
        app = self.app()
        app.button(key="report_gallery_next").click().run()
        self.assertEqual(len(app.checkbox), 1)
        app.button(key="report_gallery_select_all").click().run()
        app.button(key="report_gallery_delete_selected").click().run()
        app.button(key="report_gallery_confirm_delete").click().run()
        self.assertFalse(app.exception)
        self.assertIn("第 1／1 頁", self.rendered(app))
        self.assertEqual(app.session_state["report_gallery_page"], 0)
        with ResearchStore(self.path) as store:
            self.assertEqual(store.search_reports()["total"], 12)

    def test_unrelated_mapped_daily_cache_is_preserved_but_legacy_cache_is_cleared(self):
        first, second = self.create(title="A"), self.create(title="B")
        app = self.app()
        app.session_state["daily_essay"] = {"body": "daily for B"}
        app.session_state["daily_essay_report_id"] = second["id"]
        app.button(key="report_gallery_delete_" + first["id"]).click().run()
        app.button(key="report_gallery_confirm_delete").click().run()
        self.assertEqual(app.session_state["daily_essay"], {"body": "daily for B"})
        del app.session_state["daily_essay_report_id"]
        app.button(key="report_gallery_delete_" + second["id"]).click().run()
        app.button(key="report_gallery_confirm_delete").click().run()
        self.assertNotIn("daily_essay", self.state(app))
        self.assertIn("尚未保存報告", self.rendered(app))

    def test_invalid_delete_plan_cannot_expose_confirmation_or_delete(self):
        report = self.create()
        app = self.app()
        app.session_state["report_gallery_delete_plan"] = [{"id": report["id"], "title": report["title"], "version": True}]
        app.run()
        self.assertFalse(app.exception)
        self.assertTrue(any("刪除清單無效" in item.value for item in app.warning))
        self.assertFalse(any(button.key == "report_gallery_confirm_delete" for button in app.button))
        with ResearchStore(self.path) as store:
            self.assertIsNotNone(store.get_report(report["id"]))


if __name__ == "__main__":
    unittest.main()
