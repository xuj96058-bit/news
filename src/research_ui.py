"""Research drafts, source preview and user-owned revision editing."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re

import streamlit as st

from .analysis_errors import analysis_error_message
from .feeds import load_sources
from .investat_pipeline import collect_investat
from .news_store import NewsStore
from .research import ResearchError, filter_articles, prepare_evidence, prepare_sources
from .research_workflow import ResearchEvidenceError, generate_research_workflow
from .research_quality import build_research_brief, rank_articles
from .research_analysis import extract_research_observations
from .research_evidence import CATALOG, collect_evidence
from .research_progress import run_generation
from .research_export import ResearchExportError, export_html, export_markdown, export_pdf
from .research_store import ResearchConflictError, ResearchStore, ResearchStoreError
from .rss_pipeline import collect_sources


def _safe_message(error):
    if isinstance(error, (ResearchError, ResearchEvidenceError, ResearchStoreError, ResearchExportError)):
        return str(error)
    return analysis_error_message(error)


def _origins(article):
    return {article.get("source_id"), *(item.get("source_id") for item in article.get("sources", []))}


def _flash(message):
    st.session_state["research_flash"] = message


def _select_articles(identifiers):
    st.session_state["research_article_ids"] = identifiers


def _generation_progress(bar, stage, received_chars=0, elapsed=0):
    steps = {
        "designing": (0.07, "規劃研究問題、證據與分析方法"),
        "supplementing": (0.18, "按證據缺口追溯公開原始資料"),
        "calculating": (0.30, "檢查口徑並執行可重算分析"),
        "auditing": (0.75, "逐項核對主張、證據與計算"),
        "preparing": (0.05, "5/5 核對研究資料與寫作範圍"),
        "retrieving": (0.08, "讀取相關原文與公開補證資料"),
        "organizing": (0.15, "整理可追溯數據"),
        "planning": (0.25, "根據證據規劃論點與反證"),
        "writing": (0.45, f"撰寫議論文（已收到{received_chars:,}個字元）"),
        "revising": (0.78, "按審稿意見修正論證"),
        "requesting": (0.15, "5/5 等待AI初稿"),
        "receiving": (0.35, f"5/5 接收初稿（已收到{received_chars:,}個字元）"),
        "validating": (0.40, "5/5 核對正文與來源清單"),
        "saving_initial": (0.45, "5/5 保存初稿"),
        "reviewing": (0.65, f"5/5 實質審稿：核對證據、比較與結論（已收到{received_chars:,}個字元）"),
        "ready": (0.85, "5/5 準備保存審稿結果"),
        "saving": (0.95, "5/5 保存審稿版本"),
        "saved": (1.0, "5/5 報告已保存"),
    }
    value, label = steps.get(stage, steps["requesting"])
    bar.progress(value, text=f"{label} · 已用{int(elapsed)}秒")


def _remember_editor(report_id, title_key, body_key, base_key):
    # Detached state survives Streamlit's cleanup of widgets for another report.
    st.session_state.setdefault("research_editor_drafts", {})[report_id] = {
        "title": st.session_state.get(title_key, ""),
        "body": st.session_state.get(body_key, ""),
        "base": st.session_state[base_key],
    }


def _sources_preview(sources):
    labels = {"rss": "RSS新聞", "investat": "Investat研究", "official": "公開核對資料", "manual": "手動證據"}
    for source in sources:
        with st.expander(f"[{source['id']}] {source['title']}"):
            st.caption(f"來源類型：{labels.get(source.get('source_type'), '補充資料')} · 內容範圍：{source.get('content_scope', '補充資料')} · 發布日：{source.get('publication_date') or source.get('published_at') or '未知'}")
            st.caption(f"資料觀測期：{source.get('observation_date') or '原文未明確提供'} · 擷取日：{source.get('retrieved_at') or source.get('retrieved_date') or '未知'}")
            st.write(source.get("content", ""))
            if source.get("url"):
                st.link_button(f"來源原文（{source['id']}）", source["url"])


def _source_fingerprint(source):
    """Bind an observation to content rather than a reusable S-number."""
    fields = ("article_id", "title", "publisher", "url", "publication_date", "observation_date", "content")
    payload = {field: source.get(field) for field in fields}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _bound_observations(sources, observations):
    by_id = {source["id"]: source for source in sources}
    accepted, stale = [], 0
    for row in observations:
        source = by_id.get(row.get("source_id")) if isinstance(row, dict) else None
        if source is None or row.get("source_fingerprint") != _source_fingerprint(source):
            stale += 1
            continue
        accepted.append({key: value for key, value in row.items() if key != "source_fingerprint"})
    return accepted, stale


def _merge_evidence(existing, added):
    """A failed refresh must not discard previously fetched documents."""
    records = {}
    for item in [*existing, *added]:
        marker = item.get("title") or item.get("url")
        records[marker] = item
    return list(records.values())


def _prepare_selection(by_id, chosen_ids, evidence):
    sources = prepare_sources([by_id[key] for key in chosen_ids]) if chosen_ids else []
    return prepare_evidence(sources, evidence) if evidence else sources


def _research_brief(topic, sources, as_of):
    rows, stale = _bound_observations(sources, st.session_state.get("research_observations", []))
    brief = build_research_brief(topic, sources, observations=rows, as_of=as_of)
    return brief, stale


def _requirements_checklist(brief):
    labels = {"fulfilled": "已具備", "candidate": "待提取與核對", "missing": "缺少"}
    st.caption(f"證據規格：{brief['profile']['label']}。來源提到主題只算候選；須有可追溯的數值、單位、觀測期與口徑才計為齊備。")
    for item in brief["requirements"]:
        source_ids = "、".join(f"[{value}]" for value in item.get("source_ids", []))
        st.write(f"{labels.get(item['status'], item['status'])}｜{item['label']} {source_ids}")
        st.caption(item.get("reason", ""))


def _fetch_supplements(keys):
    with st.spinner("讀取選定網頁及PDF摘錄…"):
        outcome = collect_evidence(keys)
    st.session_state["research_official_evidence"] = _merge_evidence(
        st.session_state.get("research_official_evidence", []), outcome["evidence"])
    st.session_state["research_evidence_status"] = outcome["sources"]
    st.session_state["research_pending_evidence_keys"] = list(keys)
    _flash(f"取得{len(outcome['evidence'])}份補充資料；已保留先前成功取得的資料，請核對內容及觀測期。")
    st.rerun()


def _remember_observations(sources, rows):
    source_map = {source["id"]: source for source in sources}
    records = {}
    for row in [*st.session_state.get("research_observations", []), *[
            {**row, "source_fingerprint": _source_fingerprint(source_map[row["source_id"]])}
            for row in rows if row.get("source_id") in source_map]]:
        marker = tuple(row.get(key) for key in ("source_fingerprint", "source_id", "requirement_id",
                       "metric", "unit", "period", "scope", "group"))
        records[marker] = row
    st.session_state["research_observations"] = list(records.values())


def _preparation_context(topic, sources):
    return (topic, tuple((source["id"], _source_fingerprint(source)) for source in sources))


def _warning_message(warning):
    return warning.get("message", "資料仍需核對。") if isinstance(warning, dict) else str(warning)


def _organize_data(*, topic, sources, as_of, brief, provider, model, api_key, analysis_available):
    st.markdown("**4．整理數據與比較**")
    st.caption("AI可先提取原文觀測值；程式再核對連續原文、數值、單位、觀測期與統計口徑，僅比較口徑一致的數據。通過錨點檢查仍需核對原始來源。")
    if st.button("AI整理證據與數據", key="research_organize_data",
                 disabled=not analysis_available or not sources or not topic.strip()):
        bar = st.progress(0.0, text="4/5 準備整理來源中的數據")

        def update(stage, received_chars, elapsed):
            labels = {"preparing": "準備數據提取", "requesting": "等待AI提取觀測值",
                      "receiving": f"接收觀測值（{received_chars:,}個字元）", "validating": "核對原文並計算可比變化"}
            bar.progress(0.8 if stage == "validating" else 0.4,
                         text=f"4/5 {labels.get(stage, '整理數據')} · 已用{int(elapsed)}秒")

        try:
            result = run_generation(
                lambda **kwargs: extract_research_observations(topic, sources, api_key=api_key, provider=provider,
                    model=model, as_of=as_of, **kwargs), update, timeout=180)
            _remember_observations(sources, result["observations"])
            st.session_state["research_preparation"] = {
                "context": _preparation_context(topic, sources), "summary": result.get("preparation", {}),
                "warnings": result.get("warnings", [])}
            _flash("數據整理完成；請核對表格與原文，並補充尚缺的必要證據。")
            st.rerun()
        except Exception as error:
            bar.empty()
            st.error(_safe_message(error))
    preparation = st.session_state.get("research_preparation", {})
    if preparation.get("context") == _preparation_context(topic, sources):
        summary = preparation.get("summary", {})
        st.caption(f"上次AI整理：建議{summary.get('suggested_count', 0)}筆，通過原文錨點檢查{summary.get('accepted_count', 0)}筆；尚未人工驗證。")
        for warning in preparation.get("warnings", []):
            st.warning(_warning_message(warning))
    with st.expander("手動整理或補充一筆觀測值"):
        st.caption("逐字貼上包含指標、數字、單位、範圍和期間的連續原文；指標、單位、範圍及分組名稱均使用原文用詞。觀測期可來自來源的觀測日期欄位，不能用發布日或擷取日代替。")
        source_map = {source["id"]: source for source in sources}
        requirements = {item["id"]: item["label"] for item in brief["requirements"]
                        if item["id"] != "historical_comparison"}
        with st.form("research_add_observation"):
            source_id = st.selectbox("引用來源", list(source_map), key="research_observation_source",
                format_func=lambda key: f"[{key}] {source_map[key]['title']}", disabled=not sources)
            requirement = st.selectbox("對應必要證據", list(requirements), key="research_observation_requirement",
                                      format_func=lambda key: requirements[key])
            metric = st.text_input("指標（原文名稱）", key="research_observation_metric")
            value = st.text_input("數值（十進制，不含單位或千分位）", key="research_observation_value")
            unit = st.text_input("單位（原文用詞）", key="research_observation_unit")
            period = st.text_input("觀測期（YYYY-MM-DD、YYYY-MM或YYYYQn）", key="research_observation_period")
            scope = st.text_input("統計範圍／分母／樣本（原文用詞）", key="research_observation_scope")
            group = st.text_input("比較分組（可選，使用原文用詞）", key="research_observation_group")
            excerpt = st.text_area("逐字原文摘錄", height=110, key="research_observation_excerpt")
            if st.form_submit_button("核對並加入觀測值", disabled=not sources):
                row = {"source_id": source_id, "requirement_id": requirement, "metric": metric, "value": value,
                       "unit": unit, "period": period, "scope": scope, "evidence_text": excerpt}
                if group.strip():
                    row["group"] = group
                checked = build_research_brief(topic, sources, observations=[row], as_of=as_of)
                if checked["observations"]:
                    _remember_observations(sources, checked["observations"])
                    _flash("已加入通過原文錨點檢查的觀測值；來源與統計口徑仍需人工核對。")
                    st.rerun()
                else:
                    for warning in checked["warnings"]:
                        st.warning(_warning_message(warning))
                    if not checked["warnings"]:
                        st.warning("觀測值未通過檢查，請補齊數值、單位、期間、口徑及逐字原文。")
    if st.button("清除已整理觀測值", key="research_clear_observations",
                 disabled=not st.session_state.get("research_observations")):
        st.session_state["research_observations"] = []
        st.session_state.pop("research_preparation", None)
        st.rerun()
    brief, stale = _research_brief(topic, sources, as_of)
    if stale:
        st.warning(f"{stale}筆觀測值所引用的來源已改變或未選入，已排除，請按本次來源重新整理；來源編號不能沿用為新證據。")
    for warning in brief.get("warnings", []):
        st.warning(_warning_message(warning))
    if brief["observations"]:
        st.caption("通過原文錨點檢查的數據（可在來源預覽逐字核對）")
        st.dataframe([{ "引用": f"[{row['source_id']}]", "證據項目": row["requirement_id"],
                        "指標": row["metric"], "數值": row["value"], "單位": row["unit"],
                        "觀測期": row["period"], "統計範圍": row["scope"], "分組": row.get("group", ""),
                        "原文摘錄": row["evidence_text"]} for row in brief["observations"]], hide_index=True)
    else:
        st.info("尚無通過原文錨點檢查的觀測值。可先按「AI整理證據與數據」，再手動補齊缺口。")
    if brief["comparisons"]:
        st.caption("同指標、單位、範圍與分組的期間比較；百分比指標的絕對變化以百分點表示，相對變化另列。算式及引用隨報告保存。")
        st.dataframe([{"指標": row["metric"], "單位": row["unit"], "統計範圍": row["scope"],
                       "分組": row.get("group", ""), "起期": row["from_period"], "起值": row["from_value"],
                       "迄期": row["to_period"], "迄值": row["to_value"], "變化（原單位／百分點）": row["change"],
                       "相對變化（%）": row.get("percent_change"), "算式": row["formula"],
                       "引用": "、".join(f"[{key}]" for key in row["source_ids"])}
                      for row in brief["comparisons"]], hide_index=True)
    else:
        st.caption("目前沒有可比期間；補充同指標、同單位和同統計範圍的另一觀測期後，才會計算變化。")
    return brief


def _generate(*, provider, model, api_key, investat_token, analysis_available):
    today = datetime.now(timezone(timedelta(hours=8))).date()
    pending = st.session_state.pop("research_pending_evidence_keys", None)
    if pending is not None:
        st.session_state["research_use_evidence"] = True
        selected = st.session_state.get("research_official_keys", [])
        st.session_state["research_official_keys"] = list(dict.fromkeys([*selected, *pending]))
    st.markdown("**1．按主題篩選來源**")
    topic = st.text_input("研究主題", value="行業擁擠度與美股槓桿集中度", key="research_topic")
    left, right = st.columns(2)
    start = left.date_input("新聞起始日期", value=today - timedelta(days=30), key="research_start")
    end = right.date_input("新聞結束日期", value=today, key="research_end")
    keywords = st.text_input("補充選材關鍵字（逗號分隔）", value="", key="research_keywords")
    available = [source for source in load_sources() if source.enabled]
    names = {source.id: source.name for source in available}
    names["investat"] = "Investat／機構研究"
    defaults = [source.id for source in available] + (["investat"] if investat_token else [])
    selected_sources = st.multiselect("本次新聞來源", list(names), default=defaults,
                                     format_func=lambda value: names[value], key="research_news_sources")
    if "investat" in selected_sources and not investat_token:
        st.info("Investat需要左側的獨立MCP token；已登入瀏覽器不會自動授權本機應用。")
    limit = st.number_input("本次抓取Investat篇數", min_value=1, max_value=50, value=50, step=1,
                            key="research_investat_limit", disabled="investat" not in selected_sources)
    if st.button("抓取所選新聞來源", key="research_collect", disabled=not selected_sources):
        with st.spinner("抓取RSS與Investat新聞…"), NewsStore() as inbox:
            outcome = collect_sources([source for source in available if source.id in selected_sources], inbox)
            statuses = outcome["sources"]
            if "investat" in selected_sources:
                result = collect_investat(inbox, token=investat_token, limit=int(limit))
                statuses += result["sources"]
            st.session_state["research_collection_status"] = statuses
        st.session_state.pop("research_article_ids", None)
    for status in st.session_state.get("research_collection_status", []):
        if status["status"] in {"error", "partial"}:
            st.warning(f"{status['name']}：{status.get('error') or '部分內容只有摘要或片段。'}")
    st.caption("只使用你配置中的新聞來源，不會自動新增RSS。抓取不調用AI；新聞庫與報告庫均保存在本機data目錄。")
    with NewsStore() as inbox:
        articles = inbox.export_articles()
    articles = [item for item in articles if _origins(item) & set(selected_sources)]
    include_unknown = st.checkbox("包括發布日期未知的新聞", value=False, key="research_unknown_dates")
    try:
        filtered = filter_articles(articles, start_date=start, end_date=end)
        if include_unknown:
            filtered += filter_articles([item for item in articles if not item.get("published_at")])
    except ResearchError as error:
        st.warning(str(error))
        filtered = []
    ranked = rank_articles([item for item in filtered if item.get("content_scope") not in {"title_only", "filing_notice"}
                           and (item.get("content") or item.get("summary"))], topic, keywords=keywords, include_unrelated=True)
    show_all = st.checkbox("顯示日期範圍內所有可用新聞（包括主題不匹配者）", value=False,
                           key="research_show_all_articles")
    suggestions = [item for item in ranked if item["score"] > 0]
    visible = ranked if show_all else suggestions
    usable = [item["article"] for item in visible[:200]]
    suggested_ids = [str(item["article"]["id"]) for item in suggestions[:8]]
    st.caption("候選按研究主題與補充關鍵字排序；推薦分數代表內容相關性，不代表證據已足夠。")
    for recommendation in suggestions[:8]:
        reason = "；".join(recommendation.get("reasons", []))
        st.caption(f"推薦｜{recommendation['article']['title']}｜{reason}")
    by_id = {str(item["id"]): item for item in usable}
    if "research_article_ids" in st.session_state:
        previous = st.session_state["research_article_ids"]
        valid = [key for key in previous if key in by_id]
        if valid != previous:
            st.session_state["research_article_ids"] = valid
    pick, clear = st.columns(2)
    pick.button("選取最多8篇主題候選", key="research_select_articles", disabled=not suggested_ids,
                on_click=_select_articles, args=(suggested_ids,))
    clear.button("清除選材", key="research_clear_articles", disabled=not st.session_state.get("research_article_ids"),
                 on_click=_select_articles, args=([],))
    chosen_ids = st.multiselect("確認報告選材（最多30篇）", list(by_id),
                               default=None if "research_article_ids" in st.session_state else suggested_ids,
                               format_func=lambda key: f"{by_id[key].get('published_at') or '日期未知'}｜{by_id[key]['title']}",
                               key="research_article_ids", max_selections=30)
    st.caption(f"找到{len(usable)}篇可用候選；主題匹配共{len(suggestions)}篇。僅標題及SEC申報通知不作研究正文。沒有結果時可調整日期或關鍵字，或開啟顯示全部。")

    st.markdown("**2．檢查必要證據是否齊全**")
    topic_defaults = build_research_brief(topic, [], as_of=today.isoformat())["recommended_catalog_keys"]
    current_official_keys = st.session_state.get("research_official_keys", topic_defaults)
    evidence = []
    if st.session_state.get("research_use_evidence", False):
        evidence = [item for item in st.session_state.get("research_official_evidence", [])
                    if item.get("title") in {CATALOG[key][0] for key in current_official_keys if key in CATALOG}]
        evidence += st.session_state.get("research_manual_evidence", [])
    try:
        sources = _prepare_selection(by_id, chosen_ids, evidence)
    except ResearchError as error:
        st.warning(str(error))
        sources = []
    brief, _ = _research_brief(topic, sources, today.isoformat())
    _requirements_checklist(brief)

    st.markdown("**3．補充資料**")
    recommended_keys = [key for key in brief["recommended_catalog_keys"] if key in CATALOG]
    if recommended_keys:
        st.caption("依缺口建議：" + "、".join(CATALOG[key][0] for key in recommended_keys))
    if st.button("按缺口讀取建議公開資料", key="research_fetch_recommended", disabled=not recommended_keys):
        _fetch_supplements(recommended_keys)
    extra = st.checkbox("加入公開核對資料或手動證據", value=False, key="research_use_evidence")
    evidence = []
    if extra:
        st.caption("補充來源與新聞分開標示。自動摘錄方便核對，並不等於數字已經過人工驗證。")
        official_keys = st.multiselect("公開資料", list(CATALOG),
                                      default=None if "research_official_keys" in st.session_state else topic_defaults,
                                      format_func=lambda key: CATALOG[key][0], key="research_official_keys")
        if st.button("讀取公開核對資料", key="research_fetch_evidence", disabled=not official_keys):
            _fetch_supplements(official_keys)
        evidence += [item for item in st.session_state.get("research_official_evidence", [])
                     if item.get("title") in {CATALOG[key][0] for key in official_keys}]
        with st.expander("手動加入資料、圖表數字或原文摘錄"):
            with st.form("research_add_evidence"):
                title = st.text_input("證據標題")
                publisher = st.text_input("機構或作者")
                url = st.text_input("原文連結（可留空）")
                published = st.text_input("發布日期（未知可留空）")
                observed = st.text_input("數據觀測日期或期間")
                content = st.text_area("證據正文、數字與單位", height=180)
                if st.form_submit_button("加入補充證據"):
                    if title.strip() and content.strip():
                        item = {"title": title, "publisher": publisher, "url": url,
                                "publication_date": published, "observation_date": observed,
                                "content": content, "source_type": "manual"}
                        st.session_state.setdefault("research_manual_evidence", []).append(item)
                        _flash("已加入本次證據；請在數據整理步驟提取可追溯的觀測值。")
                        st.rerun()
                    else:
                        st.warning("請提供標題與正文。")
            if st.button("清除手動補充證據", key="research_clear_evidence"):
                st.session_state["research_manual_evidence"] = []
                st.rerun()
        evidence += st.session_state.get("research_manual_evidence", [])
    for status in st.session_state.get("research_evidence_status", []):
        if status["status"] == "error":
            st.warning(f"{status['name']}：{status['error']}；先前成功取得的資料已保留。")
    try:
        sources = _prepare_selection(by_id, chosen_ids, evidence)
    except ResearchError as error:
        st.warning(str(error))
        sources = []
    counts = {kind: sum(source.get("source_type") == kind for source in sources)
              for kind in ("rss", "investat", "official", "manual")}
    st.caption(f"本次送給AI：RSS新聞{counts['rss']}篇 · Investat研究{counts['investat']}篇 · 公開核對資料{counts['official']}份 · 手動證據{counts['manual']}份。")
    if sources and not counts["rss"] and not counts["investat"]:
        st.info("本次只使用公開核對資料或手動證據，尚未選入RSS／Investat新聞。若希望結合新聞分析，請先選取上方的報告選材。")
    with st.expander(f"預覽即將送給AI的資料（{len(sources)}個來源）", expanded=False):
        _sources_preview(sources)
    brief = _organize_data(topic=topic, sources=sources, as_of=today.isoformat(), brief=brief,
        provider=provider, model=model, api_key=api_key, analysis_available=analysis_available)
    st.markdown("**5．寫作及實質審稿**")
    report_label = st.radio("報告模式", ["議論文式研究報告（建議）", "完整研究報告（建議）", "資料缺口簡報"], key="research_report_mode",
                            help="議論文模式會補讀相關原文、先規劃論點再撰稿與審查；資料不足會縮小結論。原有模式仍可使用。")
    report_mode = "argumentative" if report_label == "議論文式研究報告（建議）" else ("gap" if report_label == "資料缺口簡報" else "research")
    deep_research = st.checkbox("深度研究：規劃、補證、計算與逐項核驗", value=True, key="research_deep",
        help="按研究問題整理數值及文件證據，最多三輪定向補證。不同寫作形式使用同一研究流程；會增加時間與模型用量。")
    budget = None
    if deep_research:
        with st.expander("研究時間與用量預算"):
            minutes = st.number_input("最長研究時間（分鐘）", min_value=2, max_value=60, value=30, step=1, key="research_minutes")
            max_calls = st.number_input("最多模型調用次數", min_value=6, max_value=24, value=16, step=1, key="research_calls")
            rounds = st.number_input("最多補證輪次", min_value=0, max_value=3, value=3, step=1, key="research_rounds")
            budget = {"timeout_seconds": int(minutes)*60, "max_calls": int(max_calls), "max_rounds": int(rounds)}
        st.caption("先保存研究資料，再保存初稿與核驗結果。沒有新證據時提前停止；證據不足會保留有限結論。補證追溯已知公開連結、官方目錄及可識別公司的SEC申報，並非無限制網頁搜尋。")
    missing = [item["label"] for item in brief["requirements"] if item["id"] in brief["missing_required"]]
    if brief["ready"]:
        st.success("必要證據已具備可追溯觀測值，可進入寫作與實質審稿；結論仍須審查原始資料與適用範圍。")
    elif report_mode == "research" and not deep_research:
        st.warning("完整研究報告尚缺：" + "、".join(missing) + "。請回到補充資料及數據整理，或明確選擇「資料缺口簡報」。")
    elif report_mode == "gap":
        st.info("本次將保存為「資料缺口簡報」；尚缺：" + "、".join(missing) + "。正文會限制結論，並列出補充資料步驟。")
    else:
        st.info("生成時會讀取相關原文及建議公開資料，再決定可支持的論點。缺少直接證據的主張會縮小或列為待驗證假說。")
    instructions = st.text_area("寫作要求（可選）", placeholder="例如：區分持倉與資金流；突出相反證據；列出下一步觀察指標。", height=90,
                                key="research_instructions")
    if deep_research:
        st.caption("將依題目組合財務、宏觀、政策或科技證據要求，先核對資料身份及口徑，再寫作並逐項核驗。模型評分不會消除程式發現的問題。")
    elif report_mode == "argumentative":
        st.caption("議論文模式依序規劃論點、撰稿、實質審稿；需要時整理數據及進行一次修訂，最多5次AI調用。以選材準確、證據充分、判斷有用、讀起來清楚作為審查標準。")
    else:
        st.caption("數據整理通常調用1次AI；寫作並保存初稿，再獨立實質審稿，通常另調用2次AI。全文仍可修改和匯出。")
    st.caption(f"生成使用左側選定的{provider}／{model}，會調用其API。內容不足時保留資料缺口，不會自動補造數字或圖表。")
    if not sources:
        st.info("請先在「確認報告選材」選擇新聞，可點「選取最多8篇主題候選」；也可加入補充證據。預覽至少有1個來源後才能生成。")
    if not analysis_available:
        st.info("請在左側輸入所選AI服務商的API key並按Enter，並確認模型設定有效，才能生成報告。")
    if not topic.strip():
        st.info("請先填寫研究主題，才能生成報告。")
    button_label = "生成並保存議論文報告" if report_mode == "argumentative" else ("生成並保存資料缺口簡報" if report_mode == "gap" else "生成並保存完整研究報告")
    if st.button(button_label, type="primary", key="research_generate",
                 disabled=not analysis_available or not sources or not topic.strip() or (report_mode == "research" and not deep_research and not brief["ready"])):
        bar = st.progress(0.0, text="5/5 準備寫作與實質審稿")
        wait_limit = budget["timeout_seconds"] if deep_research else (720 if report_mode == "argumentative" else 360)
        st.caption(f"本次整體等待上限為{wait_limit}秒。初稿會先保存，審稿失敗仍可繼續編輯。")
        initial = None
        try:
            elapsed = 0

            def update(stage, received_chars, seconds):
                nonlocal elapsed
                elapsed = seconds
                _generation_progress(bar, stage, received_chars, elapsed)

            def metadata_for(draft):
                return {**draft.get("metadata", {}), "source_count": len(sources),
                        "report_mode": draft.get("metadata", {}).get("report_mode", report_mode), "research_brief": draft.get("metadata", {}).get("research_brief", brief),
                        "selection": {"start": start.isoformat(), "end": end.isoformat(), "keywords": keywords,
                                      "source_ids": selected_sources},
                        "coverage": {"news": len(chosen_ids), "evidence": len(evidence)}}

            def save_writer(draft):
                nonlocal initial, sources
                sources = draft.get("sources", sources)
                _generation_progress(bar, "saving_initial", elapsed=elapsed)
                with ResearchStore() as store:
                    if initial is None:
                        initial = store.create_report(topic=topic, title=draft["title"], body=draft["body"], sources=sources,
                            provider=provider, model=model, as_of=today.isoformat(), metadata=metadata_for(draft))
                    else:
                        initial = store.save_review(initial["id"], title=draft["title"], body=draft["body"],
                            expected_version=initial["version"], metadata=metadata_for(draft), sources=sources, note="研究階段保存")
                st.session_state["research_active_report"] = initial["id"]
                st.success("研究資料已保存，正在生成正文。" if draft.get("metadata", {}).get("workflow_stage") == "research_prepared" else "初稿已保存，正在逐項核驗及審稿。")
                return initial["id"]

            draft = run_generation(
                lambda **kwargs: generate_research_workflow(topic, sources, api_key=api_key, provider=provider,
                    model=model, as_of=today.isoformat(), instructions=instructions,
                    research_brief=brief, report_mode=report_mode,
                    deep_research=deep_research, budget=budget,
                    writing_style="argumentative" if report_mode == "argumentative" else "legacy", **kwargs),
                update, on_artifact=save_writer, timeout=wait_limit)
            if initial is None:
                save_writer(draft)
            _generation_progress(bar, "saving", elapsed=elapsed)
            sources = draft.get("sources", sources)
            conflict = False
            with ResearchStore() as store:
                try:
                    saved = store.save_review(initial["id"], title=draft["title"], body=draft["body"],
                        expected_version=initial["version"], metadata=metadata_for(draft), sources=sources, note="AI獨立審稿結果")
                except ResearchConflictError:
                    # Another page may already be editing the saved initial draft.
                    # Keep that text and save the review as a separate suggestion.
                    conflict = True
                    suggestion_metadata = {**metadata_for(draft), "parent_report_id": initial["id"]}
                    if suggestion_metadata.get("review_status") in {"reviewed", "needs_review"}:
                        suggestion_metadata["reviewed_version"] = 1
                    saved = store.create_report(topic=topic, title=draft["title"] + "（審稿建議）", body=draft["body"],
                        sources=sources, provider=provider, model=model, as_of=today.isoformat(),
                        metadata=suggestion_metadata)
            _generation_progress(bar, "saved", elapsed=elapsed)
            st.session_state["research_active_report"] = initial["id"] if conflict else saved["id"]
            if conflict:
                _flash("你的修改已保留；AI審稿結果另存為「審稿建議」，可在報告清單選取比較。")
            elif draft.get("metadata", {}).get("review_status") == "review_failed":
                _flash("初稿已保存；獨立審稿未完成，可直接修改與匯出。")
            else:
                _flash("初稿與審稿版本均已保存；請在「報告庫與編輯」核對、修改和匯出。")
            st.rerun()
        except Exception as error:
            bar.empty()
            st.error(_safe_message(error))
            if initial is not None:
                try:
                    with ResearchStore() as store:
                        store.save_review(initial["id"], title=initial["title"], body=initial["body"],
                            expected_version=initial["version"], metadata={"workflow_stage": "review_failed",
                                "review_status": "review_failed", "review_error": _safe_message(error)},
                            note="審稿未完成；初稿保留")
                except ResearchConflictError:
                    pass
                except ResearchStoreError as save_error:
                    st.error(_safe_message(save_error))
                st.warning("初稿已保存，審稿未完成；可到「報告庫與編輯」修改或匯出。")


def _import_report():
    with st.expander("匯入已有報告（JSON或Markdown）"):
        upload = st.file_uploader("選擇報告檔案", type=["json", "md"], key="research_import_file")
        if st.button("匯入報告", key="research_import", disabled=upload is None):
            try:
                raw = upload.getvalue()
                if len(raw) > 2 * 1024 * 1024:
                    raise ValueError()
                text = raw.decode("utf-8-sig")
                item = json.loads(text) if upload.name.lower().endswith(".json") else {"body": text}
                if not isinstance(item, dict) or not isinstance(item.get("body"), str) or not item["body"].strip():
                    raise ValueError()
                title = item.get("title") or Path(upload.name).stem
                if not isinstance(title, str):
                    raise ValueError()
                as_of = item.get("as_of") or datetime.now(timezone(timedelta(hours=8))).date().isoformat()
                with ResearchStore() as store:
                    saved = store.create_report(topic=str(item.get("topic") or title), title=title, body=item["body"],
                                                sources=item.get("sources", []), provider="import", model="", as_of=as_of,
                                                metadata={"report_scope": "imported"})
                st.session_state["research_active_report"] = saved["id"]
                _flash("已匯入為可編輯報告，原始內容會保留。")
                st.rerun()
            except Exception:
                st.error("無法匯入；請使用有效UTF-8 Markdown，或含title、body及可選sources的JSON報告。")


def _library():
    _import_report()
    with ResearchStore() as store:
        reports = store.list_reports()
    if not reports:
        st.info("尚未保存研究報告；可先生成草稿或匯入已有報告。")
        return
    records = {item["id"]: item for item in reports}
    if st.session_state.get("research_active_report") not in records:
        st.session_state["research_active_report"] = reports[0]["id"]
    selected = st.selectbox("選擇報告", list(records),
                            format_func=lambda key: f"{records[key]['title']}｜v{records[key]['version']}｜{records[key]['as_of']}",
                            key="research_active_report")
    report = records[selected]
    base_key, title_key, body_key = (f"research_editor_{name}_{selected}" for name in ("base", "title", "body"))
    drafts = st.session_state.setdefault("research_editor_drafts", {})
    draft = drafts.setdefault(selected, {"base": report["version"], "title": report["title"], "body": report["body"]})
    if base_key not in st.session_state:
        st.session_state[base_key] = draft["base"]
    if title_key not in st.session_state:
        st.session_state[title_key] = draft["title"]
    if body_key not in st.session_state:
        st.session_state[body_key] = draft["body"]
    if st.button("載入最新保存版本", key="research_reload_editor"):
        st.session_state[base_key] = report["version"]
        st.session_state[title_key] = report["title"]
        st.session_state[body_key] = report["body"]
        _remember_editor(selected, title_key, body_key, base_key)
    if st.session_state[base_key] != report["version"]:
        st.warning("其他頁面已保存新版本。你的編輯仍保留；先複製需要的文字，再載入最新版本合併修改。")
    st.caption(f"已保存版本v{report['version']} · {report['provider']}／{report['model']} · 修改不會調用AI或覆蓋原始草稿。")
    metadata = report.get("metadata", {})
    if metadata.get("deep_research"):
        answer = {"full": "研究設計所需證據已具備可追溯紀錄", "limited": "部分問題可回答", "unanswered": "現有資料不足以回答"}
        st.info("深度研究｜" + answer.get(metadata.get("answer_status"), "核驗尚未完成") + "；不代表已獨立核實。")
        with st.expander("研究計劃、證據定位、計算與逐項核驗"):
            st.json({"research_packet": metadata.get("research_packet"), "claim_audit": metadata.get("claim_audit"),
                     "execution": metadata.get("research_execution"), "budget": metadata.get("research_budget")})
    if metadata.get("report_mode") == "gap":
        st.warning("報告模式：資料缺口簡報。必要證據尚未齊備，不可視為完整研究結論；修改標題或正文不會改變此保存狀態。")
    elif metadata.get("report_mode") == "research":
        st.caption("報告模式：完整研究報告；必要證據通過原文錨點檢查，仍須人工核對來源及結論。")
    saved_brief = metadata.get("research_brief")
    if isinstance(saved_brief, dict) and saved_brief.get("requirements"):
        with st.expander("本次必要證據、數據與比較"):
            _requirements_checklist(saved_brief)
            st.caption(f"保存觀測值{len(saved_brief.get('observations', []))}筆；可比變化{len(saved_brief.get('comparisons', []))}組。")
            st.json({"observations": saved_brief.get("observations", []), "comparisons": saved_brief.get("comparisons", [])})
    status = metadata.get("review_status")
    reviewed_version = metadata.get("reviewed_version")
    if not isinstance(status, str):
        status = None
    if not isinstance(reviewed_version, int) or isinstance(reviewed_version, bool) or reviewed_version < 1:
        reviewed_version = None
    if status:
        if reviewed_version and reviewed_version != report["version"]:
            st.info(f"AI審稿基於v{reviewed_version}；目前v{report['version']}包含後續修改，仍由你核對。")
        else:
            st.info({"pending": "初稿已保存，尚待獨立審稿。", "draft": "初稿已保存，尚待獨立審稿。",
                "reviewed": "已完成AI獨立審稿；尚未人工核實，全文可自由修改。",
                "needs_review": "此稿有待核對項目，全文仍可自由修改和匯出。",
                "review_failed": "獨立審稿未完成；初稿已保留，可自由修改和匯出。"}.get(status, "草稿待核對。"))
        with st.expander("獨立審稿結果與待核對項目", expanded=status in {"needs_review", "review_failed"}):
            if metadata.get("review_summary"):
                st.text(metadata["review_summary"])
            if metadata.get("review_error"):
                st.text(metadata["review_error"])
            for issue in [*metadata.get("validation_warnings", []), *metadata.get("review_findings", [])][:40]:
                st.text(issue.get("message", "") if isinstance(issue, dict) else str(issue))
    if metadata.get("parent_report_id"):
        st.caption("此稿是另存的審稿建議；原報告中的人工修改已保留。")
    if report.get("metadata", {}).get("citation_repaired"):
        st.caption("生成時已經過一次AI引用修正；仍請核對正文與來源是否相符。")
    for warning in report.get("metadata", {}).get("review_warnings", []):
        st.warning(warning)
    with st.expander("歷史版本與還原"):
        with ResearchStore() as store:
            revisions = store.list_revisions(selected)
        choices = {item["id"]: item for item in revisions}
        revision_id = st.selectbox("歷史版本", list(choices), key=f"research_revision_{selected}",
                                   format_func=lambda key: f"v{choices[key]['version']}｜{choices[key]['created_at']}｜{choices[key]['note']}")
        st.text(choices[revision_id]["body"])
        if metadata.get("deep_research"):
            with st.expander("此歷史版本的來源與研究紀錄"):
                with ResearchStore() as store:
                    snapshot = store.get_research_snapshot(selected, choices[revision_id]["version"])
                st.json(snapshot)
        if st.button("還原為新的保存版本", key=f"research_restore_{selected}"):
            try:
                with ResearchStore() as store:
                    restored = store.restore_revision(selected, revision_id, expected_version=report["version"])
                st.session_state[base_key] = restored["version"]
                st.session_state[title_key] = restored["title"]
                st.session_state[body_key] = restored["body"]
                _remember_editor(selected, title_key, body_key, base_key)
                _flash("已新增還原版本；所有舊版本仍保留。")
                st.rerun()
            except ResearchStoreError as error:
                st.error(str(error))
    editor_args = (selected, title_key, body_key, base_key)
    title = st.text_input("報告標題", key=title_key, on_change=_remember_editor, args=editor_args)
    body = st.text_area("報告全文（Markdown，可完全修改）", height=520, key=body_key,
                        on_change=_remember_editor, args=editor_args)
    note = st.text_input("本次修改說明（可留空）", key=f"research_edit_note_{selected}")
    if st.button("保存我的修改", type="primary", key="research_save_edit"):
        try:
            with ResearchStore() as store:
                saved = store.save_revision(selected, title=title, body=body,
                                            expected_version=st.session_state[base_key], note=note)
            st.session_state[base_key] = saved["version"]
            _remember_editor(selected, title_key, body_key, base_key)
            _flash(f"已保存v{saved['version']}；原始AI草稿和歷史版本均保留。")
            st.rerun()
        except ResearchStoreError as error:
            st.error(str(error))
    with st.expander("預覽已保存版本", expanded=True):
        st.markdown(report["body"])
    with st.expander("原始AI草稿與來源快照"):
        st.text(report["original_body"])
        st.json(report.get("original_sources", report["sources"]))
    st.caption("下載的是已保存版本。編輯器中的未保存文字不會自動替換匯出內容。JSON可在另一份news中匯入；data目錄請自行備份。")
    base = f"research-{selected}-v{report['version']}"
    cols = st.columns(4)
    cols[0].download_button("下載Markdown", export_markdown(report), file_name=base + ".md", mime="text/markdown", key="research_download_md")
    cols[1].download_button("下載HTML", export_html(report), file_name=base + ".html", mime="text/html", key="research_download_html")
    cols[2].download_button("下載可匯入JSON", json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"),
                            file_name=base + ".json", mime="application/json", key="research_download_json")
    if st.button("準備PDF下載", key="research_prepare_pdf"):
        try:
            st.session_state["research_pdf"] = {"id": selected, "version": report["version"], "data": export_pdf(report)}
        except ResearchExportError as error:
            st.error(str(error))
    pdf = st.session_state.get("research_pdf", {})
    if pdf.get("id") == selected and pdf.get("version") == report["version"]:
        cols[3].download_button("下載PDF", pdf["data"], file_name=base + ".pdf", mime="application/pdf", key="research_download_pdf")


def render_research_tab(*, provider, model, api_key, investat_token, analysis_available):
    st.subheader("研究報告")
    st.caption("按主題篩選來源 → 檢查必要證據 → 補充資料 → 整理數據與比較 → 寫作及實質審稿")
    message = st.session_state.pop("research_flash", None)
    if message:
        st.success(message)
    generate, library = st.tabs(["生成新報告", "報告庫與編輯"])
    with generate:
        _generate(provider=provider, model=model, api_key=api_key, investat_token=investat_token,
                  analysis_available=analysis_available)
    with library:
        try:
            _library()
        except Exception as error:
            st.error(_safe_message(error))
