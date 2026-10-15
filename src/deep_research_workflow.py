"""One research preparation and audit pipeline, with existing writing adapters."""
from __future__ import annotations

import copy
import hashlib
import json
import time
from threading import Event

from openai import OpenAI

from .analysis_errors import ModelResponseError, ProviderAPIError, ProviderConfigurationError
from .llm_providers import claude_completion, configure_provider_request, get_provider, validate_provider_model
from .research import (MAX_PROMPT_CHARS, ResearchError, _date_label, _nonstream_content,
    _progress, _stream_content, _transport_timed_out, _validated_sources)
from .research_audit import AUDIT_SYSTEM, parse_claim_audit
from .research_packet import prepare_research_packet, render_packet_appendix, packet_findings

DEFAULT_BUDGET = {"max_rounds": 3, "max_calls": 16, "timeout_seconds": 1800}


class ResearchBudgetError(ResearchError):
    def __init__(self):
        super().__init__("generation_timeout")
        self.args = ("深度研究已達設定的時間或模型調用上限；已完成資料及草稿保留。",)


class PreparationBudgetError(ResearchBudgetError):
    """Stop optional preparation while there is still budget to write and review."""


def _failure_reason(error):
    """Only application-owned categories may enter persisted diagnostics."""
    if isinstance(error, PreparationBudgetError):
        return "writing_budget_reserved"
    if _transport_timed_out(error) or isinstance(error, ResearchBudgetError):
        return "generation_timeout"
    if isinstance(error, (ResearchError, ModelResponseError)):
        return error.reason
    status = getattr(error, "status_code", None)
    if type(status) is int and status in {400, 401, 402, 403, 404, 408, 429, 500, 502, 503, 504}:
        return f"provider_http_{status}"
    if isinstance(error, ProviderConfigurationError):
        return "provider_configuration"
    return "provider_or_processing_error"


def research_budget(value=None):
    if value is None:
        return dict(DEFAULT_BUDGET)
    if not isinstance(value, dict) or set(value) - set(DEFAULT_BUDGET):
        raise ValueError("研究預算格式不正確。")
    result = {**DEFAULT_BUDGET, **value}
    for key, lower, upper in (("max_rounds", 0, 3), ("max_calls", 6, 24), ("timeout_seconds", 120, 3600)):
        item = result[key]
        if isinstance(item, bool) or not isinstance(item, int) or not lower <= item <= upper:
            raise ValueError("研究預算超出支援範圍。")
    return result


def audit_paragraphs(draft, writing_style):
    if not draft:
        return []
    import re
    import unicodedata

    # Only exact structural labels are exempt. A topical or numeric heading may
    # assert a conclusion and must receive the same evidence checks as prose.
    structural = {
        "report", "researchreport", "researchdraft", "draft", "analysis", "overview",
        "introduction", "background", "summary", "executivesummary", "findings", "keyfindings",
        "evidence", "reasoning", "counterargument", "conclusion", "conclusions", "methodology",
        "limitations", "watchlist", "risks", "references", "appendix", "researchprepared",
        "研究报告", "研究報告", "研究草稿", "报告", "報告", "草稿", "研究資料準備", "研究资料准备",
        "資料缺口簡報", "资料缺口简报", "證據缺口簡報", "证据缺口简报", "引言", "背景", "概述",
        "摘要", "核心結論", "核心结论", "主要發現", "主要发现", "證據", "证据", "論證", "论证",
        "推理", "反方與替代解釋", "反方与替代解释", "結論", "结论", "方法", "研究方法",
        "限制", "資料限制", "资料限制", "後續觀察", "后续观察", "風險", "风险", "參考資料", "参考资料", "附錄", "附录",
    }

    def substantive(value):
        if not isinstance(value, str) or not value.strip():
            return False
        text = unicodedata.normalize("NFKC", value).strip()
        # Section numbering alone is not a financial assertion.
        text = re.sub(r"^(?:第?[一二三四五六七八九十]+[章节章節、.．:：)）]|\d+[.．:：)）]\s*)\s*", "", text)
        label = re.sub(r"[\W_]+", "", text).casefold()
        return bool(label) and label not in structural

    def sources_for(paragraphs):
        return list(dict.fromkeys(identifier for paragraph in paragraphs
            for identifier in paragraph.get("source_ids", []) if isinstance(identifier, str)))

    if writing_style == "argumentative":
        from .argumentation import essay_paragraphs
        all_paragraphs = list(essay_paragraphs(draft))
        paragraphs = []
        for section, index, paragraph in all_paragraphs:
            if section.endswith(".claim"):
                number = int(section.split(".")[1])
                heading = draft["arguments"][number - 1].get("heading")
                if substantive(heading):
                    prefix = f"arguments.{number}."
                    ids = sources_for([entry for key, _, entry in all_paragraphs if key.startswith(prefix)])
                    paragraphs.append({"section": prefix + "heading", "paragraph": 1,
                                       "text": heading, "source_ids": ids})
            paragraphs.append({"section": section, "paragraph": index, **paragraph})
        title_ids = sources_for([paragraph for _, _, paragraph in all_paragraphs])
    else:
        from .research_workflow import _FIELDS
        paragraphs = [{"section": section, "paragraph": index, **paragraph}
            for section in sorted(_FIELDS)
            for index, paragraph in enumerate(draft.get(section, []), 1)]
        title_ids = sources_for([paragraph for section in sorted(_FIELDS) for paragraph in draft.get(section, [])])
    if substantive(draft.get("title")):
        paragraphs.insert(0, {"section": "title", "paragraph": 1,
                              "text": draft["title"], "source_ids": title_ids})
    return paragraphs


