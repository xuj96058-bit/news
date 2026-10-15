"""Deterministic end-to-end deep workflow; no live providers or data services."""
import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from src.deep_research_workflow import research_budget
from src.research import prepare_sources
from src.research_packet import prepare_research_packet, render_packet_appendix
from src.research_workflow import generate_research_workflow
from test_argumentation import essay, plan

TOPIC = "Orders and profits"
AS_OF = "2026-10-14"


def sources():
    return prepare_sources([{"id": i, "title": title, "content": content,
        "published_at": "2026-10-10", "content_scope": "provided_text"}
        for i, title, content in [(1, "Orders", "Orders improved in the disclosed business."),
                                  (2, "Costs", "Costs constrained margins in the disclosed business.")]])


def design():
    return {"version": 1, "topic": TOPIC, "as_of": AS_OF, "markets": ["us", "hk", "cn"],
        "questions": [{"id": "Q1", "question": "What supports the operating outlook?"}],
        "requirements": [{"id": "R1", "question_id": "Q1", "description": "Operating evidence",
                          "kind": "document", "critical": True}], "methods": []}


def evidence():
    return {"items": [{"id": f"E{i}", "kind": "document", "requirement_id": "R1",
        "source_id": source["id"], "quote": source["content"], "statement": source["content"],
        "event_status": "unknown"} for i, source in enumerate(sources(), 1)]}


class Stream:
    def __init__(self, value): self.value = value
    def __iter__(self):
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=json.dumps(self.value)), finish_reason="stop")])
    def close(self): pass


