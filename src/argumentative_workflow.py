"""Evidence-led planning, argumentative writing and review with durable drafts.

Existing two-pass reports remain readable. This workflow changes application
instructions and tools, not model weights, and never stores credentials.
"""
from __future__ import annotations

import copy
import json
import re
import time

from openai import OpenAI

from .analysis_errors import ModelResponseError, ProviderAPIError, ProviderConfigurationError
from .llm_providers import claude_completion, get_provider, validate_provider_model
from .research import (MAX_PROMPT_CHARS, MAX_RESPONSE_CHARS, ResearchError,
    _date_label, _markdown_text, _number_values, _nonstream_content, _progress,
    _stream_content, _transport_timed_out, _validated_sources, prepare_evidence)
from .research_quality import build_research_brief, select_relevant_passages
from .research_workflow import (_CapturedStream, _computed_numbers, _decode,
    _safe_model_text, _allowed_urls, _safe_review_error)

ESSAY_DEADLINE_SECONDS = 720
ESSAY_STAGE_SECONDS = 240
QUALITY_DIMENSIONS = ("relevance", "evidence", "reasoning", "readability")


def _finding(message, *, section="thesis", paragraph=1, severity="warning", ids=()):
    return {"section": section, "paragraph": paragraph, "severity": severity,
            "message": message, "source_ids": list(ids)}


def _checked_essay(payload, sources, brief):
    from .argumentation import parse_essay, essay_paragraphs
    draft, warnings = parse_essay(payload, sources, brief)
    urls = _allowed_urls(sources)
    def heading_numbers(text, ids, section):
        evidence = "\n".join(str(source.get(k) or "") for source in sources if source["id"] in ids
            for k in ("content", "title", "published_at", "publication_date", "observation_date"))
        if not _number_values(text).issubset(_number_values(evidence)):
            warnings.append({"code":"untraceable_numbers", "section":section, "paragraph":1,
                "message":"標題中的數字未能追溯至支持來源。"})
    heading_numbers(draft["title"], {s["id"] for s in sources}, "thesis")
    for n, argument in enumerate(draft["arguments"], 1):
        ids = set(argument["claim"]["source_ids"])
        ids.update(identifier for p in argument["evidence"] for identifier in p["source_ids"])
        heading_numbers(argument["heading"], ids, f"arguments.{n}.claim")
    for section, index, paragraph in essay_paragraphs(draft):
        paragraph["text"], unknown = _safe_model_text(paragraph["text"], urls)
        if unknown:
            warnings.append({"code": "unknown_links_removed", "section": section,
                "paragraph": index, "message": "已移除來源以外的連結。"})
        evidence = "\n".join(str(source.get(key) or "") for source in sources
            if source["id"] in paragraph["source_ids"]
            for key in ("content", "title", "published_at", "publication_date", "observation_date"))
        text = re.sub(r"(?i)(?<![A-Za-z0-9])S\s*\d+(?![A-Za-z0-9])", "", paragraph["text"])
        computed, missing = _computed_numbers(brief, paragraph["source_ids"], text, evidence)
        if not _number_values(text).issubset(_number_values(evidence) | computed) or missing:
            warnings.append({"code": "untraceable_numbers", "section": section,
                "paragraph": index, "message": "數字或算式未能追溯至本段全部來源及操作數。"})
    return draft, warnings


def _review(raw, sources, brief, plan):
    from .argumentation import essay_paragraphs, essay_findings
    payload = _decode(raw)
    if not {"draft", "findings", "summary", "quality"} <= set(payload):
        raise ModelResponseError("invalid_schema")
    draft, warnings = _checked_essay(payload["draft"], sources, brief)
    locations = {(section, index) for section, index, _p in essay_paragraphs(draft)}
    locations.add(("thesis", 1))
    allowed = {source["id"] for source in sources}
    findings = payload["findings"]
    if not isinstance(findings, list) or len(findings) > 40:
        raise ModelResponseError("invalid_schema")
    clean = []
    for item in findings:
        if (not isinstance(item, dict) or not {"section", "paragraph", "severity", "message", "source_ids"} <= set(item)
                or not isinstance(item["section"], str)
                or not isinstance(item["paragraph"], int) or isinstance(item["paragraph"], bool)
                or (item["section"], item["paragraph"]) not in locations
                or item["severity"] not in {"info", "warning", "error"}
                or not isinstance(item["message"], str) or not 1 <= len(item["message"]) <= 1200
                or not isinstance(item["source_ids"], list)
                or any(not isinstance(identifier, str) or identifier not in allowed for identifier in item["source_ids"])):
            raise ModelResponseError("invalid_schema")
        message, _ = _safe_model_text(item["message"], _allowed_urls(sources))
        clean.append({**item, "message": message})
    quality = payload["quality"]
    if (not isinstance(quality, dict) or any(not isinstance(quality.get(k), int)
        or isinstance(quality[k], bool) or not 0 <= quality[k] <= 4 for k in QUALITY_DIMENSIONS)
        or quality.get("verdict") not in {"pass", "revise", "insufficient"}):
        raise ModelResponseError("invalid_schema")
    for key in QUALITY_DIMENSIONS:
        if quality[key] < 3:
            clean.append(_finding(f"品質項目 {key} 尚未達標，需補證或修改。"))
    if quality["verdict"] != "pass":
        clean.append(_finding("審稿要求修改或補充證據，尚未達成可交付的論證。"))
    clean.extend(essay_findings(draft, plan))
    summary = payload["summary"]
    if not isinstance(summary, str) or not 1 <= len(summary) <= 2000:
        raise ModelResponseError("invalid_schema")
    summary, _ = _safe_model_text(summary, _allowed_urls(sources))
    return draft, warnings, clean, summary, quality


