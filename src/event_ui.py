"""Paste-and-analyze event briefs, with explicit paid actions and saved history."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import streamlit as st

from .analysis_errors import analysis_error_message
from .collector import save_event
from .event_analysis import MAX_SOURCE_CHARS, analyze_event, brief_to_analysis
from .event_store import EventStore
from .llm_providers import provider_environment_key


_JUDGEMENTS = {"initial": "首次判斷", "maintained": "維持原判斷", "strengthened": "原判斷加強",
               "weakened": "原判斷削弱", "reversed": "推翻原判斷", "uncertain": "暫未能判斷"}
_DIRECTIONS = {"positive": "正面", "negative": "負面", "neutral": "中性", "mixed": "正反因素並存"}
_CONFIDENCE = {"low": "低", "medium": "中", "high": "高"}
_STAGES = {"planning": "整理論點與資料缺口", "searching": "補查資料", "writing": "撰寫短篇研判",
           "reviewing": "核對證據與反方解釋", "saving": "保存研判", "ready": "分析完成"}


def _safe_url(value):
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme in {"https", "http"} and parsed.hostname
                and parsed.username is None and parsed.password is None):
            return value
    except ValueError:
        pass
    return None


def _hk_time(value):
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return "時間未提供時區"
        return stamp.astimezone(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d %H:%M 香港時間")
    except (ValueError, TypeError):
        return "時間未知"


def _statement(value, source_ids):
    if not isinstance(value, dict) or not value.get("text"):
        return
    identifiers = [identifier for identifier in value.get("source_ids", []) if identifier in source_ids]
    suffix = " " + " ".join(f"[{identifier}]" for identifier in identifiers) if identifiers else ""
    st.text(str(value["text"]) + suffix)


def render_event_revision(revision, *, historical=False):
    """Display model text as plain text; only snapshot-owned URLs become links."""
    brief = revision.get("brief") or {}
    metadata = revision.get("metadata") or {}
    sources = revision.get("sources") or []
    source_ids = {source.get("id") for source in sources if isinstance(source.get("id"), str)}
    st.caption(_hk_time(revision.get("created_at")))
    if historical:
        st.caption("歷史判斷快照，並非已證實事實；請對照當時證據及後續更新。")
    st.text(brief.get("headline") or "事件研判")
    status = metadata.get("review_status", "pending")
    if status == "passed":
        st.caption("模型審稿已完成；來源與市場判斷仍需人工核對。")
    elif status == "needs_review":
        st.warning("這份研判仍有待核對項目。")
    elif status in {"failed", "review_failed"}:
        st.warning("審稿未完成，已保留初稿及證據。")
    else:
        st.warning("此版本尚未完成審稿。")
    _statement(brief.get("summary"), source_ids)
    st.caption("判斷變化：" + _JUDGEMENTS.get(brief.get("judgement"), "暫未能判斷")
               + " · 分析自評信心：" + _CONFIDENCE.get(brief.get("confidence"), "未知"))
    _statement(brief.get("judgement_reason"), source_ids)
    st.markdown("**本次新增資訊**")
    _statement(brief.get("new_information"), source_ids)
    if brief.get("facts"):
        st.markdown("**事實與來源**")
        for item in brief["facts"]:
            _statement(item, source_ids)
    if brief.get("impacts"):
        st.markdown("**影響如何傳導**")
        for impact in brief["impacts"]:
            st.text(" · ".join(str(value) for value in (impact.get("ticker") or "未指定標的",
                _DIRECTIONS.get(impact.get("direction"), "待確認"), impact.get("horizon")) if value))
            _statement(impact.get("mechanism"), source_ids)
    for label, field in (("反方因素與成立條件", "counterarguments"), ("下一個驗證點", "watchlist")):
        if brief.get(field):
            st.markdown(f"**{label}**")
            for item in brief[field]:
                _statement(item, source_ids)
    if brief.get("missing_evidence"):
        st.markdown("**尚缺資料**")
        for item in brief["missing_evidence"]:
            st.text(str(item))
    with st.expander("來源與核對紀錄"):
        for source in sources:
            st.text(f"[{source.get('id', '')}] {source.get('title') or '未提供標題'}")
            st.text("材料：" + str(source.get("content_scope") or "未標示")
                    + " · 發布：" + str(source.get("published_at") or "未知"))
            if source.get("content_scope") == "search_summary":
                st.caption("搜尋模型整理的摘要，尚未獨立核對原文。")
            if source.get("content_truncated"):
                st.caption("這次分析只使用了來源的一部分文字。")
            if url := _safe_url(source.get("url")):
                st.link_button("核對來源 " + str(source.get("id") or ""), url)
        for field in ("validation_warnings", "review_findings"):
            for item in metadata.get(field, []):
                st.text(str(item.get("message") or item) if isinstance(item, dict) else str(item))
        st.caption(f"分析請求：{metadata.get('api_call_count', 0)} · 補證請求：{metadata.get('search_calls', 0)}")
        for outcome in metadata.get("search_outcomes", []):
            for warning in outcome.get("warnings", []) if isinstance(outcome, dict) else []:
                st.text(str(warning))
    st.download_button("下載這次研判與證據", json.dumps(revision, ensure_ascii=False, indent=2).encode("utf-8"),
                       file_name="event-analysis.json", mime="application/json",
                       key=f"event_download_{revision.get('id')}", on_click="ignore")


def _event_changed():
    st.session_state.pop("event_topic", None)
    st.session_state.pop("event_result", None)


def render_news_analysis_tab(*, provider, model, api_key, analysis_available, db_path=None):
    """Keep the existing text area and 分析 action as the primary entry point."""
    news_text = st.text_area("貼上新聞內容", height=200, placeholder="輸入中文或英文新聞…", key="event_news_text")
    st.caption("分析會整理新資訊、影響途徑、反方因素及下一個驗證點，並保存證據與判斷歷史。")
    if len(news_text.strip()) > MAX_SOURCE_CHARS:
        st.warning(f"這則新聞超過 {MAX_SOURCE_CHARS:,} 字元；本次會使用前 {MAX_SOURCE_CHARS:,} 字元，請先保留需要分析的關鍵段落。")
    try:
        with EventStore(db_path) as store:
            _render_analysis_controls(news_text, store=store, provider=provider, model=model,
                                      api_key=api_key, analysis_available=analysis_available)
    except Exception as exc:
        st.error(analysis_error_message(exc))


def _render_analysis_controls(news_text, *, store, provider, model, api_key, analysis_available):
    events = store.list_events(limit=50)
    by_id = {event["id"]: event for event in events}
    pending = st.session_state.pop("event_pending_selection", None)
    if pending:
        if pending not in by_id:
            previous = store.get_event(pending)
            if previous:
                by_id[pending] = previous
        if pending in by_id:
            st.session_state["event_selection"] = pending
    if st.session_state.get("event_selection") not in by_id:
        st.session_state["event_selection"] = None
    flash = st.session_state.pop("event_flash", None)
    if flash:
        st.success(flash)
    with st.expander("事件歷史、來源與補證設定"):
        event_id = st.selectbox("事件連結", [None, *by_id],
            format_func=lambda value: "建立新事件" if value is None else by_id[value]["title"],
            key="event_selection", on_change=_event_changed,
            help="同一事件的後續消息選擇已有事件；不同事件請選建立新事件。")
        default_topic = by_id[event_id]["title"] if event_id else ""
        topic = st.text_input("事件名稱／研究問題（選填）", value=default_topic, key="event_topic", max_chars=300,
                              placeholder="留空時以新聞首行作為事件名稱")
        source_url = st.text_input("新聞原文連結（選填）", key="event_source_url",
                                   placeholder="https://…", max_chars=2000).strip()
        save_daily = st.checkbox("同時儲存到日報庫", value=True, key="event_save_daily")
        search_enabled = st.checkbox("按資料缺口搜尋補證", value=False, key="event_search_enabled")
        search_key = ""
        include_x = False
        rounds = 1
        turns = 2
        if search_enabled:
            st.caption("搜尋使用 xAI，另計費；設定限制輪數與輸出長度，不保證固定金額。")
            if provider == "xai":
                search_key = api_key
                st.caption("使用側欄的 xAI API key 搜尋。")
            else:
                entered = st.text_input("補證用 xAI API key", type="password", key="event_search_api_key",
                                        help="只保留於目前工作階段，不會將其他服務商的 key 送往 xAI。").strip()
                search_key = entered or provider_environment_key("xai")
                if not entered and search_key:
                    st.caption("補證使用本機環境設定的 xAI API key。")
            include_x = st.checkbox("同時搜尋 X 貼文", value=False, key="event_include_x")
            rounds = st.selectbox("最多補證輪數", [1, 2], key="event_search_rounds")
            turns = st.selectbox("每輪最多搜尋步數", [1, 2], index=1, key="event_search_turns")
            if not search_key:
                st.info("請設定 xAI API key，或關閉補證以使用目前提供的資料。")
    allowed = analysis_available and (not search_enabled or bool(search_key))
    if event_id:
        st.text("本次延續事件：" + by_id[event_id]["title"])
        st.caption("若貼上的是另一件事，請在上方設定選擇「建立新事件」。")
    if st.button("分析", type="primary", disabled=not allowed, key="event_analyze"):
        if not news_text.strip():
            st.info("請先貼上要分析的新聞。")
        elif source_url and not _safe_url(source_url):
            st.error("原文連結須為不含登入資訊的完整 http 或 https 網址。")
        elif rounds not in (1, 2) or turns not in (1, 2) or not allowed:
            st.error("請確認分析與搜尋設定。")
        else:
            resolved_topic = topic.strip() or news_text.strip().splitlines()[0][:120]
            article = {"id": "manual-" + hashlib.sha256(news_text.strip().encode("utf-8")).hexdigest(),
                       "title": resolved_topic, "content": news_text.strip(), "summary": "",
                       "content_scope": "provided_text", "source_type": "manual", "url": source_url,
                       "source_name": "手動貼上的新聞", "source_kind": "manual"}
            progress = st.empty()
            try:
                progress.info("開始整理事件及相關歷史…")
                revision = analyze_event([article], topic=resolved_topic, event_id=event_id, store=store,
                    provider=provider, model=model, api_key=api_key, search_enabled=search_enabled,
                    search_api_key=search_key, include_x=include_x, max_search_rounds=rounds,
                    max_search_turns=turns,
                    on_progress=lambda stage, *_: progress.info(_STAGES.get(stage, "分析進行中…")))
                message = "已保存事件研判。"
                if revision.get("metadata", {}).get("cached"):
                    message = "已載入相同資料及設定的研判，沒有重複呼叫模型。"
                if save_daily:
                    try:
                        analysis = brief_to_analysis(revision["brief"])
                        data = analysis.model_dump() if hasattr(analysis, "model_dump") else analysis
                        saved = save_event(news_text.strip(), data, source="web")
                        message += " 已儲存到日報庫。" if saved else " 日報庫已有這則新聞。"
                    except Exception:
                        message += " 事件研判已保存，但日報庫儲存失敗。"
                st.session_state["event_pending_selection"] = revision["event_id"]
                # A cached input can refer to an older version of this event.
                # Keep the actual returned snapshot, not merely the event ID.
                st.session_state["event_result"] = revision
                st.session_state["event_flash"] = message
                progress.empty()
                st.rerun()
            except Exception as exc:
                progress.empty()
                st.error(analysis_error_message(exc))
    if event_id:
        revisions = store.list_revisions(event_id, limit=20)
        if revisions:
            st.subheader("分析結果")
            selected = st.session_state.get("event_result")
            if not isinstance(selected, dict) or selected.get("event_id") != event_id:
                selected = revisions[0]
            st.caption("下方是本次載入的已保存研判；修改新聞後，按「分析」才會產生新版本。")
            render_event_revision(selected, historical=selected["id"] != revisions[0]["id"])
            others = [revision for revision in revisions if revision["id"] != selected["id"]]
            if others:
                with st.expander(f"其他已保存判斷（最近 {len(others)} 次）"):
                    for revision in others:
                        render_event_revision(revision, historical=True)
        else:
            st.info("這個事件還沒有已保存的研判。")
    elif not news_text.strip():
        st.caption("也可以在設定中選擇已有事件，查看保存的判斷與來源。")
