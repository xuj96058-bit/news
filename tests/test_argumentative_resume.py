"""Resume invokes only the shared reviewer against an unchanged source snapshot."""
import copy
import hashlib
import json
import time
import unittest
from unittest.mock import MagicMock, patch

from src.analysis_errors import ProviderAPIError
from src.argumentative_workflow import resume_argumentative_review, _body
from src.deep_research_workflow import render_deep_research_body
from src.research import ResearchError
from src.research_packet import packet_findings
import test_deep_research_workflow as deep_fixtures
from test_argumentative_workflow import Stream, review


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class ResumeReviewTests(unittest.TestCase):
    def snapshot(self):
        case = deep_fixtures.DeepResearchTests()
        result = case.generate(case.client())
        result["metadata"]["api_call_count"] = 9
        result["metadata"]["research_execution"]["api_call_count"] = 9
        return result

    def client(self, *values):
        client = MagicMock()
        client.chat.completions.create.side_effect = [Stream(value) if isinstance(value, dict) else value for value in values]
        return client

    def revised(self, report, *, verdict="pass"):
        draft = copy.deepcopy(report["metadata"]["structured_draft"])
        draft["title"] = "Automatically revised operating outlook"
        return review(draft, verdict=verdict)

    def test_one_call_reviews_saved_draft_and_invalidates_old_audit(self):
        snapshot = self.snapshot()
        original = copy.deepcopy(snapshot)
        revised = self.revised(snapshot)
        client, saved, stages = self.client(revised), [], []
        with patch("src.argumentative_workflow.build_research_brief", side_effect=AssertionError("must reuse brief")), \
                patch("src.research_tables.extract_table_observations", side_effect=AssertionError("must not extract")):
            result = resume_argumentative_review(snapshot, api_key="synthetic-key", client=client,
                request_guard=stages.append, on_draft=saved.append)
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertEqual(stages, ["reviewing"])
        payload = json.loads(client.chat.completions.create.call_args.kwargs["messages"][1]["content"])
        self.assertEqual(payload["writer_draft"], original["metadata"]["structured_draft"])
        self.assertEqual(payload["sources"], original["sources"])
        self.assertEqual(payload["argument_plan"], original["metadata"]["argument_plan"])
        self.assertEqual(payload["research_brief"], original["metadata"]["research_brief"])
        self.assertEqual(result["metadata"]["structured_draft"], revised["draft"])
        self.assertEqual(result["sources"], original["sources"])
        self.assertEqual(result["metadata"]["api_call_count"], 10)
        self.assertEqual(result["metadata"]["research_execution"]["api_call_count"], 10)
        self.assertEqual(result["metadata"]["review_history"][-1]["api_call_count"], 10)
        audit = result["metadata"]["claim_audit"]
        self.assertEqual(audit["status"], "stale_after_revision")
        self.assertEqual(audit["claims"], [])
        self.assertFalse(audit["complete"])
        self.assertNotIn("draft_hash", audit)
        self.assertEqual(audit["current_draft_hash"], digest(revised["draft"]))
        self.assertEqual(audit["previous_draft_hash"], original["metadata"]["claim_audit"]["draft_hash"])
        self.assertEqual(result["metadata"]["review_status"], "needs_review")
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[-1], result)
        self.assertEqual(result["body"], render_deep_research_body(result))
        self.assertEqual(snapshot, original)

    def test_single_call_revise_verdict_is_saved_without_another_request(self):
        snapshot = self.snapshot()
        client = self.client(self.revised(snapshot, verdict="revise"))
        result = resume_argumentative_review(snapshot, api_key="synthetic-key", client=client)
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertEqual(result["metadata"]["workflow_stage"], "review_complete")
        self.assertEqual(result["metadata"]["quality"]["verdict"], "revise")
        self.assertEqual(result["metadata"]["review_status"], "needs_review")

    def test_unchanged_draft_keeps_only_its_matching_existing_audit(self):
        snapshot = self.snapshot()
        result = resume_argumentative_review(snapshot, api_key="synthetic-key",
            client=self.client(review(snapshot["metadata"]["structured_draft"])))
        self.assertEqual(result["metadata"]["claim_audit"], snapshot["metadata"]["claim_audit"])
        self.assertEqual(result["metadata"]["structured_draft"], snapshot["metadata"]["structured_draft"])

    def test_malformed_transient_and_fatal_failure_cannot_exceed_one_call(self):
        for failure in ({"unexpected": True}, ProviderAPIError(503), ProviderAPIError(401)):
            with self.subTest(failure=type(failure).__name__):
                snapshot = self.snapshot()
                client = self.client(failure)
                result = resume_argumentative_review(snapshot, api_key="synthetic-key", client=client)
                self.assertEqual(client.chat.completions.create.call_count, 1)
                self.assertEqual(result["metadata"]["review_status"], "review_failed")
                self.assertEqual(result["metadata"]["structured_draft"], snapshot["metadata"]["structured_draft"])
                self.assertEqual(result["metadata"]["api_call_count"], 10)
                self.assertEqual(result["metadata"]["claim_audit"], snapshot["metadata"]["claim_audit"])

    def test_two_call_budget_uses_same_format_repair_loop_and_diagnostics(self):
        snapshot = self.snapshot()
        client = self.client({"unexpected": True}, self.revised(snapshot, verdict="revise"))
        result = resume_argumentative_review(snapshot, api_key="synthetic-key", client=client, max_calls=2)
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertEqual(result["metadata"]["api_call_count"], 11)
        self.assertEqual(len(result["metadata"]["data_preparation"]["review_format_retries"]), 1)
        self.assertEqual(result["metadata"]["workflow_stage"], "review_complete")
        second = json.loads(client.chat.completions.create.call_args.kwargs["messages"][1]["content"])
        self.assertIn("response_repair_request", second)

    def test_checkpoint_failure_prevents_another_review(self):
        snapshot = self.snapshot()
        client = self.client(self.revised(snapshot, verdict="revise"), self.revised(snapshot))
        def failed(_report):
            raise RuntimeError("synthetic storage failure")
        with self.assertRaisesRegex(RuntimeError, "synthetic storage failure"):
            resume_argumentative_review(snapshot, api_key="synthetic-key", client=client,
                max_calls=2, on_draft=failed)
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_late_deadline_keeps_saved_revision_and_offsets_history_only_once(self):
        snapshot = self.snapshot()
        first = self.revised(snapshot, verdict="revise")
        client, saved, now = MagicMock(), [], [0.0]
        def response(**_kwargs):
            if client.chat.completions.create.call_count == 1:
                return Stream(first)
            now[0] = 1001.0
            raise ResearchError("generation_timeout")
        client.chat.completions.create.side_effect = response
        with patch("src.argumentative_workflow.time.monotonic", side_effect=lambda: now[0]):
            result = resume_argumentative_review(snapshot, api_key="synthetic-key", client=client,
                max_calls=2, overall_deadline=1000.0, on_draft=saved.append)
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertEqual(len(saved), 2)
        self.assertEqual(saved[0]["metadata"]["api_call_count"], 10)
        self.assertEqual(result["metadata"]["api_call_count"], 11)
        self.assertEqual(result["metadata"]["review_history"][-1]["api_call_count"], 10)
        self.assertEqual(result["metadata"]["structured_draft"], first["draft"])
        self.assertEqual(result["metadata"]["review_status"], "review_failed")
        self.assertEqual(result["metadata"]["claim_audit"]["status"], "stale_after_revision")
        self.assertEqual(result["metadata"]["claim_audit"]["claims"], [])

    def test_valid_second_review_sees_stale_audit_and_retains_packet_warnings(self):
        snapshot = self.snapshot()
        packet = snapshot["metadata"]["research_packet"]
        packet["evidence"]["warnings"].append({"message": "The extracted evidence still lacks a comparable period."})
        packet["packet_hash"] = digest({key: value for key, value in packet.items() if key != "packet_hash"})
        snapshot["metadata"]["review_findings"].extend(packet_findings(packet))
        metadata = snapshot["metadata"]
        snapshot["body"] = _body(metadata["structured_draft"], snapshot["sources"], packet["design"]["as_of"],
            plan=metadata["argument_plan"], warnings=metadata["validation_warnings"],
            findings=metadata["review_findings"], summary=metadata["review_summary"], stage=metadata["workflow_stage"])
        snapshot["body"] = render_deep_research_body(snapshot)
        first = self.revised(snapshot, verdict="revise")
        client = self.client(first, review(first["draft"]))
        result = resume_argumentative_review(snapshot, api_key="synthetic-key", client=client, max_calls=2)
        second = json.loads(client.chat.completions.create.call_args.kwargs["messages"][1]["content"])
        self.assertEqual(second["writer_draft"], first["draft"])
        self.assertEqual(second["claim_audit"]["status"], "stale_after_revision")
        self.assertEqual(second["claim_audit"]["claims"], [])
        expected = packet_findings(snapshot["metadata"]["research_packet"])
        self.assertTrue(expected)
        for finding in expected:
            self.assertEqual(result["metadata"]["review_findings"].count(finding), 1)

    def test_human_edits_and_expired_deadline_do_not_start_requests(self):
        snapshot = self.snapshot()
        edited = copy.deepcopy(snapshot)
        edited["body"] += "\nHuman authored paragraph."
        client = self.client()
        with self.assertRaises(ValueError):
            resume_argumentative_review(edited, api_key="synthetic-key", client=client)
        result = resume_argumentative_review(snapshot, api_key="synthetic-key", client=client,
            overall_deadline=time.monotonic() - 1)
        self.assertEqual(client.chat.completions.create.call_count, 0)
        self.assertEqual(result["metadata"]["structured_draft"], snapshot["metadata"]["structured_draft"])
        self.assertEqual(result["metadata"]["api_call_count"], 9)
        self.assertEqual(result["metadata"]["review_error_reason"], "generation_timeout")

    def test_partial_overlap_between_review_and_packet_findings_is_saved(self):
        snapshot = self.snapshot()
        packet = snapshot["metadata"]["research_packet"]
        packet["evidence"]["warnings"].extend([{"message": "First evidence gap."}, {"message": "Second evidence gap."}])
        packet["packet_hash"] = digest({key: value for key, value in packet.items() if key != "packet_hash"})
        metadata = snapshot["metadata"]
        metadata["review_findings"].extend(packet_findings(packet))
        snapshot["body"] = _body(metadata["structured_draft"], snapshot["sources"], packet["design"]["as_of"],
            plan=metadata["argument_plan"], warnings=metadata["validation_warnings"],
            findings=metadata["review_findings"], summary=metadata["review_summary"], stage=metadata["workflow_stage"])
        snapshot["body"] = render_deep_research_body(snapshot)
        response = self.revised(snapshot)
        response["findings"] = [packet_findings(packet)[0]]
        saved = []
        result = resume_argumentative_review(snapshot, api_key="synthetic-key", client=self.client(response), on_draft=saved.append)
        self.assertEqual(len(saved), 1)
        self.assertEqual(result["metadata"]["workflow_stage"], "review_complete")
        self.assertEqual(result["metadata"]["structured_draft"], response["draft"])
        for finding in packet_findings(packet):
            self.assertEqual(result["metadata"]["review_findings"].count(finding), 1)

    def test_successful_resume_explicitly_clears_persisted_previous_review_error(self):
        import tempfile
        from pathlib import Path
        from src.research_store import ResearchStore
        snapshot = self.snapshot()
        metadata = snapshot["metadata"]
        metadata.update(workflow_stage="review_failed", review_status="review_failed",
            quality_status="review_failed", review_error="Previous review failed.", review_error_reason="invalid_schema")
        snapshot["body"] = _body(metadata["structured_draft"], snapshot["sources"], "2026-10-14",
            plan=metadata["argument_plan"], warnings=metadata["validation_warnings"],
            findings=metadata["review_findings"], summary=metadata["review_error"], stage="review_failed")
        snapshot["body"] = render_deep_research_body(snapshot)
        with tempfile.TemporaryDirectory() as directory, ResearchStore(Path(directory)/"research.db") as store:
            stored = store.create_report(topic="Orders and profits", title=snapshot["title"], body=snapshot["body"],
                sources=snapshot["sources"], as_of="2026-10-14", provider=metadata["provider"], model=metadata["model"], metadata=metadata)
            result = resume_argumentative_review(stored, api_key="synthetic-key", client=self.client(self.revised(snapshot)))
            saved = store.save_review(stored["id"], title=result["title"], body=result["body"],
                expected_version=stored["version"], metadata=result["metadata"], sources=result["sources"])
            self.assertEqual(saved["metadata"]["review_error"], "")
            self.assertEqual(saved["metadata"]["review_error_reason"], "")
            self.assertEqual(saved["metadata"]["workflow_stage"], "review_complete")


if __name__ == "__main__":
    unittest.main()
