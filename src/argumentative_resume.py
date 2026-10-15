"""Resume saved research through the existing reviewer; never regenerate prose locally."""
from __future__ import annotations

import copy
import hashlib
import json
import time

from .analysis_errors import provider_failure_reason, provider_status_code
from .deep_research_workflow import audit_paragraphs, render_deep_research_body
from .research import ResearchError
from .research_packet import packet_findings


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def resume_review(saved_report, *, api_key, provider=None, model=None,
        on_progress=None, register_cancel=None, on_draft=None, client=None,
        request_guard=None, overall_deadline=None, max_calls=1):
    """Review only an unedited, structured deep argumentative snapshot.

    Every request and retry uses the normal workflow and shared guard. The
    default single-call cap includes malformed-response and transport retries.
    This entry point never starts another claim audit or modifies source prose.
    """
    from .argumentative_workflow import generate_argumentative_report, _body
    from .research_workflow import _safe_review_error

    if type(max_calls) is not int or not 1 <= max_calls <= 4:
        raise ValueError("續審調用上限須為 1 至 4。")
    snapshot = copy.deepcopy(saved_report)
    # Exact generated-body matching rejects human edits before any paid call.
    render_deep_research_body(snapshot)
    original = snapshot["metadata"]
    if (original.get("writing_style") != "argumentative"
            or not isinstance(original.get("research_brief"), dict)):
        raise ValueError("續審需要已保存的深度議論文結構化快照。")
    original_draft = copy.deepcopy(original["structured_draft"])
    original_hash = _hash(original_draft)
    original_audit = copy.deepcopy(original.get("claim_audit", original["research_packet"].get("claim_audit", {})))
    prepared = copy.deepcopy(original["research_packet"])
    provider = provider or snapshot.get("provider") or original.get("provider") or "deepseek"
    model = model or snapshot.get("model") or original.get("model")
    date = prepared["design"]["as_of"]
    topic = snapshot.get("topic") or prepared["design"]["topic"]
    prior_calls = original.get("api_call_count", 0)
    if type(prior_calls) is not int or prior_calls < 0:
        raise ValueError("已保存的模型調用紀錄無效。")
    history_count = len(original.get("review_history", []))
    started, attempts = time.monotonic(), []
    persistence_error, latest, latest_local = None, None, None

    def guard(stage):
        if stage not in {"reviewing", "revising"} or len(attempts) >= max_calls:
            raise ResearchError("generation_timeout")
        if overall_deadline is not None and time.monotonic() >= overall_deadline:
            raise ResearchError("generation_timeout")
        limit = request_guard(stage) if request_guard is not None else None
        attempts.append({"stage": stage, "attempt": prior_calls + len(attempts) + 1, "resumed": True})
        return limit

    def finish(value):
        result = copy.deepcopy(value)
        # Prove the exact received core before adding deterministic findings.
        # A reviewer may independently return only some of those findings.
        render_deep_research_body(result)
        metadata = result["metadata"]
        changed = _hash(metadata["structured_draft"]) != original_hash
        packet = metadata["research_packet"]
        if changed:
            audit = {"claims": [], "complete": False, "independently_verified": False,
                "status": "stale_after_revision", "unresolved_count": max(1, len(audit_paragraphs(
                    metadata["structured_draft"], "argumentative"))),
                "summary": "審稿已修改正文；先前逐項核驗不再適用，須重新核驗。",
                "previous_draft_hash": original_audit.get("draft_hash") or original_hash,
                "current_draft_hash": _hash(metadata["structured_draft"])}
            metadata["claim_audit"] = audit
            packet["claim_audit"] = copy.deepcopy(audit)
            # A second review, if explicitly budgeted, sees that the old claims
            # no longer apply to the adopted draft, rather than reusing them.
            prepared.setdefault("claim_audit", {}).clear()
            prepared["claim_audit"].update(copy.deepcopy(audit))
        elif original_audit:
            metadata["claim_audit"] = copy.deepcopy(original_audit)
            packet["claim_audit"] = copy.deepcopy(original_audit)
        packet["packet_hash"] = _hash({key: item for key, item in packet.items() if key != "packet_hash"})
        prepared["packet_hash"] = _hash({key: item for key, item in prepared.items() if key != "packet_hash"})
        # Reviewer findings are replaced on adoption; deterministic evidence
        # gaps still apply to the unchanged research packet after any revision.
        findings = list(metadata.get("review_findings", []))
        for finding in packet_findings(packet):
            if finding not in findings:
                findings.append(finding)
        metadata["review_findings"] = findings
        audit = metadata.get("claim_audit", {})
        if metadata.get("review_status") != "review_failed" and (changed or not audit.get("complete")
                or audit.get("unresolved_count") or packet["answer_status"] != "full"
                or metadata.get("validation_warnings") or metadata.get("preparation_warnings")
                or any(row.get("severity") in {"warning", "error"} for row in metadata.get("review_findings", []))):
            metadata.update(review_status="needs_review", quality_status="needs_review")
        metadata.update(api_call_count=prior_calls + len(attempts), report_has_body=True)
        metadata.setdefault("data_preparation", {})["review_resume"] = {
            "source_draft_hash": original_hash, "max_calls": max_calls, "api_call_count": len(attempts)}
        for row in metadata.get("review_history", [])[history_count:]:
            row["api_call_count"] = prior_calls + row["api_call_count"]
        execution = copy.deepcopy(original.get("research_execution", {}))
        execution.update(api_call_count=prior_calls + len(attempts),
            stages=[*execution.get("stages", []), *copy.deepcopy(attempts)],
            duration_seconds=round(execution.get("duration_seconds", 0) + time.monotonic() - started, 2))
        metadata["research_execution"] = execution
        result["sources"] = copy.deepcopy(snapshot["sources"])
        result["as_of"] = date
        result["body"] = _body(metadata["structured_draft"], result["sources"], date,
            plan=metadata["argument_plan"], warnings=metadata.get("validation_warnings", []),
            findings=metadata["review_findings"],
            summary=metadata.get("review_error", "") if metadata["workflow_stage"] == "review_failed"
                else metadata.get("review_summary", ""), stage=metadata["workflow_stage"])
        result["body"] = render_deep_research_body(result)
        return result

    def checkpoint(value):
        nonlocal latest, latest_local, persistence_error
        latest_local = copy.deepcopy(value)
        result = finish(value)
        if on_draft:
            try:
                on_draft(copy.deepcopy(result))
            except Exception as error:
                persistence_error = error
                raise
        latest = result

    try:
        result = generate_argumentative_report(topic, snapshot["sources"], api_key=api_key,
            provider=provider, model=model, as_of=date, on_progress=on_progress,
            register_cancel=register_cancel, on_draft=checkpoint, client=client,
            retrieve_sources=False, prepared_research=prepared, request_guard=guard,
            overall_deadline=overall_deadline, _resume_snapshot=snapshot, _resume_max_calls=max_calls)
    except Exception as error:
        if error is persistence_error:
            raise
        # A deadline after a received review still returns the last checkpoint;
        # a failed request otherwise preserves the original structured draft.
        result = copy.deepcopy(latest_local or snapshot)
        result.pop("version", None)
        metadata = result["metadata"]
        status = provider_status_code(error)
        reason = f"provider_http_{status}" if status is not None else (
            error.reason if isinstance(error, ResearchError) else provider_failure_reason(error))
        summary = _safe_review_error(error)
        metadata.update(workflow_stage="review_failed", review_status="review_failed",
            quality_status="review_failed", review_error=summary, review_error_reason=reason)
        result["body"] = _body(metadata["structured_draft"], snapshot["sources"], date,
            plan=metadata["argument_plan"], warnings=metadata.get("validation_warnings", []),
            findings=metadata.get("review_findings", []), summary=summary, stage="review_failed")
    checkpoint(result)
    return latest
