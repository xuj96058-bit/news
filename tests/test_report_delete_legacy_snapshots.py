"""Deletion compatibility for synthetic legacy deep-research databases only."""

import json
import tempfile
import unittest
from pathlib import Path

from src.research_store import ResearchConflictError, ResearchStore, ResearchStoreError
from tests.test_research_store import report_args


LEGACY_SNAPSHOT_SCHEMA = """
CREATE TABLE research_snapshots (
    report_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    sources_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    snapshot_status TEXT NOT NULL CHECK(snapshot_status IN ('captured','legacy_current','legacy_sources_only')),
    captured_at TEXT NOT NULL,
    PRIMARY KEY(report_id,version),
    FOREIGN KEY(report_id,version) REFERENCES research_revisions(report_id,version)
)
"""


class LegacySnapshotDeletionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "legacy_reports.sqlite3"
        self.store = ResearchStore(self.path)
        self.store.connection.execute(LEGACY_SNAPSHOT_SCHEMA)
        self.store.connection.commit()

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def create_report_with_snapshots(self, title):
        first = self.store.create_report(**report_args(title=title))
        self.store.save_revision(first["id"], title=title + " edited", body="Synthetic revised text", expected_version=1)
        current = self.store.restore_revision(first["id"], first["revision_id"], expected_version=2)
        with self.store.connection:
            for version, status in enumerate(("captured", "legacy_current", "legacy_sources_only"), 1):
                self.store.connection.execute("INSERT INTO research_snapshots VALUES(?,?,?,?,?,?)", (
                    first["id"], version,
                    json.dumps([{"id": "S1", "content": f"Synthetic evidence version {version}"}]),
                    json.dumps({"status": status, "version": version}), status, f"2026-10-17T00:00:0{version}Z",
                ))
        return current

    def database_rows(self):
        return {table: [tuple(row) for row in self.store.connection.execute(f"SELECT * FROM {table} ORDER BY 1,2")]
                for table in ("research_reports", "research_revisions", "research_snapshots")}

    def test_deletes_all_selected_legacy_snapshot_versions_and_preserves_other_reports(self):
        selected = self.create_report_with_snapshots("Selected legacy report")
        other = self.create_report_with_snapshots("Unselected legacy report")
        other_revisions = self.store.list_revisions(other["id"])
        other_snapshots = [tuple(row) for row in self.store.connection.execute(
            "SELECT * FROM research_snapshots WHERE report_id=? ORDER BY version", (other["id"],))]
        with ResearchStore(self.path) as observer:
            self.assertEqual(self.store.delete_reports({selected["id"]: selected["version"]}), 1)
            self.assertIsNone(observer.get_report(selected["id"]))
            self.assertEqual(observer.list_revisions(selected["id"]), [])
            self.assertEqual(observer.connection.execute(
                "SELECT COUNT(*) FROM research_snapshots WHERE report_id=?", (selected["id"],)).fetchone()[0], 0)
        with ResearchStore(self.path) as reopened:
            self.assertEqual(reopened.get_report(other["id"]), other)
            self.assertEqual(reopened.list_revisions(other["id"]), other_revisions)
            self.assertEqual([tuple(row) for row in reopened.connection.execute(
                "SELECT * FROM research_snapshots WHERE report_id=? ORDER BY version", (other["id"],))], other_snapshots)
            self.assertIsNotNone(reopened.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_snapshots'").fetchone())
            self.assertEqual(reopened.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(reopened.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_stale_or_missing_batch_preserves_every_snapshot_before_any_delete(self):
        first = self.create_report_with_snapshots("First")
        second = self.create_report_with_snapshots("Second")
        before = self.database_rows()
        for expected, error in (({first["id"]: first["version"], second["id"]: 1}, ResearchConflictError),
                                ({first["id"]: first["version"], "missing": 1}, ResearchStoreError)):
            with self.subTest(expected=expected), self.assertRaises(error):
                self.store.delete_reports(expected)
            self.assertEqual(self.database_rows(), before)
            self.assertFalse(self.store.connection.in_transaction)

    def test_later_sql_failure_rolls_back_snapshots_revisions_and_prior_report_delete(self):
        first = self.create_report_with_snapshots("First")
        second = self.create_report_with_snapshots("Second")
        before = self.database_rows()
        self.store.connection.execute("""CREATE TRIGGER fail_late_delete BEFORE DELETE ON research_reports
            WHEN OLD.original_title='Second'
            BEGIN SELECT RAISE(ABORT, 'synthetic private database detail'); END""")
        with self.assertRaisesRegex(ResearchStoreError,
                                    "^Could not delete the selected reports\\. No selected reports were deleted\\.$"):
            self.store.delete_reports({first["id"]: first["version"], second["id"]: second["version"]})
        self.assertEqual(self.database_rows(), before)
        self.assertEqual(self.store.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        with ResearchStore(self.path) as observer:
            self.assertEqual(observer.get_report(first["id"]), first)
            self.assertEqual(observer.get_report(second["id"]), second)
            self.assertEqual(observer.connection.execute("SELECT COUNT(*) FROM research_snapshots").fetchone()[0], 6)

    def test_unknown_dependent_table_is_not_cascaded_or_modified(self):
        report = self.create_report_with_snapshots("Selected")
        with self.store.connection:
            self.store.connection.execute("CREATE TABLE unrelated_legacy_children (report_id TEXT REFERENCES research_reports(id), payload TEXT)")
            self.store.connection.execute("INSERT INTO unrelated_legacy_children VALUES(?,?)", (report["id"], "Preserve this row"))
        before = self.database_rows()
        with self.assertRaises(ResearchStoreError):
            self.store.delete_reports({report["id"]: report["version"]})
        self.assertEqual(self.database_rows(), before)
        self.assertEqual(tuple(self.store.connection.execute("SELECT * FROM unrelated_legacy_children").fetchone()),
                         (report["id"], "Preserve this row"))
        self.assertEqual(self.store.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_main_schema_without_snapshot_table_still_deletes_normally(self):
        with ResearchStore(":memory:") as current:
            report = current.create_report(**report_args())
            self.assertIsNone(current.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='research_snapshots'").fetchone())
            self.assertEqual(current.delete_report(report["id"], expected_version=1), 1)
            self.assertIsNone(current.get_report(report["id"]))
            self.assertEqual(current.list_revisions(report["id"]), [])
            self.assertEqual(current.connection.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
