"""Shared requests and claim audit handling for generation and audit continuation."""
from __future__ import annotations

import copy
import hashlib
import json
import time

from .analysis_errors import ProviderAPIError
from .llm_providers import claude_completion, configure_provider_request
from .research import (MAX_PROMPT_CHARS, ResearchError, _nonstream_content,
                       _progress, _stream_content)
from .research_audit import AUDIT_SYSTEM, parse_claim_audit


def request_research_stage(system, payload, stage, *, api_key, provider, model,
        client, request_guard, on_progress=None, register_cancel=None):
    """One provider attempt; the caller owns every call and deadline allowance."""
    user = json.dumps(payload, ensure_ascii=False)
    if len(system) + len(user) > MAX_PROMPT_CHARS:
        raise ResearchError("source_budget")
    deadline = request_guard(stage)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ResearchError("generation_timeout")
    _progress(on_progress, stage)
    if provider == "anthropic":
        value = _nonstream_content(claude_completion(model=model, api_key=api_key,
            system=system, user=user))
        if time.monotonic() >= deadline:
            raise ResearchError("generation_timeout")
        return value
    params = {"model": model, "messages": [{"role": "system", "content": system},
        {"role": "user", "content": user}], "response_format": {"type": "json_object"},
        "stream": True, "timeout": min(60, remaining)}
    if provider == "openai":
        params["max_completion_tokens"] = 16000
    elif provider == "deepseek":
        params.update(max_tokens=24000,
            extra_body={"thinking": {"type": "enabled"}, "reasoning_effort": "high"})
    else:
        params.update(max_tokens=12000, temperature=0.2)
    return _stream_content(client.chat.completions.create(**configure_provider_request(provider, params)),
        deadline=deadline, register_cancel=register_cancel, max_chunks=50000,
        on_progress=lambda _stage, size: _progress(on_progress, stage, size))


def run_claim_audit(draft, writing_style, packet, *, request, blocked_reason=None):
    """Use the same parser and conservative failure result for every audit."""
    from .deep_research_workflow import audit_paragraphs, _failure_reason

    paragraphs = audit_paragraphs(draft, writing_style)
    if not paragraphs:
        return {"claims": [], "summary": "草稿格式尚未通過，無法逐項核驗。", "complete": False,
            "unresolved_count": 1, "independently_verified": False}
    try:
        if blocked_reason in {"provider_http_401", "provider_http_402", "provider_http_403", "provider_http_404"}:
            raise ProviderAPIError(int(blocked_reason.rsplit("_", 1)[1]))
        cited = {identifier for row in paragraphs for identifier in row["source_ids"]}
        snapshots = [{"source_id": row["id"], "title": row.get("title", ""),
            "content": row.get("content", ""), "content_scope": row.get("content_scope", ""),
            "publication_date": row.get("publication_date", row.get("published_at", "")),
            "url": row.get("url", ""),
            "content_sha256": hashlib.sha256(row.get("content", "").encode("utf-8")).hexdigest()}
            for row in packet["sources"] if row["id"] in cited]
        raw = request(AUDIT_SYSTEM, {"paragraphs": paragraphs, "research_design": packet["design"],
            "evidence_items": packet["evidence"]["items"], "calculations": packet["calculations"],
            "snapshot_sources": snapshots}, "auditing")
        result = parse_claim_audit(raw, paragraphs, packet["evidence"]["items"], packet["calculations"])
    except Exception as error:
        result = {"claims": [{"section": row["section"], "paragraph": row["paragraph"],
            "status": "unknown", "evidence_ids": [], "calculation_ids": [],
            "reason": "逐項核驗未完成，保留待核對狀態。"} for row in paragraphs],
            "summary": "逐項核驗未完成。", "complete": False, "unresolved_count": len(paragraphs),
            "independently_verified": False, "error_kind": type(error).__name__,
            "error_reason": _failure_reason(error)}
    result["draft_hash"] = hashlib.sha256(json.dumps(draft, ensure_ascii=False,
        sort_keys=True).encode("utf-8")).hexdigest()
    return result


def finalize_deep_audit(report, packet, audit, *, metadata_update=None, allow_unstructured=False):
    """Apply audit metadata and render only after validating the prior snapshot."""
    from .deep_research_workflow import _audit_findings, _DEEP_GAP_NOTICE, render_deep_research_body
    from .research_packet import packet_findings, render_packet_appendix

    result = copy.deepcopy(report)
    metadata = result["metadata"]
    draft = metadata.get("structured_draft")
    if draft is not None:
        # Validate before changing findings; partial overlap between reviewer
        # and program findings cannot invalidate this exact known source body.
        render_deep_research_body(result)
    elif not allow_unstructured:
        raise ValueError("逐項核驗需要有效的結構化正文。")
    old_audit = metadata.get("claim_audit", metadata.get("research_packet", {}).get("claim_audit", {}))
    old_findings = _audit_findings(old_audit)
    findings = [row for row in metadata.get("review_findings", []) if row not in old_findings]
    packet = copy.deepcopy(packet)
    packet["claim_audit"] = copy.deepcopy(audit)
    packet["packet_hash"] = hashlib.sha256(json.dumps({key: value for key, value in packet.items()
        if key != "packet_hash"}, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    for finding in [*packet_findings(packet), *_audit_findings(audit)]:
        if finding not in findings:
            findings.append(finding)
    metadata.update(copy.deepcopy(metadata_update or {}))
    unresolved = (metadata.get("preparation_warnings") or packet.get("preparation_warnings")
        or packet["answer_status"] != "full" or not audit.get("complete")
        or audit.get("unresolved_count", 0) or metadata.get("validation_warnings")
        or any(row.get("severity") in {"warning", "error"} for row in findings))
    status = metadata.get("review_status")
    if status != "review_failed":
        status = "needs_review" if unresolved else "reviewed"
    metadata.update(research_packet=packet, claim_audit=copy.deepcopy(audit), review_findings=findings[:100],
        answer_status=packet["answer_status"], review_status=status,
        quality_status="reviewed_snapshot" if status == "reviewed" else status, report_has_body=True)
    result["sources"] = copy.deepcopy(packet["sources"])
    if draft is None:
        result["body"] = (_DEEP_GAP_NOTICE if unresolved else "") + result["body"] + render_packet_appendix(packet)
        return result
    date, stage = packet["design"]["as_of"], metadata["workflow_stage"]
    if metadata.get("writing_style") == "argumentative" or metadata.get("report_mode") == "argumentative":
        from .argumentative_workflow import _body
        result["body"] = _body(draft, result["sources"], date, plan=metadata["argument_plan"],
            warnings=metadata.get("validation_warnings", []), findings=metadata["review_findings"],
            summary=metadata.get("review_error", "") if stage == "review_failed" else metadata.get("review_summary", ""), stage=stage)
    else:
        from .research_workflow import _body, _FIELDS
        displayed = [row if row.get("section") in _FIELDS else {**row, "section": "executive_findings",
            "paragraph": 1, "message": str(row.get("section", "報告")) + "：" + row["message"]} for row in metadata["review_findings"]]
        result["body"] = _body(result["title"], draft, result["sources"], date, stage=stage,
            warnings=metadata.get("validation_warnings", []), findings=displayed,
            summary=metadata.get("review_summary", ""), error=metadata.get("review_error") or None,
            research_brief=metadata.get("research_brief"), report_mode=metadata.get("report_mode", "legacy"))
    result["body"] = render_deep_research_body(result)
    return result
