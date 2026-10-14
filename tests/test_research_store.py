import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID
from unittest.mock import patch

from src.research_store import ResearchConflictError, ResearchStore, ResearchStoreError


def report_args(**overrides):
    result = {
        "topic": "Monetary policy", "title": "Policy outlook", "body": "Original generated report.",
        "sources": [{"id": "article-1", "title": "Policy decision", "content": "Complete source text.",
                     "url": "https://example.com/decision", "published_at": "2026-10-13T08:00:00Z"}],
        "provider": "OpenAI", "model": "test-model", "as_of": "2026-10-14T08:00:00Z",
        "metadata": {"article_count": 1, "warnings": []},
    }
    result.update(overrides)
    return result


class ResearchStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "nested" / "research_reports.db"
        self.store = ResearchStore(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_original_snapshot_and_generation_metadata_persist_after_reopen(self):
        args = report_args()
        created = self.store.create_report(**args)
        UUID(created["id"])
        UUID(created["revision_id"])
        self.assertEqual(created["version"], 1)
        self.assertEqual(created["original_body"], args["body"])
        self.assertEqual(datetime.fromisoformat(created["created_at"]).tzinfo, timezone.utc)
        args["sources"][0]["content"] = "Later source update"
        created["sources"][0]["title"] = "Edited detached return value"
        with ResearchStore(self.path) as reopened:
            saved = reopened.get_report(created["id"])
            self.assertEqual(saved["sources"][0]["content"], "Complete source text.")
            self.assertEqual(saved["sources"][0]["title"], "Policy decision")
            self.assertEqual((saved["provider"], saved["model"], saved["as_of"]),
                             (args["provider"], args["model"], args["as_of"]))
            self.assertEqual(saved["metadata"], {"article_count": 1, "warnings": []})
            self.assertEqual(reopened.list_reports()[0]["id"], created["id"])

    def test_edits_and_restore_append_versions_preserving_every_original(self):
        created = self.store.create_report(**report_args())
        edited = self.store.save_revision(created["id"], title="Human title", body="Human analysis",
                                          expected_version=1, note="Corrected the interpretation")
        restored = self.store.restore_revision(created["id"], created["revision_id"], expected_version=2)
        self.assertEqual((edited["version"], restored["version"]), (2, 3))
        self.assertEqual((restored["title"], restored["body"]), (created["title"], created["body"]))
        self.assertNotEqual(restored["revision_id"], created["revision_id"])
        self.assertEqual(edited["original_body"], created["body"])
        self.assertEqual(edited["sources"], created["sources"])
        with ResearchStore(self.path) as reopened:
            history = reopened.list_revisions(created["id"])
            self.assertEqual([revision["version"] for revision in history], [3, 2, 1])
            self.assertEqual(history[1]["body"], "Human analysis")
            self.assertEqual(history[1]["note"], "Corrected the interpretation")
            self.assertEqual(history[0]["restored_from_revision_id"], created["revision_id"])
            self.assertEqual(reopened.get_report(created["id"])["original_title"], created["title"])

    def test_citation_repair_status_persists_without_provider_request_settings(self):
        created = self.store.create_report(**report_args(metadata={
            "citation_repaired": True, "request": {"api_key": "SYNTHETIC_PRIVATE_SETTING"},
        }))
        with ResearchStore(self.path) as reopened:
            saved = reopened.get_report(created["id"])
            self.assertEqual(saved["metadata"], {"citation_repaired": True})
        self.assertNotIn(b"SYNTHETIC_PRIVATE_SETTING", self.path.read_bytes())

    def test_review_saves_revision_and_metadata_preserving_writer_draft_and_history(self):
        original_metadata = {
            "article_count": 1, "workflow_schema_version": 1,
            "workflow_stage": "writer", "review_status": "draft",
            "structured_draft": {"title": "Writer title", "sections": [
                {"heading": "Evidence", "paragraphs": [{"text": "Writer claim", "source_ids": ["S1"]}]}]},
        }
        created = self.store.create_report(**report_args(metadata=original_metadata))
        # The writer's output must already be durable while the review runs.
        with ResearchStore(self.path) as reopened:
            self.assertEqual(reopened.get_report(created["id"])["body"], created["body"])
            self.assertEqual(reopened.get_report(created["id"])["metadata"], original_metadata)
        reviewed = self.store.save_review(
            created["id"], title="Reviewed title", body="Reviewed analysis [S1]", expected_version=1,
            metadata={"workflow_stage": "review", "review_status": "reviewed",
                      "review_summary": "Checked dates and source claims.", "review_findings": [],
                      "reviewed_version": 99},
        )
        self.assertEqual((reviewed["version"], reviewed["title"], reviewed["body"]),
                         (2, "Reviewed title", "Reviewed analysis [S1]"))
        self.assertEqual(reviewed["original_body"], created["body"])
        self.assertEqual(reviewed["original_title"], created["title"])
        self.assertEqual(reviewed["sources"], created["sources"])
        self.assertEqual(reviewed["metadata"]["article_count"], 1)
        self.assertEqual(reviewed["metadata"]["structured_draft"], original_metadata["structured_draft"])
        self.assertEqual(reviewed["metadata"]["reviewed_version"], 2)
        edited = self.store.save_revision(created["id"], title="My title", body="My complete replacement",
                                          expected_version=2, note="Author edit")
        self.assertEqual((edited["version"], edited["body"]), (3, "My complete replacement"))
        self.assertEqual(edited["metadata"]["reviewed_version"], 2)
        history = self.store.list_revisions(created["id"])
        self.assertEqual([revision["body"] for revision in history],
                         ["My complete replacement", "Reviewed analysis [S1]", created["body"]])
        self.assertEqual(history[1]["note"], "AI independent review")

    def test_review_cannot_change_author_edits_or_metadata_on_stale_version(self):
        created = self.store.create_report(**report_args(metadata={"review_status": "draft", "warnings": ["Writer warning"]}))
        with ResearchStore(self.path) as reviewer:
            expected_version = reviewer.get_report(created["id"])["version"]
            author_edit = self.store.save_revision(created["id"], title="Author's title", body="Author's text",
                                                   expected_version=1, note="Keep this edit")
            history = self.store.list_revisions(created["id"])
            with self.assertRaises(ResearchConflictError):
                reviewer.save_review(created["id"], title="AI title", body="AI revision",
                                     expected_version=expected_version,
                                     metadata={"review_status": "reviewed", "review_summary": "AI change"})
        self.assertEqual(self.store.get_report(created["id"]), author_edit)
        self.assertEqual(self.store.list_revisions(created["id"]), history)

    def test_failed_review_transaction_rolls_back_metadata_version_and_revision(self):
        created = self.store.create_report(**report_args(metadata={"review_status": "draft", "workflow_stage": "writer"}))
        history = self.store.list_revisions(created["id"])
        self.store.connection.execute("""
            CREATE TRIGGER reject_test_review BEFORE INSERT ON research_revisions
            BEGIN SELECT RAISE(ABORT, 'sensitive database detail'); END
        """)
        with self.assertRaisesRegex(ResearchStoreError, "^Could not save the report review\\.$"):
            self.store.save_review(created["id"], title="AI title", body="AI review", expected_version=1,
                                   metadata={"review_status": "reviewed", "review_summary": "New metadata"})
        self.assertEqual(self.store.get_report(created["id"]), created)
        self.assertEqual(self.store.list_revisions(created["id"]), history)

    def test_review_metadata_failure_rolls_back_before_revision_append(self):
        created = self.store.create_report(**report_args())
        self.store.connection.execute("""
            CREATE TRIGGER reject_test_metadata BEFORE UPDATE OF metadata_json ON research_reports
            BEGIN SELECT RAISE(ABORT, 'sensitive metadata detail'); END
        """)
        with self.assertRaisesRegex(ResearchStoreError, "^Could not save the report review\\.$"):
            self.store.save_review(created["id"], title="AI title", body="AI review", expected_version=1,
                                   metadata={"review_status": "reviewed"})
        self.assertEqual(self.store.get_report(created["id"]), created)
        self.assertEqual(len(self.store.list_revisions(created["id"])), 1)

    def test_safe_workflow_metadata_persists_and_strips_nested_credentials(self):
        created = self.store.create_report(**report_args(metadata={
            "workflow_schema_version": 1, "workflow_stage": "writer", "review_status": "pending",
            "validation_warnings": [{"code": "unknown_source", "section": "Evidence", "paragraph": 0,
                                     "message": "Source requires confirmation", "api_key": "WARNING_SECRET"}],
            "parent_report_id": "older-report", "structured_draft": {
                "paragraphs": [{"text": "Evidence", "source_ids": ["S1"], "api_key": "DRAFT_SECRET"}],
                "request": {"messages": "REQUEST_SECRET"}},
        }))
        reviewed = self.store.save_review(
            created["id"], title="Review", body="Reviewed", expected_version=1,
            metadata={"workflow_stage": "review", "review_status": "needs_review",
                      "review_findings": [{"severity": "warning", "text": "Unclear date", "access_token": "FINDING_SECRET"}],
                      "review_summary": "Verify date", "review_error": None,
                      "usage": {"review": {"tokens": 23, "headers": {"authorization": "AUTH_SECRET"}}},
                      "request": {"messages": "TOP_SECRET"}, "client_settings": "CLIENT_SECRET"},
        )
        self.assertEqual(reviewed["metadata"]["structured_draft"],
                         {"paragraphs": [{"text": "Evidence", "source_ids": ["S1"]}]})
        self.assertEqual(reviewed["metadata"]["review_findings"], [{"severity": "warning", "text": "Unclear date"}])
        self.assertEqual(reviewed["metadata"]["validation_warnings"], [{"code": "unknown_source", "section": "Evidence",
                                                                     "paragraph": 0, "message": "Source requires confirmation"}])
        self.assertEqual(reviewed["metadata"]["usage"], {"review": {"tokens": 23}})
        self.assertEqual(reviewed["metadata"]["reviewed_version"], 2)
        self.assertEqual(reviewed["metadata"]["parent_report_id"], "older-report")
        with ResearchStore(self.path) as reopened:
            self.assertEqual(reopened.get_report(created["id"])["metadata"], reviewed["metadata"])
        self.assertNotIn(b"SECRET", self.path.read_bytes())

    def test_failed_review_status_does_not_claim_current_version_was_reviewed(self):
        created = self.store.create_report(**report_args(metadata={"review_status": "draft"}))
        reviewed = self.store.save_review(created["id"], title="Review", body="Review", expected_version=1,
                                          metadata={"review_status": "reviewed"})
        failed = self.store.save_review(created["id"], title=reviewed["title"], body=reviewed["body"], expected_version=2,
                                       metadata={"review_status": "review_failed", "review_error": "Review could not complete"})
        self.assertNotIn("reviewed_version", failed["metadata"])
        self.assertEqual(failed["metadata"]["review_status"], "review_failed")
        self.assertEqual(len(self.store.list_revisions(created["id"])), 3)

    def test_invalid_review_metadata_does_not_write_and_old_reports_remain_editable(self):
        created = self.store.create_report(**report_args())
        self.assertNotIn("workflow_stage", created["metadata"])
        for metadata in (["not a dictionary"], {"review_findings": object()}, {"review_findings": [float("nan")]},
                         {"structured_draft": {1: "non-text key"}}):
            with self.assertRaises(ResearchStoreError):
                self.store.save_review(created["id"], title="AI title", body="AI text", expected_version=1,
                                       metadata=metadata)
        cyclic = []
        cyclic.append(cyclic)
        with self.assertRaises(ResearchStoreError):
            self.store.save_review(created["id"], title="AI title", body="AI text", expected_version=1,
                                   metadata={"review_findings": cyclic})
        self.assertEqual(self.store.get_report(created["id"]), created)
        self.assertEqual(len(self.store.list_revisions(created["id"])), 1)
        edited = self.store.save_revision(created["id"], title="", body="Freely edited old report", expected_version=1)
        self.assertEqual(edited["body"], "Freely edited old report")
        with self.assertRaisesRegex(ResearchStoreError, "not found"):
            self.store.save_review("missing", title="x", body="x", expected_version=1, metadata={})

    def test_second_editor_cannot_overwrite_newer_version_or_restore_over_it(self):
        created = self.store.create_report(**report_args())
        with ResearchStore(self.path) as second_editor:
            stale = second_editor.get_report(created["id"])
            self.store.save_revision(created["id"], title="Latest", body="Newer human edit", expected_version=1)
            with self.assertRaises(ResearchConflictError):
                second_editor.save_revision(created["id"], title="Stale", body="Older draft",
                                             expected_version=stale["version"])
            with self.assertRaises(ResearchConflictError):
                second_editor.restore_revision(created["id"], created["revision_id"], expected_version=1)
        saved = self.store.get_report(created["id"])
        self.assertEqual((saved["version"], saved["body"]), (2, "Newer human edit"))
        self.assertEqual(len(self.store.list_revisions(created["id"])), 2)

    def test_credentials_and_provider_request_arguments_never_reach_database(self):
        sources = [{
            "title": "Source", "API-Key": "SOURCE_SECRET", "Authorization": "HEADER_SECRET",
            "OpenAI_API_TOKEN": "PROVIDER_SECRET", "secret_access_key": "ACCESS_SECRET",
            "raw_metadata": {"safe": "retained", "auth": {"value": "AUTH_SECRET"},
                             "items": [{"refresh_token": "TOKEN_SECRET", "id": "source-id"}]},
            "request_params": {"model": "model", "messages": "REQUEST_SECRET"},
            "url": "https://user:URL_PASSWORD@example.com/news?id=7&api_key=QUERY_SECRET",
        }]
        metadata = {"article_count": 1, "api_key": "METADATA_SECRET", "request_params": "PARAMS_SECRET",
                    "arbitrary_client_settings": {"credential": "OTHER_SECRET"},
                    "coverage": {"region": "US", "nested": [{"client_secret": "NESTED_SECRET", "days": 2}]}}
        created = self.store.create_report(**report_args(sources=sources, metadata=metadata))
        self.assertEqual(created["sources"], [{
            "title": "Source", "raw_metadata": {"safe": "retained", "items": [{"id": "source-id"}]},
            "url": "https://example.com/news?id=7",
        }])
        self.assertEqual(created["metadata"], {"article_count": 1, "coverage": {"region": "US", "nested": [{"days": 2}]}})
        rows = self.store.connection.execute("SELECT sources_json, metadata_json FROM research_reports").fetchone()
        persisted = json.dumps(tuple(rows))
        self.assertNotIn("SECRET", persisted)
        self.assertNotIn("URL_PASSWORD", persisted)
        self.assertNotIn(b"SECRET", self.path.read_bytes())
        self.assertNotIn(b"URL_PASSWORD", self.path.read_bytes())

    def test_imported_snapshot_urls_strip_signatures_and_fragments_without_rewriting_prose(self):
        prose = "https://example.com/public?series=CPI opens the public series.\nKeep this paragraph unchanged."
        created = self.store.create_report(**report_args(
            provider="import", model="", sources=[{
                "title": "Imported source", "content": prose,
                "url": "https://example.com/news?id=7&X-Amz-Signature=SIGNED_SECRET&lang=zh#access_token=FRAGMENT_SECRET",
                "origins": [{"url": "https://example.com/data?series=CPI&sig=SHORT_SECRET#token=OTHER_SECRET"}],
                "reference": "https://example.com/public?series=CPI&lang=zh%20HK#table-1",
            }], metadata={"coverage": {"reference": "https://example.com/evidence?topic=policy#token=METADATA_SECRET"}},
        ))
        with ResearchStore(self.path) as reopened:
            saved = reopened.get_report(created["id"])
            source = saved["sources"][0]
            self.assertEqual(source["url"], "https://example.com/news?id=7&lang=zh")
            self.assertEqual(source["origins"][0]["url"], "https://example.com/data?series=CPI")
            self.assertEqual(source["reference"], "https://example.com/public?series=CPI&lang=zh%20HK")
            self.assertEqual(source["content"], prose)
            self.assertEqual(saved["metadata"]["coverage"]["reference"], "https://example.com/evidence?topic=policy")
        self.assertNotIn(b"SECRET", self.path.read_bytes())

    @patch("src.research_store._now", return_value="2026-10-14T08:00:00.000000+00:00")
    def test_listing_prioritizes_last_edit_and_respects_limit(self, _clock):
        first = self.store.create_report(**report_args(title="First"))
        second = self.store.create_report(**report_args(title="Second"))
        self.assertEqual([item["id"] for item in self.store.list_reports()], [second["id"], first["id"]])
        self.store.save_revision(first["id"], title="Edited first", body="", expected_version=1)
        self.assertEqual([item["id"] for item in self.store.list_reports(1)], [first["id"]])
        self.assertEqual(self.store.list_reports(0), [])

    def test_missing_records_and_invalid_values_are_safe_and_do_not_write(self):
        self.assertIsNone(self.store.get_report("missing"))
        self.assertEqual(self.store.list_revisions("missing"), [])
        with self.assertRaisesRegex(ResearchStoreError, "not found"):
            self.store.save_revision("missing", title="x", body="x", expected_version=1)
        created = self.store.create_report(**report_args())
        other = self.store.create_report(**report_args(title="Other report"))
        with self.assertRaisesRegex(ResearchStoreError, "not found"):
            self.store.restore_revision(created["id"], other["revision_id"], expected_version=1)
        for version in (0, -1, True, "1"):
            with self.assertRaises(ResearchStoreError):
                self.store.save_revision(created["id"], title="x", body="x", expected_version=version)
        with self.assertRaises(ResearchStoreError):
            self.store.create_report(**report_args(sources=[{"content": float("nan")}]))
        with self.assertRaises(ResearchStoreError):
            self.store.create_report(**report_args(metadata={"warnings": object()}))
        self.assertEqual(self.store.get_report(created["id"])["version"], 1)
        self.assertEqual(len(self.store.list_reports()), 2)

    def test_failed_revision_rolls_back_version_and_exposes_only_safe_error(self):
        created = self.store.create_report(**report_args())
        self.store.connection.execute("""
            CREATE TRIGGER reject_test_edit BEFORE INSERT ON research_revisions
            BEGIN SELECT RAISE(ABORT, 'sensitive database detail'); END
        """)
        with self.assertRaisesRegex(ResearchStoreError, "^Could not save the report revision\\.$"):
            self.store.save_revision(created["id"], title="Draft", body="Draft", expected_version=1)
        self.assertEqual(self.store.get_report(created["id"])["version"], 1)
        self.assertEqual(len(self.store.list_revisions(created["id"])), 1)


if __name__ == "__main__":
    unittest.main()