def _audit_findings(audit):
    return [{"section": row["section"], "paragraph": row["paragraph"],
             "severity": "error" if row["status"] == "contradicted" else "warning",
             "message": ("逐項核驗：" + "；".join([*row.get("program_reasons", []),
                 row.get("reason", "此主張尚未獲充分支持。")]))[:1200], "source_ids": row.get("source_ids", [])}
            for row in audit.get("claims", []) if row["status"] != "supported"]


def generate_deep_research_report(topic, sources, *, api_key, provider="deepseek", model=None,
        as_of, instructions="", on_progress=None, register_cancel=None, on_draft=None,
        client=None, report_mode="argumentative", writing_style="argumentative",
        retrieve_sources=True, budget=None):
    """Paid requests are counted centrally, including writer/reviewer requests.

    A preparation memo and the initial prose are saved before subsequent paid
    stages. Failed audits cannot promote a report to a reviewed state.
    """
    limits = research_budget(budget)
    checked = _validated_sources(sources)
    if not isinstance(topic, str) or not 1 <= len(topic.strip()) <= 500:
        raise ResearchError("invalid_topic")
    if not _date_label(as_of):
        raise ResearchError("invalid_dates")
    if not isinstance(instructions, str) or len(instructions) > 4000:
        raise ResearchError("source_budget")
    if not isinstance(api_key, str) or not api_key.strip() or "\n" in api_key or "\r" in api_key:
        raise ProviderConfigurationError("missing_key")
    selected = get_provider(provider)
    model = validate_provider_model(provider, model or selected.models[0])
    topic, key = topic.strip(), api_key.strip()
    style = "argumentative" if writing_style == "argumentative" or report_mode == "argumentative" else "legacy"
    started = time.monotonic()
    deadline = started + limits["timeout_seconds"]
    calls, usage_stages = 0, []
    owned, saved, packet = False, None, None
    persistence_error = None
    active_stage = "preparing"
    failures = []
    cancelled = Event()
    writer_calls = 3 if style == "argumentative" else 2
    writer_seconds = min(720 if style == "argumentative" else 360, limits["timeout_seconds"] * 0.6)
    preparation_deadline = deadline - writer_seconds
    preparing = True

    def record_failure(error):
        item = {"stage": active_stage, "reason": _failure_reason(error)}
        if item not in failures:
            failures.append(item)
        return item

    def guard(stage):
        nonlocal calls, active_stage
        active_stage = stage
        if cancelled.is_set():
            raise ResearchError("generation_timeout")
        if time.monotonic() >= deadline or calls >= limits["max_calls"]:
            raise ResearchBudgetError()
        if preparing and (time.monotonic() >= preparation_deadline
                or calls >= limits["max_calls"] - writer_calls):
            raise PreparationBudgetError()
        calls += 1
        usage_stages.append({"stage": stage, "attempt": calls})

    def request(system, payload, stage):
        nonlocal active_stage
        active_stage = stage
        user = json.dumps(payload, ensure_ascii=False)
        if len(system) + len(user) > MAX_PROMPT_CHARS:
            raise ResearchError("source_budget")
        guard(stage)
        _progress(on_progress, stage)
        stage_deadline = min(preparation_deadline if preparing else deadline, time.monotonic() + 240)
        if provider == "anthropic":
            value = _nonstream_content(claude_completion(model=model, api_key=key, system=system, user=user))
            if time.monotonic() >= stage_deadline:
                raise ResearchBudgetError()
            return value
        params = {"model": model, "messages": [{"role": "system", "content": system},
            {"role": "user", "content": user}], "response_format": {"type": "json_object"},
            "stream": True, "timeout": min(60, stage_deadline - time.monotonic())}
        if provider == "openai":
            params["max_completion_tokens"] = 16000
        elif provider == "deepseek":
            params.update(max_tokens=24000, extra_body={"thinking": {"type": "enabled"}, "reasoning_effort": "high"})
        else:
            params.update(max_tokens=12000, temperature=0.2)
        return _stream_content(client.chat.completions.create(**configure_provider_request(provider, params)), deadline=stage_deadline,
            register_cancel=register_cancel, max_chunks=50000,
            on_progress=lambda _stage, size: _progress(on_progress, stage, size))

    def metadata():
        packet["packet_hash"] = hashlib.sha256(json.dumps({key: value for key, value in packet.items()
            if key != "packet_hash"}, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        warnings = [*packet.get("preparation_warnings", []), *failures]
        return {"deep_research": True, "research_packet": packet, "research_budget": limits,
            "research_execution": {"api_call_count": calls, "stages": list(usage_stages),
                "duration_seconds": round(time.monotonic()-started, 2)},
            "api_call_count": calls, "answer_status": packet["answer_status"],
            "workflow_schema_version": 2, "manually_verified": False,
            "preparation_warnings": warnings,
            **({"failure_stage": failures[-1]["stage"], "failure_reason": failures[-1]["reason"]} if failures else {})}

    def persist(draft):
        nonlocal saved, persistence_error
        draft = copy.deepcopy(draft)
        draft["sources"] = copy.deepcopy(packet["sources"])
        draft["metadata"] = {**draft.get("metadata", {}), **metadata()}
        if on_draft:
            try:
                on_draft(copy.deepcopy(draft))
            except Exception as error:
                persistence_error = error
                raise
        saved = draft

    def audit(draft, *, reserve_calls=0, reserve_seconds=0):
        paragraphs = audit_paragraphs(draft, style)
        if not paragraphs:
            return {"claims": [], "summary": "草稿格式尚未通過，無法逐項核驗。", "complete": False,
                    "unresolved_count": 1, "independently_verified": False}
        try:
            # An optional first-pass audit must not consume the review's slot.
            if calls >= limits["max_calls"] - reserve_calls or deadline - time.monotonic() <= reserve_seconds:
                raise ResearchBudgetError()
            raw = request(AUDIT_SYSTEM, {"paragraphs": paragraphs, "research_design": packet["design"],
                "evidence_items": packet["evidence"]["items"], "calculations": packet["calculations"]}, "auditing")
            result = parse_claim_audit(raw, paragraphs, packet["evidence"]["items"], packet["calculations"])
        except Exception as error:
            # A failed or exhausted audit is explicitly incomplete, not a pass.
            result = {"claims": [{"section": p["section"], "paragraph": p["paragraph"],
                "status": "unknown", "evidence_ids": [], "calculation_ids": [],
                "reason": "逐項核驗未完成，保留待核對狀態。"} for p in paragraphs],
                "summary": "逐項核驗未完成。", "complete": False, "unresolved_count": len(paragraphs),
                "independently_verified": False, "error_kind": type(error).__name__}
        result["draft_hash"] = hashlib.sha256(json.dumps(draft, ensure_ascii=False,
            sort_keys=True).encode("utf-8")).hexdigest()
        return result

    def writer_checkpoint(draft):
        draft["metadata"]["report_has_body"] = True
        persist(draft)  # Never spend an audit call before durable acknowledgement.
        packet["claim_audit"] = audit(draft.get("metadata", {}).get("structured_draft"),
            reserve_calls=1, reserve_seconds=min(240, limits["timeout_seconds"] * 0.2))

    def preparation_checkpoint(value):
        nonlocal packet
        packet = value
        from .research import _markdown_text
        memo = {"title": (topic + "｜研究資料準備")[:180], "sources": packet["sources"],
            "body": f"# {_markdown_text(topic)}\n\n資料截至：{as_of}\n\n> 研究準備紀錄；正文尚未生成。\n" + render_packet_appendix(packet),
            "metadata": {"workflow_stage": "research_prepared", "review_status": "pending",
                "quality_status": "pending", "report_mode": report_mode, "writing_style": style,
                "report_has_body": False}}
        persist(memo)

    try:
        if register_cancel:
            register_cancel(cancelled.set)
        if provider != "anthropic":
            if client is None:
                client = OpenAI(api_key=key, base_url=selected.base_url, timeout=60, max_retries=0)
                owned = True
                if register_cancel:
                    register_cancel(client.close)
            elif isinstance(client, OpenAI) and (client.api_key != key or str(client.base_url).rstrip("/") != selected.base_url.rstrip("/")):
                raise ProviderConfigurationError("invalid_model")
        try:
            packet = prepare_research_packet(topic, checked, as_of=as_of, request=request,
                retrieve_sources=retrieve_sources, max_rounds=limits["max_rounds"], deadline=preparation_deadline,
                on_progress=on_progress, on_checkpoint=preparation_checkpoint)
        except Exception as error:
            if error is persistence_error or cancelled.is_set() or time.monotonic() >= deadline:
                raise
            if isinstance(error, ProviderConfigurationError) or getattr(error, "status_code", None) in {401, 402, 403, 404}:
                raise
            failure = record_failure(error)
            if packet is None:
                # Design failure still leaves the supplied sources available to
                # the writer. Build an honest empty packet without a paid call.
                from .research_design import fallback_design
                packet = prepare_research_packet(topic, checked, as_of=as_of,
                    request=lambda _system, _payload, stage: fallback_design(topic, _date_label(as_of))
                        if stage == "designing" else {"items": []},
                    retrieve_sources=False, max_rounds=0)
            packet["stop_reason"] = "preparation_interrupted"
            packet["retrieval_outcomes"].append({**failure, "status": "incomplete"})
            if packet["answer_status"] == "full":
                packet["answer_status"] = "limited"
        finally:
            preparing = False
        preparation_checkpoint(packet)
        from .research_workflow import generate_research_workflow
        result = generate_research_workflow(topic, packet["sources"], api_key=key, provider=provider,
            model=model, as_of=as_of, instructions=instructions, on_progress=on_progress,
            register_cancel=register_cancel, on_draft=writer_checkpoint, client=client,
            report_mode=report_mode, writing_style=style, retrieve_sources=False,
            prepared_research=packet, request_guard=guard, overall_deadline=deadline)
        draft = result["metadata"].get("structured_draft")
        answer_scope = result["metadata"].get("argument_plan", {}).get("answer_scope")
        if answer_scope == "insufficient":
            packet["answer_status"] = "unanswered"
        elif answer_scope == "narrowed" and packet["answer_status"] == "full":
            packet["answer_status"] = "limited"
        final_audit = audit(draft)
        packet["claim_audit"] = final_audit
        packet["packet_hash"] = hashlib.sha256(json.dumps({key: value for key, value in packet.items()
            if key != "packet_hash"}, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        findings = [*result["metadata"].get("review_findings", []), *packet_findings(packet), *_audit_findings(final_audit)]
        unresolved = (bool(failures) or bool(packet.get("preparation_warnings"))
            or packet["answer_status"] != "full" or not final_audit.get("complete")
            or final_audit.get("unresolved_count", 0) or result["metadata"].get("validation_warnings")
            or any(row.get("severity") in {"warning", "error"} for row in findings))
        status = result["metadata"].get("review_status")
        if status != "review_failed":
            status = "needs_review" if unresolved else "reviewed"
        result["metadata"].update(metadata(), claim_audit=final_audit, review_findings=findings[:100],
            review_status=status, quality_status="reviewed_snapshot" if status == "reviewed" else status,
            report_has_body=True)
        if unresolved:
            result["body"] = "> 深度核驗仍有缺口；請結合文末研究紀錄閱讀，不能視為已獨立核實。\n\n" + result["body"]
        result["body"] += render_packet_appendix(packet)
        result["sources"] = packet["sources"]
        _progress(on_progress, "ready")
        return result
    except Exception as error:
        if error is persistence_error:
            raise
        record_failure(error)
        if saved is not None:
            has_body = saved["metadata"].get("report_has_body", False)
            status = "review_failed" if has_body else "preparation_failed"
            message = ("研究未完成；已保留最後成功保存的正文草稿。" if has_body else
                "研究準備中斷，尚未生成正文；已保留來源與研究紀錄。")
            saved["metadata"].update(metadata(), workflow_stage=status, review_status=status,
                quality_status=status, review_error=message, report_has_body=has_body)
            saved["body"] = "> " + message + "\n\n" + saved["body"]
            return saved
        if isinstance(error, (ResearchError, ModelResponseError, ProviderConfigurationError, ProviderAPIError)):
            raise
        if _transport_timed_out(error):
            raise ResearchError("generation_timeout") from None
        raise ProviderAPIError() from None
    finally:
        if owned:
            try:
                client.close()
            except Exception:
                pass
