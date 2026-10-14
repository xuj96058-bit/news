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
