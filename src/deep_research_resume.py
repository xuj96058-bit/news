"""Continue only the claim audit of an unchanged saved research snapshot."""
from __future__ import annotations

import copy
import math
import time
from threading import Event

from openai import OpenAI

from .analysis_errors import ProviderConfigurationError
from .llm_providers import get_provider, validate_provider_model
from .research import ResearchError
from .research_audit_workflow import request_research_stage, run_claim_audit
from .research_timing import STAGE_SECONDS


def resume_audit(saved_report, *, api_key, provider=None, model=None,
        on_progress=None, register_cancel=None, on_draft=None, client=None,
        request_guard=None, overall_deadline=None, max_calls=1, timeout_seconds=None):
    """A single explicitly budgeted audit; no preparation, writing or review."""
    from .deep_research_workflow import render_deep_research_body
    from .research_audit_workflow import finalize_deep_audit

    if type(max_calls) is not int or max_calls != 1:
        raise ValueError("續做逐項核驗只允許一次模型調用。")
    if timeout_seconds is None:
        timeout_seconds = STAGE_SECONDS["auditing"]
    if (type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 3600):
        raise ValueError("逐項核驗時間上限須大於零且不超過 3600 秒。")
    if (overall_deadline is not None and (type(overall_deadline) not in {int, float}
            or not math.isfinite(overall_deadline))):
        raise ValueError("逐項核驗截止時間無效。")
    snapshot = copy.deepcopy(saved_report)
    render_deep_research_body(snapshot)
    metadata = snapshot["metadata"]
    if metadata.get("workflow_stage") not in {"review_complete", "review_failed"}:
        raise ValueError("請先完成正文的自動審稿流程，再續做逐項核驗。")
    prior_calls = metadata.get("api_call_count", 0)
    execution = copy.deepcopy(metadata.get("research_execution", {}))
    preparation = copy.deepcopy(metadata.get("data_preparation", {}))
    if (type(prior_calls) is not int or prior_calls < 0 or not isinstance(execution, dict)
            or not isinstance(execution.get("stages", []), list) or not isinstance(preparation, dict)):
        raise ValueError("已保存的研究執行紀錄無效。")
    duration = execution.get("duration_seconds", 0)
    if type(duration) not in {int, float} or not math.isfinite(duration) or duration < 0:
        raise ValueError("已保存的研究時間紀錄無效。")
    if not isinstance(api_key, str) or not api_key.strip() or "\n" in api_key or "\r" in api_key:
        raise ProviderConfigurationError("missing_key")
    provider = provider or snapshot.get("provider") or metadata.get("provider") or "deepseek"
    selected = get_provider(provider)
    model = validate_provider_model(provider,
        model or snapshot.get("model") or metadata.get("model") or selected.models[0])
    key = api_key.strip()
    started = time.monotonic()
    deadline = started + timeout_seconds
    if overall_deadline is not None:
        deadline = min(deadline, overall_deadline)
    cancelled, attempts = Event(), []
    owned = False
    packet = copy.deepcopy(metadata["research_packet"])
    style = metadata.get("writing_style", "legacy")
    if metadata.get("report_mode") == "argumentative":
        style = "argumentative"

    def guard(stage):
        if stage != "auditing" or attempts or cancelled.is_set() or time.monotonic() >= deadline:
            raise ResearchError("generation_timeout")
        limit = deadline
        external_limit = request_guard(stage) if request_guard is not None else None
        if external_limit is not None:
            if type(external_limit) not in {int, float} or not math.isfinite(external_limit):
                raise ResearchError("generation_timeout")
            limit = min(limit, external_limit)
        if cancelled.is_set() or time.monotonic() >= limit:
            raise ResearchError("generation_timeout")
        attempts.append({"stage": stage, "attempt": prior_calls + 1, "resumed": True,
            "deadline_seconds": round(duration + limit - started, 2)})
        return limit

    def request(system, payload, stage):
        return request_research_stage(system, payload, stage, api_key=key,
            provider=provider, model=model, client=client, request_guard=guard,
            on_progress=on_progress, register_cancel=register_cancel)

    try:
        if register_cancel:
            register_cancel(cancelled.set)
        if provider != "anthropic":
            if client is None:
                client = OpenAI(api_key=key, base_url=selected.base_url, timeout=60, max_retries=0)
                owned = True
                if register_cancel:
                    register_cancel(client.close)
            elif isinstance(client, OpenAI) and (client.api_key != key
                    or str(client.base_url).rstrip("/") != selected.base_url.rstrip("/")):
                raise ProviderConfigurationError("invalid_model")
        audit = run_claim_audit(metadata["structured_draft"], style, packet, request=request)
        audit.update(provider=provider, model=model)
        total_calls = prior_calls + len(attempts)
        execution.update(api_call_count=total_calls,
            stages=[*execution.get("stages", []), *copy.deepcopy(attempts)],
            duration_seconds=round(duration + time.monotonic() - started, 2))
        preparation["audit_resume"] = {"max_calls": 1, "timeout_seconds": timeout_seconds,
            "api_call_count": len(attempts), "source_draft_hash": audit.get("draft_hash")}
        result = finalize_deep_audit(snapshot, packet, audit,
            metadata_update={"api_call_count": total_calls, "research_execution": execution,
                "data_preparation": preparation})
        if on_draft:
            on_draft(copy.deepcopy(result))
        return result
    finally:
        if owned:
            try:
                client.close()
            except Exception:
                pass