class DeepResearchTests(unittest.TestCase):
    def client(self, *, revise=False, fail_at=None):
        client = MagicMock()
        self.payloads = []
        def respond(**kwargs):
            payload = json.loads(kwargs["messages"][1]["content"])
            self.payloads.append(payload)
            if fail_at == len(self.payloads):
                raise RuntimeError("synthetic-secret-provider-failure")
            if "available_sources" in payload:
                value = design()
            elif "previous_items" in payload:
                value = evidence()
            elif "paragraphs" in payload:
                value = {"claims": [{"section": p["section"], "paragraph": p["paragraph"],
                    "status": "supported", "evidence_ids": ["E1", "E2"], "calculation_ids": [],
                    "reason": "Compared with the frozen source excerpts."} for p in payload["paragraphs"]],
                    "summary": "Compared with evidence."}
            elif "writer_draft" in payload:
                draft = essay()
                if revise:
                    draft["title"] = "Revised operating outlook"
                value = {"draft": draft, "findings": [], "summary": "Reviewed against sources.",
                         "quality": {**{key: 3 for key in ("relevance", "evidence", "reasoning", "readability")}, "verdict": "pass"}}
            elif "argument_plan" in payload:
                value = essay()
            else:
                value = plan()
            return Stream(value)
        client.chat.completions.create.side_effect = respond
        return client

    def generate(self, client, **kwargs):
        return generate_research_workflow(TOPIC, sources(), api_key="synthetic-key", as_of=AS_OF,
            deep_research=True, writing_style="argumentative", report_mode="argumentative",
            retrieve_sources=False, client=client, **kwargs)

    def test_budgets_are_bounded_and_defaults_are_not_mutated(self):
        self.assertEqual(research_budget()["max_calls"], 16)
        for value in ({"max_calls": True}, {"max_calls": 25}, {"max_rounds": 4},
                      {"timeout_seconds": 119}, {"extra": 1}):
            with self.subTest(value=value), self.assertRaises(ValueError): research_budget(value)
        research_budget()["max_calls"] = 6
        self.assertEqual(research_budget()["max_calls"], 16)

    def test_real_writers_receive_packet_and_review_receives_claim_audit(self):
        saved = []
        client = self.client()
        result = self.generate(client, on_draft=saved.append)
        self.assertEqual(client.chat.completions.create.call_count, 6, result["metadata"])
        self.assertTrue(any(p.get("claim_audit") for p in self.payloads if "writer_draft" in p))
        self.assertTrue(any(p.get("research_packet") for p in self.payloads if "argument_plan" in p))
        self.assertEqual(saved[0]["metadata"]["workflow_stage"], "research_prepared")
        self.assertTrue(any(p["metadata"].get("structured_draft") for p in saved))
        self.assertEqual(result["metadata"]["answer_status"], "limited")  # narrowed original question
        self.assertEqual(result["metadata"]["review_status"], "needs_review")
        self.assertEqual(result["metadata"]["research_execution"]["api_call_count"], 6)
        self.assertIn("研究設計與證據紀錄", result["body"])
        self.assertNotIn("synthetic-key", json.dumps(result))

    def test_final_audit_uses_revised_title_and_body(self):
        saved = []
        result = self.generate(self.client(revise=True), on_draft=saved.append)
        audits = [p for p in self.payloads if "paragraphs" in p]
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[-1]["paragraphs"][0]["text"], "Revised operating outlook")
        initial = next(row for row in saved if row["metadata"]["workflow_stage"] == "writer_saved")
        self.assertNotEqual(initial["metadata"]["structured_draft"]["title"], audits[-1]["paragraphs"][0]["text"])
        self.assertEqual(result["metadata"]["claim_audit"]["draft_hash"], result["metadata"]["research_packet"]["claim_audit"]["draft_hash"])

    def test_completed_review_is_saved_before_final_audit_request(self):
        saved = []
        client = self.client(revise=True)
        original = client.chat.completions.create.side_effect
        def check_audit_checkpoint(**kwargs):
            value = original(**kwargs)
            if "paragraphs" in self.payloads[-1]:
                self.assertEqual(saved[-1]["metadata"]["workflow_stage"], "review_complete")
                self.assertEqual(saved[-1]["title"], "Revised operating outlook")
                self.assertTrue(saved[-1]["metadata"]["report_has_body"])
                self.assertEqual(saved[-1]["metadata"]["review_history"][-1]["verdict"], "pass")
            return value
        client.chat.completions.create.side_effect = check_audit_checkpoint
        self.generate(client, on_draft=saved.append)

    def test_audit_failure_never_promotes_report(self):
        result = self.generate(self.client(fail_at=6))
        self.assertFalse(result["metadata"]["claim_audit"]["complete"])
        self.assertEqual(result["metadata"]["review_status"], "needs_review")
        self.assertTrue(result["metadata"]["report_has_body"])
        self.assertNotIn("synthetic-secret-provider-failure", json.dumps(result))

    def test_final_audit_handles_partial_overlap_with_packet_findings(self):
        from src.research_packet import prepare_research_packet, packet_findings
        client = self.client()
        original_response = client.chat.completions.create.side_effect
        warnings = [{"message": "First evidence gap."}, {"message": "Second evidence gap."}]
        def prepare(*args, **kwargs):
            packet = prepare_research_packet(*args, **kwargs)
            packet["evidence"]["warnings"].extend(warnings)
            return packet
        def respond(**kwargs):
            value = original_response(**kwargs)
            if "writer_draft" in self.payloads[-1]:
                value.value["findings"] = [{"section": "limitations", "paragraph": 1,
                    "severity": "warning", "message": warnings[0]["message"], "source_ids": []}]
            return value
        client.chat.completions.create.side_effect = respond
        with patch("src.deep_research_workflow.prepare_research_packet", side_effect=prepare):
            result = self.generate(client)
        self.assertEqual(result["metadata"]["workflow_stage"], "review_complete")
        self.assertTrue(result["metadata"]["claim_audit"]["complete"])
        for finding in packet_findings(result["metadata"]["research_packet"]):
            self.assertEqual(result["metadata"]["review_findings"].count(finding), 1)

    def test_reviewer_failure_never_reclassifies_saved_body_as_preparation(self):
        saved = []
        result = self.generate(self.client(fail_at=5), on_draft=saved.append)
        self.assertEqual(result["metadata"]["review_status"], "review_failed")
        self.assertTrue(result["metadata"]["report_has_body"])
        self.assertTrue(saved[-1]["metadata"]["report_has_body"])
        self.assertTrue(saved[-1]["metadata"]["structured_draft"])
        self.assertNotIn("正文尚未生成", result["body"])

    def test_minimum_call_budget_prioritizes_review_and_final_audit(self):
        client = self.client()
        result = self.generate(client, budget={"max_calls": 6})
        self.assertEqual(client.chat.completions.create.call_count, 6)
        self.assertEqual(result["metadata"]["research_execution"]["api_call_count"], 6)
        self.assertTrue(result["metadata"]["claim_audit"]["complete"])
        self.assertEqual([row["stage"] for row in result["metadata"]["research_execution"]["stages"]],
            ["designing", "organizing", "planning", "writing", "reviewing", "auditing"])
        self.assertEqual(result["metadata"]["review_status"], "needs_review")

    def test_evidence_api_failure_preserves_design_and_continues_to_prose(self):
        saved = []
        result = self.generate(self.client(fail_at=2), on_draft=saved.append)
        self.assertFalse(saved[0]["metadata"]["report_has_body"])
        self.assertTrue(saved[-1]["metadata"]["report_has_body"])
        self.assertTrue(result["metadata"]["structured_draft"])
        self.assertEqual(result["metadata"]["review_status"], "needs_review")
        self.assertEqual(result["metadata"]["failure_stage"], "organizing")
        self.assertEqual(result["metadata"]["failure_reason"], "provider_or_processing_error")
        self.assertEqual(result["metadata"]["answer_status"], "unanswered")
        self.assertNotIn("正文尚未生成", result["body"])
        self.assertEqual(result["metadata"]["research_packet"]["design"]["questions"], design()["questions"])
        self.assertNotIn("synthetic-secret-provider-failure", json.dumps(result))

    def test_design_api_failure_uses_sources_without_claiming_completed_research(self):
        result = self.generate(self.client(fail_at=1))
        self.assertTrue(result["metadata"]["report_has_body"])
        self.assertEqual(result["metadata"]["failure_stage"], "designing")
        self.assertEqual(result["metadata"]["answer_status"], "unanswered")
        self.assertEqual(result["metadata"]["review_status"], "needs_review")
        self.assertNotIn("正文尚未生成", result["body"])

    def test_repeated_provider_failure_keeps_memo_separate_from_a_draft(self):
        client = self.client()
        original = client.chat.completions.create.side_effect
        def fail_after_design(**kwargs):
            if len(self.payloads):
                raise RuntimeError("synthetic-secret-token")
            return original(**kwargs)
        client.chat.completions.create.side_effect = fail_after_design
        result = self.generate(client)
        self.assertFalse(result["metadata"]["report_has_body"])
        self.assertEqual(result["metadata"]["workflow_stage"], "preparation_failed")
        self.assertEqual(result["metadata"]["review_status"], "preparation_failed")
        self.assertEqual(result["metadata"]["failure_stage"], "writing")
        self.assertNotIn("synthetic-secret-token", json.dumps(result))
        self.assertEqual(client.chat.completions.create.call_count, 4)

    def test_extraction_and_planning_transport_failures_still_reach_body(self):
        client = self.client()
        original = client.chat.completions.create.side_effect
        def two_failures(**kwargs):
            result = original(**kwargs)
            if len(self.payloads) in {2, 3}:
                raise RuntimeError("synthetic-secret-connection")
            return result
        client.chat.completions.create.side_effect = two_failures
        result = self.generate(client)
        self.assertTrue(result["metadata"]["report_has_body"])
        self.assertTrue(result["metadata"]["structured_draft"]["arguments"])
        self.assertTrue(any(row.get("stage") == "planning" for row in result["metadata"]["preparation_warnings"]))
        self.assertEqual(result["metadata"]["review_status"], "needs_review")
        self.assertNotIn("synthetic-secret-connection", json.dumps(result))

    def test_preparation_call_budget_reserves_plan_writer_and_reviewer(self):
        packet = prepare_research_packet(TOPIC, sources(), as_of=AS_OF,
            request=lambda _s, _p, stage: design() if stage == "designing" else evidence(),
            retrieve_sources=False)
        def busy_preparation(*_args, request, on_checkpoint, **_kwargs):
            on_checkpoint(packet)
            for _ in range(10):
                request("Extraction", {"previous_items": []}, "organizing")
            return packet
        client = self.client()
        with patch("src.deep_research_workflow.prepare_research_packet", side_effect=busy_preparation):
            result = self.generate(client, budget={"max_calls": 6})
        stages = [row["stage"] for row in result["metadata"]["research_execution"]["stages"]]
        self.assertEqual(stages, ["organizing"] * 2 + ["planning", "writing", "reviewing", "auditing"])
        self.assertTrue(result["metadata"]["report_has_body"])
        self.assertEqual(result["metadata"]["failure_reason"], "writing_budget_reserved")
        self.assertEqual(client.chat.completions.create.call_count, 6)

    def test_preparation_timeout_leaves_time_for_body_and_review(self):
        from src.research import ResearchError
        now = [0.0]
        client = self.client()
        original = client.chat.completions.create.side_effect
        def slow_extraction(**kwargs):
            value = original(**kwargs)
            if "previous_items" in self.payloads[-1]:
                # Preparation has at most one quarter of this short budget.
                now[0] = 49.0
                raise ResearchError("generation_timeout")
            return value
        client.chat.completions.create.side_effect = slow_extraction
        with patch("src.deep_research_workflow.time.monotonic", side_effect=lambda: now[0]):
            result = self.generate(client, budget={"timeout_seconds": 120})
        self.assertTrue(result["metadata"]["report_has_body"])
        self.assertEqual(result["metadata"]["failure_reason"], "generation_timeout")
        self.assertTrue(any("writer_draft" in payload for payload in self.payloads))
        self.assertLess(result["metadata"]["research_execution"]["duration_seconds"], 120)

    def test_slow_extraction_is_allowed_beyond_old_four_minute_limit(self):
        now = [0.0]
        client = self.client()
        original = client.chat.completions.create.side_effect
        def slow_extraction(**kwargs):
            value = original(**kwargs)
            if "previous_items" in self.payloads[-1]:
                now[0] += 400
            return value
        client.chat.completions.create.side_effect = slow_extraction
        with patch("src.deep_research_workflow.time.monotonic", side_effect=lambda: now[0]):
            result = self.generate(client)
        self.assertTrue(result["metadata"]["report_has_body"])
        self.assertNotIn("failure_reason", result["metadata"])
        self.assertTrue(result["metadata"]["research_packet"]["evidence"]["items"])

    def test_review_checkpoints_do_not_spend_calls_and_revision_reserves_final_audit(self):
        from src.deep_research_workflow import ResearchBudgetError, generate_deep_research_report
        client = self.client()
        saved = []
        def writer(_topic, _sources, *, request_guard, on_draft, **_kwargs):
            from src.argumentative_workflow import _body
            request_guard("planning")
            request_guard("writing")
            value = {"title": essay()["title"], "body": _body(essay(), _sources, AS_OF,
                plan=plan(), stage="writer_saved"),
                "metadata": {"workflow_stage": "writer_saved", "structured_draft": essay(),
                    "review_status": "pending", "writing_style": "argumentative", "argument_plan": plan()}}
            on_draft(copy.deepcopy(value))
            self.assertEqual(client.chat.completions.create.call_count, 2)
            request_guard("reviewing")
            value["metadata"].update(workflow_stage="review_saved", review_status="needs_review")
            on_draft(copy.deepcopy(value))
            self.assertEqual(client.chat.completions.create.call_count, 2)
            with self.assertRaises(ResearchBudgetError):
                request_guard("revising")
            return value
        with patch("src.research_workflow.generate_research_workflow", side_effect=writer):
            result = generate_deep_research_report(TOPIC, sources(), api_key="synthetic-key", as_of=AS_OF,
                retrieve_sources=False, client=client, budget={"max_calls": 6}, on_draft=saved.append)
        self.assertTrue(any(row["metadata"]["workflow_stage"] == "review_saved" for row in saved))
        self.assertEqual([row["stage"] for row in result["metadata"]["research_execution"]["stages"]],
            ["designing", "organizing", "planning", "writing", "reviewing", "auditing"])
        self.assertTrue(result["metadata"]["claim_audit"]["complete"])

    def test_fatal_reviewer_status_prevents_final_paid_audit(self):
        from src.analysis_errors import ProviderAPIError
        for status in (401, 402, 403, 404):
            for sse in (False, True):
                with self.subTest(status=status, sse=sse):
                    client = self.client()
                    original = client.chat.completions.create.side_effect
                    def rejected_review(**kwargs):
                        value = original(**kwargs)
                        if "writer_draft" in self.payloads[-1]:
                            if sse:
                                error = RuntimeError("synthetic-provider-secret")
                                error.body = {"code": status, "message": "synthetic-provider-secret"}
                                raise error
                            raise ProviderAPIError(status)
                        return value
                    client.chat.completions.create.side_effect = rejected_review
                    result = self.generate(client)
                    self.assertEqual(client.chat.completions.create.call_count, 5)
                    self.assertTrue(result["metadata"]["report_has_body"])
                    self.assertFalse(result["metadata"]["claim_audit"]["complete"])
                    self.assertEqual(result["metadata"]["claim_audit"]["error_reason"], f"provider_http_{status}")
                    self.assertEqual(result["metadata"]["review_status"], "review_failed")
                    self.assertNotIn("synthetic-provider-secret", json.dumps(result))

    def test_denied_optional_revision_uses_reserved_final_audit_with_real_writer(self):
        client = self.client(revise=True)
        original = client.chat.completions.create.side_effect
        saved = []
        def low_score_review(**kwargs):
            value = original(**kwargs)
            if "writer_draft" in self.payloads[-1]:
                value.value["quality"]["reasoning"] = 2
            return value
        client.chat.completions.create.side_effect = low_score_review
        result = self.generate(client, budget={"max_calls": 6}, on_draft=saved.append)
        self.assertEqual(client.chat.completions.create.call_count, 6)
        self.assertEqual(result["metadata"]["review_error_reason"], "generation_timeout")
        self.assertEqual(result["metadata"]["review_history"][0]["verdict"], "revise")
        self.assertTrue(any(row["metadata"]["workflow_stage"] == "review_saved" for row in saved))
        self.assertEqual(result["metadata"]["structured_draft"]["title"], "Revised operating outlook")
        self.assertTrue(result["metadata"]["claim_audit"]["complete"])
        self.assertEqual([row["stage"] for row in result["metadata"]["research_execution"]["stages"]],
            ["designing", "organizing", "planning", "writing", "reviewing", "auditing"])

    def test_review_stage_timeout_still_uses_available_final_audit(self):
        from src.research import ResearchError
        client = self.client()
        original = client.chat.completions.create.side_effect
        def timed_out_review(**kwargs):
            value = original(**kwargs)
            if "writer_draft" in self.payloads[-1]:
                raise ResearchError("generation_timeout")
            return value
        client.chat.completions.create.side_effect = timed_out_review
        result = self.generate(client, budget={"max_calls": 6})
        self.assertEqual(client.chat.completions.create.call_count, 6)
        self.assertEqual(result["metadata"]["review_error_reason"], "generation_timeout")
        self.assertEqual(result["metadata"]["review_status"], "review_failed")
        self.assertTrue(result["metadata"]["claim_audit"]["complete"])
        self.assertTrue(result["metadata"]["report_has_body"])

    def test_review_retry_keeps_audit_slot_and_uses_spare_call_only(self):
        from src.analysis_errors import ProviderAPIError
        for max_calls in (6, 7):
            with self.subTest(max_calls=max_calls):
                client = self.client()
                original = client.chat.completions.create.side_effect
                failed = [False]
                def fail_review_once(**kwargs):
                    value = original(**kwargs)
                    if "writer_draft" in self.payloads[-1] and not failed[0]:
                        failed[0] = True
                        raise ProviderAPIError(503)
                    return value
                client.chat.completions.create.side_effect = fail_review_once
                result = self.generate(client, budget={"max_calls": max_calls})
                stages = [row["stage"] for row in result["metadata"]["research_execution"]["stages"]]
                self.assertEqual(stages, ["designing", "organizing", "planning", "writing"]
                    + ["reviewing"] * (max_calls - 5) + ["auditing"])
                self.assertEqual(client.chat.completions.create.call_count, max_calls)
                self.assertTrue(result["metadata"]["claim_audit"]["complete"])
                self.assertEqual(result["metadata"]["review_status"],
                    "review_failed" if max_calls == 6 else "needs_review")

    def test_writer_retry_preserves_first_review_and_audit_calls(self):
        from src.analysis_errors import ProviderAPIError
        for max_calls in (6, 7):
            with self.subTest(max_calls=max_calls):
                client = self.client()
                original = client.chat.completions.create.side_effect
                failed = [False]
                def fail_writer_once(**kwargs):
                    value = original(**kwargs)
                    payload = self.payloads[-1]
                    if "argument_plan" in payload and "writer_draft" not in payload and not failed[0]:
                        failed[0] = True
                        raise ProviderAPIError(503)
                    return value
                client.chat.completions.create.side_effect = fail_writer_once
                result = self.generate(client, budget={"max_calls": max_calls})
                stages = [row["stage"] for row in result["metadata"]["research_execution"]["stages"]]
                self.assertEqual(stages, ["designing", "organizing", "planning", "writing"]
                    + (["writing", "reviewing", "auditing"] if max_calls == 7 else []))
                self.assertEqual(client.chat.completions.create.call_count, 7 if max_calls == 7 else 4)
                self.assertEqual(result["metadata"]["report_has_body"], max_calls == 7)
                if max_calls == 7:
                    self.assertTrue(result["metadata"]["claim_audit"]["complete"])

    def test_review_format_recovery_uses_only_a_spare_call_and_preserves_audit(self):
        for max_calls in (6, 7):
            with self.subTest(max_calls=max_calls):
                client = self.client()
                original = client.chat.completions.create.side_effect
                failed = [False]
                def invalid_review_once(**kwargs):
                    value = original(**kwargs)
                    if "writer_draft" in self.payloads[-1] and not failed[0]:
                        failed[0] = True
                        return Stream({})
                    return value
                client.chat.completions.create.side_effect = invalid_review_once
                saved = []
                result = self.generate(client, budget={"max_calls": max_calls}, on_draft=saved.append)
                stages = [row["stage"] for row in result["metadata"]["research_execution"]["stages"]]
                self.assertEqual(stages, ["designing", "organizing", "planning", "writing"]
                    + ["reviewing"] * (max_calls - 5) + ["auditing"])
                self.assertEqual(client.chat.completions.create.call_count, max_calls)
                self.assertTrue(result["metadata"]["claim_audit"]["complete"])
                self.assertTrue(result["metadata"]["report_has_body"])
                self.assertEqual(result["metadata"]["review_status"],
                    "review_failed" if max_calls == 6 else "needs_review")
                self.assertEqual(saved[-1]["metadata"]["workflow_stage"],
                    "review_failed" if max_calls == 6 else "review_complete")

    def test_fatal_status_after_writer_text_saves_body_without_more_requests(self):
        from src.analysis_errors import ProviderAPIError
        for status in (401, 402, 403, 404):
            for sse in (False, True):
                with self.subTest(status=status, sse=sse):
                    saved = []
                    client = self.client()
                    original = client.chat.completions.create.side_effect
                    class FatalStream(Stream):
                        def __iter__(self):
                            yield SimpleNamespace(choices=[SimpleNamespace(
                                delta=SimpleNamespace(content=json.dumps(self.value)), finish_reason=None)])
                            if sse:
                                error = RuntimeError("synthetic-fatal-secret")
                                error.body = {"error": {"code": status, "message": "synthetic-fatal-secret"}}
                                raise error
                            raise ProviderAPIError(status)
                    def fatal_writer(**kwargs):
                        value = original(**kwargs)
                        payload = self.payloads[-1]
                        if "argument_plan" in payload and "writer_draft" not in payload:
                            return FatalStream(value.value)
                        return value
                    client.chat.completions.create.side_effect = fatal_writer
                    result = self.generate(client, on_draft=saved.append)
                    self.assertEqual(client.chat.completions.create.call_count, 4)
                    self.assertTrue(result["metadata"]["report_has_body"])
                    self.assertEqual(result["metadata"]["structured_draft"]["title"], essay()["title"])
                    self.assertEqual(result["metadata"]["review_error_reason"], f"provider_http_{status}")
                    self.assertEqual(saved[-1]["metadata"]["review_status"], "review_failed")
                    self.assertFalse(result["metadata"]["claim_audit"]["complete"])
                    self.assertEqual(result["metadata"]["claim_audit"]["error_reason"], f"provider_http_{status}")
                    self.assertNotIn("synthetic-fatal-secret", json.dumps(result))

    def test_cancel_after_writer_text_saves_body_without_more_requests(self):
        callbacks, saved = [], []
        client = self.client()
        original = client.chat.completions.create.side_effect
        class CancelledStream(Stream):
            def __iter__(self):
                yield SimpleNamespace(choices=[SimpleNamespace(
                    delta=SimpleNamespace(content=json.dumps(self.value)), finish_reason=None)])
                for callback in callbacks:
                    callback()
                raise RuntimeError("synthetic-cancel-secret")
        def cancelled_writer(**kwargs):
            value = original(**kwargs)
            payload = self.payloads[-1]
            if "argument_plan" in payload and "writer_draft" not in payload:
                return CancelledStream(value.value)
            return value
        client.chat.completions.create.side_effect = cancelled_writer
        result = self.generate(client, register_cancel=callbacks.append, on_draft=saved.append)
        self.assertEqual(client.chat.completions.create.call_count, 4)
        self.assertTrue(result["metadata"]["report_has_body"])
        self.assertEqual(saved[-1]["metadata"]["structured_draft"]["title"], essay()["title"])
        self.assertEqual(result["metadata"]["review_error_reason"], "generation_timeout")
        self.assertFalse(result["metadata"]["claim_audit"]["complete"])
        self.assertNotIn("synthetic-cancel-secret", json.dumps(result))

    def test_cancellation_after_review_does_not_start_final_audit(self):
        callbacks = []
        client = self.client()
        original = client.chat.completions.create.side_effect
        def cancel_after_review(**kwargs):
            value = original(**kwargs)
            if "writer_draft" in self.payloads[-1]:
                callbacks[0]()
            return value
        client.chat.completions.create.side_effect = cancel_after_review
        result = self.generate(client, register_cancel=callbacks.append)
        self.assertEqual(client.chat.completions.create.call_count, 5)
        self.assertFalse(result["metadata"]["claim_audit"]["complete"])
        self.assertEqual(result["metadata"]["claim_audit"]["error_reason"], "generation_timeout")
        self.assertTrue(result["metadata"]["report_has_body"])

    def test_cancelled_preparation_does_not_start_another_request(self):
        callbacks = []
        client = self.client()
        original = client.chat.completions.create.side_effect
        def cancel_during_extraction(**kwargs):
            value = original(**kwargs)
            if "previous_items" in self.payloads[-1]:
                callbacks[0]()
                raise RuntimeError("closed transport")
            return value
        client.chat.completions.create.side_effect = cancel_during_extraction
        result = self.generate(client, register_cancel=callbacks.append)
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertFalse(result["metadata"]["report_has_body"])
        self.assertEqual(result["metadata"]["workflow_stage"], "preparation_failed")

    def test_thirty_source_failure_shape_still_generates_evidence_bounded_body(self):
        # The real regression saved a design for 30 sources, then failed on the
        # first organizing request with no rounds or evidence saved.
        rows = [copy.deepcopy(sources()[index % 2]) for index in range(30)]
        for index, row in enumerate(rows, 1):
            row["id"] = f"S{index}"
        client = self.client(fail_at=2)
        result = generate_research_workflow(TOPIC, rows, api_key="synthetic-key", as_of=AS_OF,
            deep_research=True, writing_style="argumentative", report_mode="argumentative",
            retrieve_sources=False, client=client)
        self.assertEqual(len(result["sources"]), 30)
        self.assertEqual(result["metadata"]["research_packet"]["evidence"]["items"], [])
        self.assertTrue(result["metadata"]["structured_draft"])
        self.assertEqual(result["metadata"]["answer_status"], "unanswered")
        self.assertNotIn("正文尚未生成", result["body"])

    def test_durable_save_failure_stops_before_next_model_request(self):
        client = self.client()
        def fail(_): raise OSError("synthetic disk failure")
        with self.assertRaisesRegex(OSError, "synthetic disk failure"):
            self.generate(client, on_draft=fail)
        self.assertEqual(client.chat.completions.create.call_count, 1)


