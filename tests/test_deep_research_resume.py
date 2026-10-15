"""Audit continuation uses frozen prose and one mocked provider request."""
import copy
import hashlib
import json
import time
import unittest
from unittest.mock import MagicMock, patch

from src.analysis_errors import ProviderAPIError
from src.deep_research_workflow import resume_deep_research_audit as resume_audit
from src.research_audit import parse_claim_audit
from src.research_timing import STAGE_SECONDS
import test_deep_research_workflow as deep_fixtures
from test_deep_research_workflow import Stream


class ResumeAuditTests(unittest.TestCase):
    def snapshot(self):
        case = deep_fixtures.DeepResearchTests()
        result = case.generate(case.client(fail_at=6))
        result.update(id=123, version=4)
        result["metadata"]["reviewed_version"] = 4
        return result

    def client(self):
        client = MagicMock()
        def respond(**kwargs):
            payload = json.loads(kwargs["messages"][1]["content"])
            return Stream({"claims": [{"section": row["section"], "paragraph": row["paragraph"],
                "status": "supported", "evidence_ids": ["E1", "E2"], "calculation_ids": [],
                "reason": "Compared with the supplied evidence."} for row in payload["paragraphs"]],
                "summary": "Audited the current frozen draft."})
        client.chat.completions.create.side_effect = respond
        return client

    def test_only_audit_runs_preserving_prose_versions_and_review(self):
        snapshot = self.snapshot(); original = copy.deepcopy(snapshot)
        client = self.client(); saved = []; stages = []
        with patch("src.deep_research_workflow.prepare_research_packet", side_effect=AssertionError("no preparation")), \
                patch("src.research_workflow.generate_research_workflow", side_effect=AssertionError("no writing or review")):
            result = resume_audit(snapshot, api_key="synthetic-key", client=client,
                request_guard=stages.append, on_draft=saved.append, timeout_seconds=900)
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertEqual(stages, ["auditing"])
        self.assertEqual(result["metadata"]["api_call_count"], 7)
        self.assertEqual(result["metadata"]["research_execution"]["stages"][-1]["attempt"], 7)
        self.assertEqual(result["metadata"]["data_preparation"]["audit_resume"]["timeout_seconds"], 900)
        for field in ("structured_draft", "quality", "review_history"):
            self.assertEqual(result["metadata"][field], original["metadata"][field])
        self.assertEqual((result["id"], result["version"]), (123, 4))
        self.assertEqual(result["sources"], original["sources"])
        self.assertTrue(result["metadata"]["claim_audit"]["complete"])
        self.assertFalse(result["metadata"]["claim_audit"]["independently_verified"])
        self.assertNotIn("逐項核驗未完成，保留待核對狀態", result["body"])
        self.assertEqual(saved, [result])
        self.assertEqual(snapshot, original)
        self.assertNotIn("synthetic-key", json.dumps(result))

    def test_payload_includes_only_cited_source_snapshots_with_hashes(self):
        snapshot = self.snapshot(); client = self.client()
        resume_audit(snapshot, api_key="synthetic-key", client=client)
        payload = json.loads(client.chat.completions.create.call_args.kwargs["messages"][1]["content"])
        cited = {identifier for paragraph in payload["paragraphs"] for identifier in paragraph["source_ids"]}
        self.assertEqual({row["source_id"] for row in payload["snapshot_sources"]}, cited)
        for row in payload["snapshot_sources"]:
            source = next(source for source in snapshot["sources"] if source["id"] == row["source_id"])
            self.assertEqual(row["content"], source["content"])
            self.assertEqual(row["content_sha256"], hashlib.sha256(source["content"].encode()).hexdigest())
        self.assertEqual(payload["evidence_items"], snapshot["metadata"]["research_packet"]["evidence"]["items"])
        self.assertNotIn("writer_draft", payload)

    def test_default_timeout_uses_shared_audit_stage_budget(self):
        result = resume_audit(self.snapshot(), api_key="synthetic-key", client=self.client())
        self.assertEqual(result["metadata"]["data_preparation"]["audit_resume"]["timeout_seconds"], STAGE_SECONDS["auditing"])

    def test_no_binding_stays_unknown_without_claiming_sources_have_no_support(self):
        paragraph = {"section": "thesis", "paragraph": 1,
            "text": "S1 forecasts revenue in the supplied snapshot.", "source_ids": ["S1"]}
        payload = {"claims": [{"section": "thesis", "paragraph": 1, "status": "supported",
            "evidence_ids": [], "calculation_ids": [], "reason": "The raw source contains a forecast."}],
            "summary": "Raw source checked."}
        result = parse_claim_audit(payload, [paragraph], [], [])
        self.assertEqual(result["claims"][0]["status"], "unknown")
        self.assertIn("不能據此判定原材料沒有支持", result["claims"][0]["program_reasons"][0])
        self.assertFalse(result["independently_verified"])

    def test_transport_fatal_and_parse_failures_never_retry_or_fake_completion(self):
        for failure in (ProviderAPIError(503), ProviderAPIError(401), Stream({})):
            with self.subTest(failure=type(failure).__name__):
                snapshot = self.snapshot(); client = MagicMock()
                client.chat.completions.create.side_effect = [failure]
                result = resume_audit(snapshot, api_key="synthetic-key", client=client)
                self.assertEqual(client.chat.completions.create.call_count, 1)
                self.assertEqual(result["metadata"]["api_call_count"], 7)
                self.assertFalse(result["metadata"]["claim_audit"]["complete"])
                self.assertEqual(result["metadata"]["review_status"], "needs_review")
                self.assertEqual(result["metadata"]["structured_draft"], snapshot["metadata"]["structured_draft"])

    def test_expired_external_guard_and_cancel_start_no_requests(self):
        for mode in ("expired", "external", "cancel"):
            with self.subTest(mode=mode):
                client = self.client(); options = {}
                if mode == "expired": options["overall_deadline"] = time.monotonic() - 1
                elif mode == "external": options["request_guard"] = lambda _stage: time.monotonic() - 1
                else: options["register_cancel"] = lambda callback: callback()
                result = resume_audit(self.snapshot(), api_key="synthetic-key", client=client, **options)
                self.assertEqual(client.chat.completions.create.call_count, 0)
                self.assertEqual(result["metadata"]["api_call_count"], 6)
                self.assertFalse(result["metadata"]["claim_audit"]["complete"])

    def test_edits_version_mismatch_and_larger_call_budget_reject_before_request(self):
        snapshot = self.snapshot()
        cases = [(snapshot, {"max_calls": 2})]
        changed = copy.deepcopy(snapshot); changed["body"] += "\nHuman edit."
        cases.append((changed, {}))
        changed = copy.deepcopy(snapshot); changed["version"] = 5
        cases.append((changed, {}))
        for report, options in cases:
            client = self.client()
            with self.assertRaises(ValueError):
                resume_audit(report, api_key="synthetic-key", client=client, **options)
            self.assertEqual(client.chat.completions.create.call_count, 0)

    def test_storage_failure_propagates_without_more_requests(self):
        client = self.client()
        def fail(_report): raise RuntimeError("synthetic storage failure")
        with self.assertRaisesRegex(RuntimeError, "synthetic storage failure"):
            resume_audit(self.snapshot(), api_key="synthetic-key", client=client, on_draft=fail)
        self.assertEqual(client.chat.completions.create.call_count, 1)


if __name__ == "__main__":
    unittest.main()
