"""Versioned source/evidence snapshots survive enrichment, conflicts and upgrades."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from src.research_store import ResearchConflictError, ResearchStore, ResearchStoreError


def arguments():
    return {"topic": "AI cash flows", "title": "Initial evidence", "body": "Initial source claim [S1].",
            "sources": [{"id": "S1", "content": "Source one", "url": "https://example.com/one"}],
            "provider": "test", "model": "test", "as_of": "2026-10-15",
            "metadata": {"deep_research": True, "workflow_stage": "evidence_saved", "review_status": "pending",
                         "research_packet": {"round": 0, "evidence": {"items": []}}}}


class ResearchSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "reports.db"
        self.store = ResearchStore(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def enrich(self, created, **overrides):
        options = {"title": "Enriched report", "body": "More evidence [S1] [S2].", "expected_version": created["version"],
                   "sources": [*created["sources"], {"id": "S2", "content": "Second source"}],
                   "metadata": {"workflow_stage": "review_complete", "review_status": "reviewed",
                                "research_packet": {"round": 1, "evidence": {"items": [{"id": "E1", "source_id": "S2"}]}},
                                "claim_audit": {"status": "reviewed_snapshot", "draft_sha256": "version-two"}}}
        options.update(overrides)
        return self.store.save_review(created["id"], **options)

    def test_enrichment_updates_current_sources_and_preserves_immutable_original(self):
        created = self.store.create_report(**arguments())
        first = self.store.get_research_snapshot(created["id"], 1)
        reviewed = self.enrich(created)
        self.assertEqual([item["id"] for item in reviewed["sources"]], ["S1", "S2"])
        self.assertEqual(reviewed["original_sources"], created["sources"])
        self.assertEqual(reviewed["original_body"], created["body"])
        self.assertEqual(self.store.get_research_snapshot(created["id"], 1), first)
        second = self.store.get_research_snapshot(created["id"], 2)
        self.assertEqual(second["sources"], reviewed["sources"])
        self.assertEqual(second["metadata"]["research_packet"]["round"], 1)
        self.assertEqual(first["metadata"]["research_packet"]["round"], 0)
        with ResearchStore(self.path) as reopened:
            self.assertEqual(reopened.get_report(created["id"]), reviewed)
            self.assertEqual([row["version"] for row in reopened.list_research_snapshots(created["id"])], [2, 1])

    def test_each_stage_is_detached_from_callers_and_later_mutation(self):
        args = arguments()
        created = self.store.create_report(**args)
        args["sources"][0]["content"] = "Caller changed initial source"
        sources = [*created["sources"], {"id": "S2", "content": "Original second source"}]
        reviewed = self.enrich(created, sources=sources)
        sources[1]["content"] = "Caller changed second source"
        reviewed["sources"][0]["content"] = "Changed returned object"
        snapshot = self.store.get_research_snapshot(created["id"], 2)
        self.assertEqual(snapshot["sources"][0]["content"], "Source one")
        self.assertEqual(snapshot["sources"][1]["content"], "Original second source")
        snapshot["metadata"]["research_packet"]["round"] = 999
        self.assertEqual(self.store.get_research_snapshot(created["id"], 2)["metadata"]["research_packet"]["round"], 1)

    def test_manual_edit_copies_sources_but_does_not_refresh_review(self):
        created = self.store.create_report(**arguments())
        reviewed = self.enrich(created)
        edited = self.store.save_revision(created["id"], title="Manual analysis", body="Changed reasoning.", expected_version=2)
        self.assertEqual(edited["version"], 3)
        self.assertEqual(edited["metadata"]["reviewed_version"], 2)
        self.assertEqual(edited["metadata"]["claim_audit"]["draft_sha256"], "version-two")
        self.assertEqual(edited["sources"], reviewed["sources"])
        snapshot = self.store.get_research_snapshot(created["id"], 3)
        self.assertEqual(snapshot["metadata"]["reviewed_version"], 2)
        self.assertEqual(snapshot["body"], "Changed reasoning.")
        self.assertEqual(self.store.get_research_snapshot(created["id"], 2)["body"], reviewed["body"])

    def test_stale_review_cannot_change_sources_metadata_or_any_snapshot(self):
        created = self.store.create_report(**arguments())
        edited = self.store.save_revision(created["id"], title="Human edit", body="Keep this.", expected_version=1)
        before = self.store.list_research_snapshots(created["id"])
        with ResearchStore(self.path) as other:
            with self.assertRaises(ResearchConflictError):
                other.save_review(created["id"], title="Stale AI", body="Stale", expected_version=1,
                                  sources=[{"id": "S99", "content": "Should not persist"}],
                                  metadata={"review_status": "reviewed"})
        self.assertEqual(self.store.get_report(created["id"]), edited)
        self.assertEqual(self.store.list_research_snapshots(created["id"]), before)

    def test_snapshot_failure_rolls_back_sources_review_version_and_revision(self):
        created = self.store.create_report(**arguments())
        first = self.store.get_research_snapshot(created["id"], 1)
        self.store.connection.execute("""
            CREATE TRIGGER reject_snapshot BEFORE INSERT ON research_snapshots
            BEGIN SELECT RAISE(ABORT, 'sensitive database detail'); END
        """)
        with self.assertRaisesRegex(ResearchStoreError, "^Could not save the report review\\.$"):
            self.enrich(created)
        self.assertEqual(self.store.get_report(created["id"]), created)
        self.assertEqual(self.store.list_research_snapshots(created["id"]), [first])
        self.assertEqual(len(self.store.list_revisions(created["id"])), 1)

    def test_secret_stripping_covers_new_sources_nested_metadata_and_snapshot_table(self):
        created = self.store.create_report(**arguments())
        sources = [*created["sources"], {"id": "S2", "api_key": "SOURCE_SECRET",
                   "url": "https://user:USER_SECRET@example.com/two?series=AI&token=URL_SECRET",
                   "origins": [{"access_token": "NESTED_SECRET", "url": "https://example.com/two#token=FRAGMENT_SECRET"}]}]
        self.enrich(created, sources=sources, metadata={"review_status": "reviewed", "deep_research": True,
                    "research_packet": {"round": 1, "sources": sources, "api_key": "PACKET_SECRET"},
                    "claim_audit": {"status": "reviewed_snapshot", "authorization": "AUDIT_SECRET"},
                    "client_settings": "SETTINGS_SECRET"})
        snapshots = self.store.list_research_snapshots(created["id"])
        serialized = json.dumps(snapshots)
        self.assertNotIn("SECRET", serialized)
        self.assertEqual(snapshots[0]["sources"][1]["url"], "https://example.com/two?series=AI")
        self.assertNotIn(b"SECRET", self.path.read_bytes())

    def test_old_schema_auto_upgrade_has_honest_fallback_and_preserves_baseline(self):
        created = self.store.create_report(**arguments())
        reviewed = self.store.save_review(created["id"], title="Legacy review", body="Legacy reviewed body",
                                          expected_version=1, metadata={"review_status": "reviewed", "research_packet": {"round": 5}})
        self.store.save_revision(created["id"], title="Legacy manual edit", body="Old edited body", expected_version=2)
        # Model an existing database produced before the snapshots table existed.
        self.store.connection.execute("DROP TABLE research_snapshots")
        self.store.connection.commit()
        self.store.close()
        self.store = ResearchStore(self.path)
        original = self.store.get_research_snapshot(created["id"], 1)
        self.assertEqual(original["snapshot_status"], "legacy_fallback")
        self.assertEqual(original["metadata"], {})
        self.assertFalse(original["historical_metadata_available"])
        current = self.store.get_research_snapshot(created["id"], 3)
        self.assertEqual(current["metadata"]["reviewed_version"], 2)
        self.assertEqual(current["sources"], created["sources"])
        enriched = self.enrich(self.store.get_report(created["id"]))
        self.assertEqual(enriched["version"], 4)
        self.assertEqual(enriched["original_sources"], created["sources"])
        baseline = self.store.get_research_snapshot(created["id"], 3)
        self.assertEqual(baseline["snapshot_status"], "legacy_current")
        self.assertEqual(baseline["metadata"]["research_packet"]["round"], 5)
        self.assertEqual(self.store.get_research_snapshot(created["id"], 1)["snapshot_status"], "legacy_sources_only")
        self.assertEqual(self.store.get_research_snapshot(created["id"], 2)["metadata"], {})
        self.assertEqual(self.store.get_research_snapshot(created["id"], 2)["sources"], created["sources"])

    def test_bad_source_values_reject_before_any_write(self):
        created = self.store.create_report(**arguments())
        before = self.store.list_research_snapshots(created["id"])
        for sources in ({}, ["not a snapshot"], [{"content": float("nan")}], [{"content": object()}]):
            with self.assertRaises(ResearchStoreError):
                self.enrich(created, sources=sources)
        self.assertEqual(self.store.get_report(created["id"]), created)
        self.assertEqual(self.store.list_research_snapshots(created["id"]), before)

    def test_omitting_sources_preserves_current_snapshot_and_missing_versions_are_none(self):
        created = self.store.create_report(**arguments())
        enriched = self.enrich(created)
        reviewed = self.store.save_review(created["id"], title="Review again", body="Same source scope", expected_version=2,
                                          metadata={"review_status": "needs_review"})
        self.assertEqual(reviewed["sources"], enriched["sources"])
        self.assertIsNone(self.store.get_research_snapshot(created["id"], 99))
        self.assertIsNone(self.store.get_research_snapshot("missing", 1))
        self.assertEqual(self.store.list_research_snapshots("missing"), [])
        for version in (0, -1, True, "1"):
            with self.assertRaises(ResearchStoreError):
                self.store.get_research_snapshot(created["id"], version)

    def test_restore_initial_revision_restores_its_sources_and_research_metadata(self):
        created = self.store.create_report(**arguments())
        initial_snapshot = self.store.get_research_snapshot(created["id"], 1)
        reviewed = self.enrich(created)
        second_snapshot = self.store.get_research_snapshot(created["id"], 2)
        restored = self.store.restore_revision(created["id"], created["revision_id"], expected_version=2)
        self.assertEqual(restored["version"], 3)
        self.assertEqual(restored["title"], created["title"])
        self.assertEqual(restored["body"], created["body"])
        self.assertEqual([row["id"] for row in restored["sources"]], ["S1"])
        self.assertEqual(restored["metadata"]["research_packet"]["round"], 0)
        self.assertEqual(restored["metadata"]["review_status"], "pending")
        self.assertNotIn("claim_audit", restored["metadata"])
        self.assertNotIn("reviewed_version", restored["metadata"])
        self.assertTrue(restored["metadata"]["restoration"]["historical_metadata_available"])
        self.assertEqual(restored["original_sources"], created["sources"])
        self.assertEqual(restored["original_body"], created["body"])
        self.assertEqual(self.store.get_research_snapshot(created["id"], 1), initial_snapshot)
        self.assertEqual(self.store.get_research_snapshot(created["id"], 2), second_snapshot)
        new_snapshot = self.store.get_research_snapshot(created["id"], 3)
        self.assertEqual(new_snapshot["sources"], created["sources"])
        self.assertEqual(new_snapshot["metadata"], restored["metadata"])
        self.assertEqual(self.store.list_revisions(created["id"])[0]["restored_from_revision_id"], created["revision_id"])
        with ResearchStore(self.path) as reopened:
            self.assertEqual(reopened.get_report(created["id"]), restored)
            self.assertEqual(reopened.get_research_snapshot(created["id"], 2)["sources"], reviewed["sources"])

    def test_restore_reviewed_revision_keeps_old_review_version(self):
        created = self.store.create_report(**arguments())
        reviewed = self.enrich(created)
        edited = self.store.save_review(created["id"], title="Later findings", body="Later sources [S3]",
            expected_version=2, sources=[*reviewed["sources"], {"id": "S3", "content": "Third source"}],
            metadata={"review_status": "reviewed", "claim_audit": {"draft_sha256": "version-three"}})
        restored = self.store.restore_revision(created["id"], reviewed["revision_id"], expected_version=3)
        self.assertEqual(restored["version"], 4)
        self.assertEqual(restored["metadata"]["reviewed_version"], 2)
        self.assertNotEqual(restored["metadata"]["reviewed_version"], restored["version"])
        self.assertEqual(restored["metadata"]["claim_audit"]["draft_sha256"], "version-two")
        self.assertEqual(restored["sources"], reviewed["sources"])
        self.assertEqual(self.store.get_research_snapshot(created["id"], 3)["sources"], edited["sources"])
        self.assertEqual(self.store.get_research_snapshot(created["id"], 4)["metadata"]["reviewed_version"], 2)

    def test_restore_legacy_revision_without_metadata_does_not_attach_current_audit(self):
        created = self.store.create_report(**arguments())
        self.store.save_review(created["id"], title="Legacy reviewed", body="Reviewed against original sources.",
            expected_version=1, metadata={"review_status": "reviewed", "claim_audit": {"draft_sha256": "later-audit"},
                                          "research_packet": {"round": 9}})
        self.store.connection.execute("DROP TABLE research_snapshots")
        self.store.connection.commit()
        self.store.close()
        self.store = ResearchStore(self.path)
        restored = self.store.restore_revision(created["id"], created["revision_id"], expected_version=2)
        self.assertEqual(restored["version"], 3)
        self.assertEqual(restored["sources"], created["sources"])
        self.assertEqual(restored["metadata"]["review_status"], "needs_review")
        self.assertEqual(restored["metadata"]["quality_status"], "needs_review")
        self.assertFalse(restored["metadata"]["restoration"]["historical_metadata_available"])
        self.assertIn("未保存歷史研究資料", restored["metadata"]["review_error"])
        for field in ("claim_audit", "research_packet", "reviewed_version"):
            self.assertNotIn(field, restored["metadata"])
        previous = self.store.get_research_snapshot(created["id"], 2)
        self.assertEqual(previous["metadata"]["claim_audit"]["draft_sha256"], "later-audit")
        self.assertEqual(restored["original_sources"], created["sources"])

    def test_restore_stale_editor_rolls_back_current_state_and_all_snapshots(self):
        created = self.store.create_report(**arguments())
        reviewed = self.enrich(created)
        before = self.store.list_research_snapshots(created["id"])
        with ResearchStore(self.path) as stale:
            with self.assertRaises(ResearchConflictError):
                stale.restore_revision(created["id"], created["revision_id"], expected_version=1)
        self.assertEqual(self.store.get_report(created["id"]), reviewed)
        self.assertEqual(self.store.list_research_snapshots(created["id"]), before)
        self.assertEqual(len(self.store.list_revisions(created["id"])), 2)

    def test_restore_snapshot_write_failure_rolls_back_sources_metadata_and_body(self):
        created = self.store.create_report(**arguments())
        reviewed = self.enrich(created)
        before = self.store.list_research_snapshots(created["id"])
        self.store.connection.execute("""
            CREATE TRIGGER reject_restoration BEFORE INSERT ON research_snapshots
            BEGIN SELECT RAISE(ABORT, 'sensitive database detail'); END
        """)
        with self.assertRaisesRegex(ResearchStoreError, "^Could not restore the report revision\\.$"):
            self.store.restore_revision(created["id"], created["revision_id"], expected_version=2)
        self.assertEqual(self.store.get_report(created["id"]), reviewed)
        self.assertEqual(self.store.list_research_snapshots(created["id"]), before)
        self.assertEqual(len(self.store.list_revisions(created["id"])), 2)

    def test_restore_unversioned_historical_review_is_explicitly_unconfirmed(self):
        args = arguments()
        args["metadata"].update(review_status="reviewed", quality_status="reviewed_snapshot")
        created = self.store.create_report(**args)
        self.enrich(created)
        restored = self.store.restore_revision(created["id"], created["revision_id"], expected_version=2)
        self.assertEqual(restored["metadata"]["review_status"], "needs_review")
        self.assertNotIn("reviewed_version", restored["metadata"])
        self.assertIn("未記錄有效的版本編號", restored["metadata"]["review_error"])


if __name__ == "__main__":
    unittest.main()
