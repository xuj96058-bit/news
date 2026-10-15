"""One research preparation and audit pipeline, with existing writing adapters."""
from __future__ import annotations

import copy
import hashlib
import json
import time
from threading import Event

from openai import OpenAI

from .analysis_errors import (ModelResponseError, ProviderAPIError, ProviderConfigurationError,
                             provider_failure_reason, provider_status_code)
from .llm_providers import get_provider, validate_provider_model
from .research import ResearchError, _date_label, _progress, _transport_timed_out, _validated_sources
from .research_packet import prepare_research_packet, render_packet_appendix, packet_findings
from .research_timing import publication_reserves, stage_deadline
from .research_audit_workflow import request_research_stage, run_claim_audit, finalize_deep_audit

DEFAULT_BUDGET = {"max_rounds": 3, "max_calls": 16, "timeout_seconds": 3600}
_DEEP_GAP_NOTICE = "> 深度核驗仍有缺口；請結合文末研究紀錄閱讀，不能視為已獨立核實。\n\n"


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
    status = provider_status_code(error)
    if type(status) is int and status in {400, 401, 402, 403, 404, 408, 429, 500, 502, 503, 504}:
        return f"provider_http_{status}"
    if isinstance(error, ProviderConfigurationError):
        return "provider_configuration"
    return provider_failure_reason(error)


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