class PreparationTests(unittest.TestCase):
    def numeric_packet(self, *, primary=False):
        from test_research_design import numeric
        rows = [numeric(metric="OCF", value="120", quote="Alpha consolidated 2026Q2 OCF was 120 USD million."),
                numeric(id="E2", metric="CapEx", value="80", quote="Alpha consolidated 2026Q2 CapEx was 80 USD million.")]
        source_rows = [sources()[0]]
        source_rows[0]["content"] = "\n".join(row["quote"] for row in rows)
        if primary:
            source_rows[0].update(url="https://www.sec.gov/Archives/example.htm", content_scope="html_excerpt")
        research_design = design()
        research_design["requirements"][0]["kind"] = "numeric"
        research_design["methods"] = ["cash_flow"]
        responses = iter([research_design, {"items": rows}, {"requests": [{"id": "C1", "kind": "cashflow_coverage",
            "operand_ids": {"ocf": "E1", "capex": "E2"}}], "limitations": []}])
        return prepare_research_packet(TOPIC, source_rows, as_of=AS_OF,
            request=lambda *args: next(responses), retrieve_sources=False)

    def test_secondary_numeric_anchor_remains_an_original_document_gap(self):
        packet = self.numeric_packet()
        self.assertEqual(packet["answer_status"], "limited")
        self.assertEqual(packet["evidence"]["requirements"][0]["source_basis"], "original_document_missing")
        self.assertTrue(packet["evidence"]["items"][0]["eligible"])  # usable as an attributed observation
        self.assertEqual(packet["calculations"][0]["result"], {"coverage_ratio": "1.5", "fcf": "40"})

    def test_primary_excerpt_allows_traceable_calculation_without_claiming_verification(self):
        packet = self.numeric_packet(primary=True)
        self.assertEqual(packet["answer_status"], "full")
        self.assertEqual(packet["calculations"][0]["inputs"]["ocf"]["value"], "120")
        self.assertFalse(packet["independently_verified"])
        self.assertEqual(packet["evidence"]["items"][0]["source_basis"], "primary_document_candidate")

    def test_design_fallback_cannot_claim_original_question_answered(self):
        response = iter(["invalid json", evidence()])
        packet = prepare_research_packet(TOPIC, sources(), as_of=AS_OF,
            request=lambda *args: next(response), retrieve_sources=False)
        self.assertEqual(packet["evidence"]["status"], "full")
        self.assertEqual(packet["answer_status"], "limited")
        self.assertIn("研究設計格式不符", render_packet_appendix(packet))

    def test_future_publication_is_removed_before_model_sees_it(self):
        source_rows = sources()
        # A stale date field must not hide a newer known timestamp.
        source_rows[1].update(publication_date="2026-10-10", published_at="2026-10-15")
        payloads = []
        def request(system, payload, stage):
            payloads.append(payload)
            return design() if stage == "designing" else {"items": [evidence()["items"][0]]}
        packet = prepare_research_packet(TOPIC, source_rows, as_of=AS_OF, request=request, retrieve_sources=False)
        self.assertEqual([s["id"] for s in packet["sources"]], ["S1"])
        self.assertEqual(len(packet["input_sources"]), 2)
        self.assertEqual(packet["excluded_sources"][0]["reason"], "published_after_cutoff")
        self.assertEqual(len(payloads[0]["available_sources"]), 1)

    def test_new_source_is_checkpointed_before_next_extraction_fails(self):
        added = copy.deepcopy(sources()[1]); added["id"] = "S3"
        added["retrieval"] = {"document_sha256": "a" * 64, "selection_spans": [[0, 10]]}
        snapshots = []
        def request(system, payload, stage):
            if stage == "designing": return design()
            if any(row["id"] == "S3" for row in payload["sources"]): raise RuntimeError("extraction unavailable")
            return {"items": []}
        with patch("src.research_discovery.supplement_sources", return_value={
                "sources": [*sources(), added], "outcomes": [], "attempted_urls": [], "added_count": 1}):
            with self.assertRaises(RuntimeError):
                prepare_research_packet(TOPIC, sources(), as_of=AS_OF, request=request, on_checkpoint=snapshots.append)
        self.assertEqual(len(snapshots[-1]["sources"]), 3)
        self.assertEqual(snapshots[-1]["source_captures"]["S3"]["document_sha256"], "a" * 64)
        self.assertEqual(len(snapshots[0]["sources"]), 2)  # immutable callback value

    def test_missing_evidence_stops_when_no_new_sources(self):
        with patch("src.research_discovery.supplement_sources", return_value={
                "sources": sources(), "outcomes": [], "attempted_urls": [], "added_count": 0}) as supplement:
            packet = prepare_research_packet(TOPIC, sources(), as_of=AS_OF,
                request=lambda system, payload, stage: design() if stage == "designing" else {"items": []})
        self.assertEqual(supplement.call_count, 1)
        self.assertEqual(packet["answer_status"], "unanswered")
        self.assertEqual(packet["stop_reason"], "no_new_sources")

    def test_rendered_calculations_keep_each_result_unit_and_input(self):
        responses = iter([design(), evidence()])
        packet = prepare_research_packet(TOPIC, sources(), as_of=AS_OF,
            request=lambda *args: next(responses), retrieve_sources=False)
        packet["calculations"] = [{"id": "C1", "kind": "cashflow_coverage", "status": "computed",
            "formula": "coverage_ratio = ocf / capex; fcf = ocf - capex", "unit": "USD million",
            "result": {"coverage_ratio": "1.5", "fcf": "40"},
            "result_units": {"coverage_ratio": "multiple", "fcf": "USD million"},
            "inputs": {"ocf": {"value": "120"}, "capex": {"value": "80"}},
            "source_ids": ["S1", "S2"], "period": "2026Q2", "entity": "A", "scope": "consolidated"}]
        body = render_packet_appendix(packet)
        self.assertIn("1.5 multiple", body)
        self.assertIn("40 USD million", body)
        self.assertNotIn("1.5 USD million", body)
        self.assertIn("120", body)
        self.assertIn("S1、S2", body)


if __name__ == "__main__":
    unittest.main()
