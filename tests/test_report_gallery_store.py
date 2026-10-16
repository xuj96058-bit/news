import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.research_store import ResearchConflictError, ResearchStore, ResearchStoreError
from tests.test_research_store import report_args


class ReportGalleryStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "reports.sqlite3"
        self.store = ResearchStore(self.path)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def create_chain(self, title):
        first = self.store.create_report(**report_args(title=title))
        edited = self.store.save_revision(first["id"], title=title + " edited", body="A human revision.", expected_version=1)
        restored = self.store.restore_revision(first["id"], first["revision_id"], expected_version=2)
        restored_again = self.store.restore_revision(first["id"], restored["revision_id"], expected_version=3)
        return first, edited, restored_again

    def test_delete_removes_original_all_revisions_and_restore_references_durably(self):
        original, _, latest = self.create_chain("Selected")
        untouched, _, untouched_latest = self.create_chain("Untouched")
        untouched_history = self.store.list_revisions(untouched["id"])
        with ResearchStore(self.path) as observer:
            self.assertIsNotNone(observer.get_report(original["id"]))
            self.assertEqual(self.store.delete_report(original["id"], expected_version=latest["version"]), 1)
            self.assertIsNone(observer.get_report(original["id"]))
            self.assertEqual(observer.list_revisions(original["id"]), [])
            self.assertEqual(observer.search_reports("Selected")["total"], 0)
            self.assertEqual(observer.get_report(untouched["id"]), untouched_latest)
        with ResearchStore(self.path) as reopened:
            self.assertIsNone(reopened.get_report(original["id"]))
            self.assertEqual(reopened.list_revisions(original["id"]), [])
            self.assertEqual(reopened.list_revisions(untouched["id"]), untouched_history)
            self.assertEqual(reopened.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(reopened.connection.execute("SELECT COUNT(*) FROM research_reports WHERE id=?", (original["id"],)).fetchone()[0], 0)

    def test_batch_deletes_only_selected_reports_and_all_their_versions(self):
        first, _, first_latest = self.create_chain("First")
        second, _, second_latest = self.create_chain("Second")
        third, _, third_latest = self.create_chain("Third")
        self.assertEqual(self.store.delete_reports({first["id"]: first_latest["version"], second["id"]: second_latest["version"]}), 2)
        self.assertEqual(self.store.search_reports()["total"], 1)
        self.assertEqual(self.store.get_report(third["id"]), third_latest)
        self.assertEqual(self.store.list_revisions(first["id"]), [])
        self.assertEqual(self.store.list_revisions(second["id"]), [])
        self.assertEqual(len(self.store.list_revisions(third["id"])), 4)

    def test_missing_or_stale_version_aborts_entire_batch(self):
        first, _, first_latest = self.create_chain("First")
        second, _, second_latest = self.create_chain("Second")
        snapshots = {item["id"]: self.store.list_revisions(item["id"]) for item in (first, second)}
        for selected, error in (({first["id"]: first_latest["version"], "missing-id": 1}, ResearchStoreError),
                                ({first["id"]: first_latest["version"], second["id"]: 1}, ResearchConflictError)):
            with self.subTest(selected=selected), self.assertRaises(error):
                self.store.delete_reports(selected)
            self.assertEqual(self.store.get_report(first["id"]), first_latest)
            self.assertEqual(self.store.get_report(second["id"]), second_latest)
            for identifier, history in snapshots.items():
                self.assertEqual(self.store.list_revisions(identifier), history)
        self.assertFalse(self.store.connection.in_transaction)

    def test_edits_from_another_connection_are_protected(self):
        first = self.store.create_report(**report_args(title="First"))
        second = self.store.create_report(**report_args(title="Second"))
        with ResearchStore(self.path) as editor:
            updated = editor.save_revision(second["id"], title="Newer title", body="Keep this edit", expected_version=1)
        with self.assertRaises(ResearchConflictError):
            self.store.delete_reports({first["id"]: 1, second["id"]: 1})
        self.assertEqual(self.store.get_report(first["id"]), first)
        self.assertEqual(self.store.get_report(second["id"]), updated)

    def test_sql_failure_rolls_back_prior_deletes_and_restoration_links(self):
        first, _, first_latest = self.create_chain("Delete A")
        second, _, second_latest = self.create_chain("Delete B")
        first_history = self.store.list_revisions(first["id"])
        second_history = self.store.list_revisions(second["id"])
        self.store.connection.execute("""CREATE TRIGGER fail_second_delete BEFORE DELETE ON research_reports
            WHEN OLD.original_title = 'Delete B'
            BEGIN SELECT RAISE(ABORT, 'sensitive database exception'); END""")
        with self.assertRaisesRegex(ResearchStoreError, "^Could not delete the selected reports\\. No selected reports were deleted\\.$"):
            self.store.delete_reports({first["id"]: first_latest["version"], second["id"]: second_latest["version"]})
        self.assertEqual(self.store.get_report(first["id"]), first_latest)
        self.assertEqual(self.store.get_report(second["id"]), second_latest)
        self.assertEqual(self.store.list_revisions(first["id"]), first_history)
        self.assertEqual(self.store.list_revisions(second["id"]), second_history)
        self.assertEqual(self.store.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_unexpected_cross_report_reference_is_not_rewritten_or_partially_deleted(self):
        first = self.store.create_report(**report_args(title="Selected"))
        second = self.store.create_report(**report_args(title="Unselected"))
        # The public restore API forbids cross-report references, but a legacy
        # imported database may contain one. Deletion must preserve isolation.
        with self.store.connection:
            self.store.connection.execute("UPDATE research_revisions SET restored_from_revision_id=? WHERE id=?",
                                          (first["revision_id"], second["revision_id"]))
        with self.assertRaises(ResearchStoreError):
            self.store.delete_reports({first["id"]: 1})
        self.assertIsNotNone(self.store.get_report(first["id"]))
        self.assertEqual(self.store.list_revisions(second["id"])[0]["restored_from_revision_id"], first["revision_id"])
        self.assertEqual(self.store.delete_reports({first["id"]: 1, second["id"]: 1}), 2)

    @patch("src.research_store._now", return_value="2026-10-16T12:00:00.000000+00:00")
    def test_search_all_history_and_stable_pages_beyond_hundred(self, _now):
        records = [self.store.create_report(**report_args(title=f"Report {index}", body=f"Body {index}."))
                   for index in range(137)]
        oldest = records[0]
        self.assertEqual(self.store.search_reports("Body 0.")["items"][0]["id"], oldest["id"])
        seen = []
        for offset in range(0, 137, 12):
            page = self.store.search_reports(limit=12, offset=offset)
            self.assertEqual(page["total"], 137)
            seen.extend(item["id"] for item in page["items"])
        self.assertEqual(seen, [item["id"] for item in reversed(records)])
        self.assertEqual(len(set(seen)), 137)
        self.assertEqual(self.store.search_reports(limit=0), {"items": [], "total": 137})
        self.store.save_revision(oldest["id"], title="Edited oldest", body="Latest searchable body", expected_version=1)
        self.assertEqual(self.store.search_reports(limit=1)["items"][0]["id"], oldest["id"])
        self.assertEqual(self.store.search_reports("Body 0.")["total"], 0)
        self.assertEqual(self.store.search_reports("Latest searchable")["items"][0]["version"], 2)

    def test_search_title_topic_body_is_literal_and_query_safe(self):
        selected = self.store.create_report(**report_args(title="AI 100% _margin", topic="中國政策", body="Costs fell; C:\\tmp remains literal."))
        self.store.create_report(**report_args(title="Other", topic="Europe", body="Other report."))
        for query in ("ai", "100%", "_margin", "中國", "Costs fell", "C:\\tmp"):
            with self.subTest(query=query):
                self.assertEqual(self.store.search_reports(query)["items"][0]["id"], selected["id"])
                self.assertEqual(self.store.search_reports(query)["total"], 1)
        for query in ("' OR 1=1 --", "not present"):
            self.assertEqual(self.store.search_reports(query), {"items": [], "total": 0})

    def test_invalid_inputs_and_empty_selection_do_not_write(self):
        report = self.store.create_report(**report_args())
        self.assertEqual(self.store.delete_reports({}), 0)
        for selected in ([], None, {"": 1}, {None: 1}, {report["id"]: 0}, {report["id"]: True}, {report["id"]: "1"}):
            with self.subTest(selected=selected), self.assertRaises(ResearchStoreError):
                self.store.delete_reports(selected)
        for options in ({"query": None}, {"limit": -1}, {"limit": True}, {"offset": -1}, {"offset": 1.5}):
            with self.subTest(options=options), self.assertRaises(ResearchStoreError):
                self.store.search_reports(**options)
        self.assertEqual(self.store.get_report(report["id"]), report)


if __name__ == "__main__":
    unittest.main()