def render_deep_research_body(report):
    """Render a matching generated snapshot without changing prose or metadata.

    Accept only a validated deep draft whose existing title/body still match
    its generated snapshot. Human edits, restored reports and raw failed drafts
    are rejected, never silently overwritten. This function performs no I/O.
    """
    from .research_packet import _render_packet_appendix

    rejected = "報告與已保存的結構化生成快照不符；保留現有正文，不可自動重新渲染。"
    if not isinstance(report, dict):
        raise ValueError(rejected)
    snapshot = copy.deepcopy(report)
    metadata = snapshot.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError(rejected)
    packet, draft = metadata.get("research_packet"), metadata.get("structured_draft")
    stage = metadata.get("workflow_stage")
    if (metadata.get("deep_research") is not True or metadata.get("report_has_body") is not True
            or metadata.get("manually_verified") is True or metadata.get("restoration")
            or stage not in {"writer_saved", "review_saved", "review_complete", "review_failed"}
            or not isinstance(packet, dict) or not isinstance(draft, dict)
            or not isinstance(snapshot.get("body"), str)):
        raise ValueError(rejected)
    reviewed_version = metadata.get("reviewed_version")
    if ("version" in snapshot and reviewed_version is not None
            and snapshot["version"] != reviewed_version):
        raise ValueError(rejected)
    try:
        packet_hash = hashlib.sha256(json.dumps({key: value for key, value in packet.items()
            if key != "packet_hash"}, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        if packet.get("packet_hash") != packet_hash:
            raise ValueError(rejected)
        date = _date_label(packet["design"]["as_of"])
        if not date or (snapshot.get("as_of") is not None and _date_label(snapshot["as_of"]) != date):
            raise ValueError(rejected)
        sources = snapshot["sources"]
        if sources != packet["sources"]:
            raise ValueError(rejected)
        sources = _validated_sources(sources)
        style = metadata.get("writing_style", "legacy")
        if metadata.get("report_mode") == "argumentative":
            style = "argumentative"
        if style not in {"argumentative", "legacy"}:
            raise ValueError(rejected)
        expected_title = draft["title"]
        if style == "legacy" and metadata.get("report_mode") == "gap":
            expected_title = ("證據缺口簡報｜" + expected_title.removeprefix("證據缺口簡報｜"))[:180]
        if snapshot.get("title") != expected_title:
            raise ValueError(rejected)
        if style == "argumentative":
            from .argumentation import parse_essay
            from .argumentative_workflow import _body
            checked, _ = parse_essay(draft, sources, metadata.get("research_brief"))
        else:
            from .research_workflow import _body, _structured_draft
            checked, _ = _structured_draft(draft, sources, metadata.get("research_brief"))
        if checked != draft:
            raise ValueError(rejected)

        warnings = metadata.get("validation_warnings", [])
        findings = metadata.get("review_findings", [])
        audit = metadata.get("claim_audit", packet.get("claim_audit", {}))
        summary = metadata.get("review_summary", "")
        error = metadata.get("review_error", "")

        def core(current_findings):
            if style == "argumentative":
                return _body(draft, sources, date, plan=metadata["argument_plan"], warnings=warnings,
                    findings=current_findings, summary=error if stage == "review_failed" else summary, stage=stage)
            from .research_workflow import _FIELDS
            # Title audit findings do not belong to a legacy prose section.
            # Keep their message while mapping the display location safely.
            displayed = [row if row.get("section") in _FIELDS else {**row,
                "section": "executive_findings", "paragraph": 1,
                "message": str(row.get("section", "報告")) + "：" + row["message"]}
                for row in current_findings]
            return _body(expected_title, draft, sources, date, stage=stage, warnings=warnings,
                findings=displayed, summary=summary, error=error or None,
                research_brief=metadata.get("research_brief"), report_mode=metadata.get("report_mode", "legacy"))

        # Earlier generators appended audit findings to metadata after rendering
        # the publication body. Rebuild those known variants for exact matching;
        # no arbitrary body prefix/suffix or user-edited section is accepted.
        audit_findings = _audit_findings(audit)
        generated_findings = [*packet_findings(packet), *audit_findings]
        core_variants = {core(findings),
            core([row for row in findings if row not in audit_findings]),
            core([row for row in findings if row not in generated_findings])}
        appendix = render_packet_appendix(packet)
        old_appendix = _render_packet_appendix(packet, historical_calculation_notes=True)
        existing = snapshot["body"]
        if not any(existing == notice + body + suffix
                for notice in ("", _DEEP_GAP_NOTICE)
                for body in core_variants for suffix in ("", appendix, old_appendix)):
            raise ValueError(rejected)
        unresolved = (metadata.get("preparation_warnings") or packet.get("preparation_warnings")
            or packet["answer_status"] != "full" or not audit.get("complete")
            or audit.get("unresolved_count", 0) or warnings
            or any(row.get("severity") in {"warning", "error"} for row in findings))
        return (_DEEP_GAP_NOTICE if unresolved else "") + core(findings) + appendix
    except (KeyError, TypeError, ModelResponseError, ResearchError) as error:
        raise ValueError(rejected) from None


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
    reserves = publication_reserves(limits["timeout_seconds"], style)
    writer_calls = 4 if style == "argumentative" else 3
    preparation_deadline = deadline - sum(reserves.values())
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
        protected_stages = {
            "planning": ("writing", "reviewing", "auditing"),
            "writing": ("reviewing", "auditing"),
            "reviewing": ("auditing",),
            "revising": ("auditing",),
        }
        # Every attempt, including transport retries, must leave the calls for
        # the required later stages. Optional revisions keep the audit slot.
        required_later_calls = {"planning": 3, "writing": 2, "reviewing": 1, "revising": 1}
        if calls >= limits["max_calls"] - required_later_calls.get(stage, 0):
            raise ResearchBudgetError()
        limit = stage_deadline(stage, deadline,
            phase_deadline=preparation_deadline if preparing else None,
            reserve_seconds=sum(reserves.get(name, 0) for name in protected_stages.get(stage, ())))
        if time.monotonic() >= limit:
            raise ResearchBudgetError()
        calls += 1
        usage_stages.append({"stage": stage, "attempt": calls,
            "deadline_seconds": round(limit - started, 2)})
        return limit

    def request(system, payload, stage):
        nonlocal active_stage
        active_stage = stage
        return request_research_stage(system, payload, stage, api_key=key, provider=provider,
            model=model, client=client, request_guard=guard, on_progress=on_progress,
            register_cancel=register_cancel)

    def metadata():
        packet["packet_hash"] = hashlib.sha256(json.dumps({key: value for key, value in packet.items()
            if key != "packet_hash"}, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        warnings = [*packet.get("preparation_warnings", []), *failures]
        return {"deep_research": True, "research_packet": packet, "research_budget": limits,
            "research_execution": {"api_call_count": calls, "stages": list(usage_stages),
                "duration_seconds": round(time.monotonic()-started, 2), "stage_reserves": dict(reserves)},
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

    def audit(draft, *, reserve_calls=0, reserve_seconds=0, blocked_reason=None):
        def audit_request(system, payload, stage):
            if calls >= limits["max_calls"] - reserve_calls or deadline - time.monotonic() <= reserve_seconds:
                raise ResearchBudgetError()
            return request(system, payload, stage)
        return run_claim_audit(draft, style, packet, request=audit_request, blocked_reason=blocked_reason)

    def writer_checkpoint(draft):
        draft["metadata"]["report_has_body"] = True
        persist(draft)
        if draft["metadata"].get("workflow_stage") == "writer_saved":
            # The reviewer gets the complete sources and evidence packet. Do
            # not spend its protected call/time on a redundant pre-review audit.
            packet["claim_audit"] = {"claims": [], "summary": "逐項核驗安排於實質審稿後，尚未完成。",
                "complete": False, "unresolved_count": 1, "independently_verified": False,
                "status": "scheduled_after_review"}

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
            if isinstance(error, ProviderConfigurationError) or provider_status_code(error) in {401, 402, 403, 404}:
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
        # Preserve the latest successful review before another paid call. The
        # final audit can take several minutes or be interrupted independently.
        result["metadata"]["report_has_body"] = True
        persist(result)
        # A stage timeout or a denied optional revision may leave the reserved
        # audit slot usable. The request guard checks actual cancellation and
        # the overall deadline; only fatal provider status blocks it here.
        final_audit = audit(draft, blocked_reason=result["metadata"].get("review_error_reason"))
        result = finalize_deep_audit(saved, packet, final_audit,
            metadata_update=metadata(), allow_unstructured=True)
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


def resume_deep_research_audit(saved_report, *, api_key, provider=None, model=None,
        on_progress=None, register_cancel=None, on_draft=None, client=None,
        request_guard=None, overall_deadline=None, max_calls=1, timeout_seconds=None):
    """Continue only a saved draft's audit through the normal audit pipeline."""
    from .deep_research_resume import resume_audit
    return resume_audit(saved_report, api_key=api_key, provider=provider, model=model,
        on_progress=on_progress, register_cancel=register_cancel, on_draft=on_draft,
        client=client, request_guard=request_guard, overall_deadline=overall_deadline,
        max_calls=max_calls, timeout_seconds=timeout_seconds)