def _body(draft, sources, as_of, *, plan, warnings=(), findings=(), summary="", stage="writer_saved", raw=""):
    from .argumentation import render_essay, essay_paragraphs
    title = draft["title"] if draft else "待核對議論文草稿"
    lines = [f"# {_markdown_text(title)}", "", f"資料截至：{as_of}（香港日期）", ""]
    if stage == "review_failed":
        lines.extend(["> 審稿未完成；已保留初稿。", ""])
    elif warnings or any(item["severity"] in {"warning", "error"} for item in findings):
        lines.extend(["> 草稿尚有待核對問題。", ""])
    if plan["answer_scope"] != "full":
        lines.extend(["> 本文依現有證據縮小論證範圍；原研究問題尚未完整回答。", ""])
    if draft:
        lines.extend(render_essay(draft))
    else:
        lines.extend(["## 未完成格式檢查的初稿", ""])
        lines.extend("    " + line for line in raw.splitlines())
    # Keep audit details after the useful prose, not ahead of the conclusion.
    if warnings or findings or plan["missing_evidence"]:
        lines.extend(["## 核對與補證", ""])
        messages = list(dict.fromkeys([item["message"] for item in [*warnings, *findings]
            if item.get("severity") != "info"] + list(plan["missing_evidence"])))
        lines.extend("- " + _markdown_text(message) for message in messages)
        lines.append("")
    if summary:
        lines.extend(["## 審稿紀錄", "", summary, ""])
    used = {identifier for _s, _i, paragraph in essay_paragraphs(draft)
            for identifier in paragraph["source_ids"]} if draft else set(plan["source_ids"])
    lines.extend(["## 引用來源", ""])
    for source in sources:
        if source["id"] not in used:
            continue
        label = _markdown_text(source["title"])
        if source.get("url"):
            label = f"[{label}]({source['url']})"
        lines.extend([f"- [{source['id']}] {label} — {_markdown_text(source.get('publisher') or '')}；"
            f"發布：{_markdown_text(source.get('publication_date') or '未知')}；"
            f"材料：{_markdown_text(source.get('content_scope') or '片段')}。"])
    return "\n".join(lines)


def generate_argumentative_report(topic, sources, *, api_key, provider="deepseek", model=None,
        as_of, instructions="", on_progress=None, register_cancel=None, on_draft=None,
        client=None, research_brief=None, retrieve_sources=True):
    from .argumentation import (PLAN_SYSTEM, ESSAY_WRITER_SYSTEM, ESSAY_REVIEWER_SYSTEM,
        parse_plan, essay_findings)
    checked = _validated_sources(sources)
    input_sources = copy.deepcopy(checked)
    if not isinstance(topic, str) or not 1 <= len(topic.strip()) <= 500:
        raise ResearchError("invalid_topic")
    topic = topic.strip()
    date = _date_label(as_of)
    if not date:
        raise ResearchError("invalid_dates")
    if not isinstance(instructions, str) or len(instructions) > 4000:
        raise ResearchError("source_budget")
    if not isinstance(api_key, str) or not api_key.strip() or "\n" in api_key or "\r" in api_key:
        raise ProviderConfigurationError("missing_key")
    selected = get_provider(provider)
    model = validate_provider_model(provider, model or selected.models[0])
    key = api_key.strip()
    deadline = time.monotonic() + ESSAY_DEADLINE_SECONDS
    retrieval = []
    brief = build_research_brief(topic, checked,
        observations=(research_brief or {}).get("observations", []), as_of=date)
    if retrieve_sources:
        from .research_retrieval import enrich_sources
        from .research_evidence import collect_evidence
        _progress(on_progress, "retrieving")
        checked, retrieval = enrich_sources(checked, topic=topic)
        checked = _validated_sources(checked)
        catalog_keys = brief["recommended_catalog_keys"][:4]
        if catalog_keys:
            supplements = collect_evidence(catalog_keys)
            known_urls = {source.get("url") for source in checked}
            added = [item for item in supplements["evidence"] if item.get("url") not in known_urls]
            if added:
                # Reserve room for direct evidence without discarding the
                # original selection retained in input_sources.
                budget = 180000 - min(12000 * len(added), 60000)
                per_source = min(12000, budget // len(checked))
                for source in checked:
                    if len(source["content"]) > per_source:
                        excerpt = select_relevant_passages(source["content"], topic, max_chars=per_source)["content"]
                        source["content"] = excerpt or source["content"][:per_source]
                        source["content_truncated"] = True
                        source["included_character_count"] = len(source["content"])
                checked = prepare_evidence(checked, added)
            retrieval.extend(supplements["sources"])
        brief = build_research_brief(topic, checked,
            observations=(research_brief or {}).get("observations", []), as_of=date)
    owned = False
    calls = 0
    persistence_error = None
    from .research_tables import extract_table_observations
    table_rows = extract_table_observations(checked, as_of=date)
    if table_rows:
        brief = build_research_brief(topic, checked,
            observations=[*brief["observations"], *table_rows], as_of=date)
    preparation_diagnostics = {"table_row_count": len(table_rows)}

    def request(system, packet, stage):
        nonlocal calls
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ResearchError("generation_timeout")
        user = json.dumps(packet, ensure_ascii=False)
        if len(user) + len(system) > MAX_PROMPT_CHARS:
            raise ResearchError("source_budget")
        _progress(on_progress, stage)
        calls += 1
        stage_deadline = min(deadline, time.monotonic() + ESSAY_STAGE_SECONDS)
        if provider == "anthropic":
            value = _nonstream_content(claude_completion(model=model, api_key=key, system=system, user=user))
            if time.monotonic() > stage_deadline:
                raise ResearchError("generation_timeout")
            return value
        params = {"model": model, "messages": [{"role": "system", "content": system},
            {"role": "user", "content": user}], "response_format": {"type": "json_object"},
            "stream": True, "timeout": min(60, remaining)}
        if provider == "openai":
            params["max_completion_tokens"] = 16000
        elif provider == "deepseek":
            params.update(max_tokens=32768 if stage in {"reviewing", "revising"} else 24000,
                extra_body={"thinking": {"type": "enabled"}, "reasoning_effort": "high"})
        else:
            params.update(max_tokens=8000, temperature=0.2)
        captured = _CapturedStream(client.chat.completions.create(**params))
        try:
            return _stream_content(captured, deadline=stage_deadline, register_cancel=register_cancel,
                max_chunks=50000,
                on_progress=lambda _s, n: _progress(on_progress, stage, n))
        except Exception:
            if stage == "writing" and captured.text.strip() and time.monotonic() < stage_deadline:
                return captured.text
            raise

    try:
        if provider != "anthropic":
            if client is None:
                client = OpenAI(api_key=key, base_url=selected.base_url, timeout=60, max_retries=0)
                owned = True
                if register_cancel:
                    register_cancel(client.close)
            elif isinstance(client, OpenAI) and (client.api_key != key
                    or str(client.base_url).rstrip("/") != selected.base_url.rstrip("/")):
                raise ProviderConfigurationError("invalid_model")
        # Quantitative subjects get one bounded extraction before planning.
        if retrieve_sources and brief["profile"]["key"] != "generic" and not brief["ready"]:
            from .research_analysis import extract_research_observations
            _progress(on_progress, "organizing")
            calls += 1
            try:
                extracted = extract_research_observations(topic, checked, api_key=key, provider=provider,
                    model=model, as_of=date, client=client, register_cancel=register_cancel,
                    on_progress=lambda _s,n: _progress(on_progress,"organizing",n))
                preparation_diagnostics.update(extracted.get("preparation", {}), warnings=extracted["warnings"])
                brief = build_research_brief(topic, checked,
                    observations=[*brief["observations"], *extracted["observations"]], as_of=date)
            except ModelResponseError:
                retrieval.append({"status": "error", "name": "數據整理", "error": "數據回覆未通過格式檢查；保留來源供論點規劃。"})
        packet = {"topic": topic, "as_of": date, "additional_instructions": instructions,
            "sources": checked, "research_brief": brief}
        plan = parse_plan(request(PLAN_SYSTEM, packet, "planning"), checked, topic)
        checked = [source for source in checked if source["id"] in plan["source_ids"]]
        if not checked:
            raise ResearchError("empty_sources")
        brief = build_research_brief(topic, checked, observations=brief["observations"], as_of=date)
        if brief["profile"]["key"] != "generic" and not brief["ready"]:
            if plan["answer_scope"] == "full":
                plan["answer_scope"] = "narrowed"
            plan["missing_evidence"] = list(dict.fromkeys(plan["missing_evidence"] + [
                row["label"] + "：" + row["reason"] for row in brief["requirements"]
                if row["status"] != "fulfilled"]))
        packet.update(sources=checked, argument_plan=plan, research_brief=brief)
        raw = request(ESSAY_WRITER_SYSTEM, packet, "writing")
        try:
            draft, warnings = _checked_essay(_decode(raw), checked, brief)
        except ModelResponseError:
            draft = None
            warnings = [{"code": "invalid_schema", "section": "thesis", "paragraph": 1,
                "message": "初稿格式未通過檢查，已保存收到的正文供修改。"}]
        findings = essay_findings(draft, plan) if draft else []
        metadata = {"provider": provider, "model": model, "writing_style": "argumentative",
            "report_mode": "argumentative", "workflow_stage": "writer_saved",
            "review_status": "pending", "quality_status": "pending", "manually_verified": False,
            "argument_plan": plan, "structured_draft": draft, "research_brief": brief,
            "input_sources": input_sources,
            "data_preparation": preparation_diagnostics,
            "retrieval_outcomes": retrieval, "validation_warnings": warnings,
            "review_findings": findings, "api_call_count": calls}
        result = {"title": draft["title"] if draft else topic[:180], "sources": checked,
            "body": _body(draft, checked, date, plan=plan, warnings=warnings, findings=findings, raw=raw),
            "metadata": metadata}
        if time.monotonic() > deadline:
            raise ResearchError("generation_timeout")
        if on_draft:
            try:
                on_draft(copy.deepcopy(result))
            except Exception as error:
                persistence_error = error
                raise
        # Persistence failures stop before any further paid request.
        try:
            reviewer_packet = {**packet, "writer_draft": draft or {"raw_text": raw[:MAX_RESPONSE_CHARS]},
                "validation_warnings": warnings, "program_findings": findings}
            review_raw = request(ESSAY_REVIEWER_SYSTEM, reviewer_packet, "reviewing")
            revised, warnings, findings, summary, quality = _review(review_raw, checked, brief, plan)
            # One bounded rewrite/review pass; do not spin on poor evidence.
            if quality["verdict"] == "revise" and deadline-time.monotonic() > 30:
                reviewer_packet.update(writer_draft=revised, validation_warnings=warnings,
                    program_findings=findings, revision_request="修正仍存在的問題；不可新增來源，證據不足就縮小結論。這是最後一輪。")
                review_raw = request(ESSAY_REVIEWER_SYSTEM, reviewer_packet, "revising")
                revised, warnings, findings, summary, quality = _review(review_raw, checked, brief, plan)
            needs_review = bool(warnings or any(f["severity"] in {"warning","error"} for f in findings))
            if plan["answer_scope"] != "full":
                needs_review = True
                findings.append(_finding("原研究問題尚未完整回答；本稿已依證據縮小論證範圍。"))
            metadata.update(workflow_stage="review_complete", review_status="needs_review" if needs_review else "reviewed",
                quality_status="needs_review" if needs_review else "reviewed_snapshot", quality=quality,
                structured_draft=revised, validation_warnings=warnings, review_findings=findings,
                review_summary=summary, api_call_count=calls)
            result.update(title=revised["title"], body=_body(revised, checked, date, plan=plan,
                warnings=warnings, findings=findings, summary=summary, stage="review_complete"))
        except Exception as error:
            summary = _safe_review_error(error)
            metadata.update(workflow_stage="review_failed", review_status="review_failed",
                quality_status="review_failed", review_error=summary,
                review_error_reason=getattr(error,"reason",type(error).__name__), api_call_count=calls)
            result["body"] = _body(draft, checked, date, plan=plan, warnings=warnings,
                findings=findings, summary=summary, stage="review_failed", raw=raw)
        if time.monotonic() > deadline:
            raise ResearchError("generation_timeout")
        _progress(on_progress, "ready")
        return result
    except (ResearchError, ModelResponseError, ProviderConfigurationError, ProviderAPIError):
        raise
    except Exception as error:
        if error is persistence_error:
            raise
        if _transport_timed_out(error):
            raise ResearchError("generation_timeout") from None
        status = getattr(error, "status_code", None)
        raise ProviderAPIError(status if isinstance(status, int) else None) from None
    finally:
        if owned:
            try:
                client.close()
            except Exception:
                pass
