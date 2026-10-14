"""Research drafts, source preview and user-owned revision editing."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re

import streamlit as st

from .analysis_errors import analysis_error_message
from .feeds import load_sources
from .investat_pipeline import collect_investat
from .news_store import NewsStore
from .research import ResearchError, filter_articles, generate_research_report, prepare_evidence, prepare_sources
from .research_evidence import CATALOG, collect_evidence
from .research_export import ResearchExportError, export_html, export_markdown, export_pdf
from .research_store import ResearchConflictError, ResearchStore, ResearchStoreError
from .rss_pipeline import collect_sources


def _safe_message(error):
    if isinstance(error, (ResearchError, ResearchStoreError, ResearchExportError)):
        return str(error)
    return analysis_error_message(error)


def _origins(article):
    return {article.get("source_id"), *(item.get("source_id") for item in article.get("sources", []))}


def _flash(message):
    st.session_state["research_flash"] = message


def _remember_editor(report_id, title_key, body_key, base_key):
    # Detached state survives Streamlit's cleanup of widgets for another report.
    st.session_state.setdefault("research_editor_drafts", {})[report_id] = {
        "title": st.session_state.get(title_key, ""),
        "body": st.session_state.get(body_key, ""),
        "base": st.session_state[base_key],
    }


def _sources_preview(sources):
    for source in sources:
        with st.expander(f"[{source['id']}] {source['title']}"):
            st.caption(f"來源類型：{source.get('source_type')} · 內容範圍：{source.get('content_scope', '補充資料')} · 發布日：{source.get('publication_date') or source.get('published_at') or '未知'}")
            st.caption(f"資料觀測期：{source.get('observation_date') or '原文未明確提供'} · 擷取日：{source.get('retrieved_at') or source.get('retrieved_date') or '未知'}")
            st.write(source.get("content", ""))
            if source.get("url"):
                st.link_button(f"來源原文（{source['id']}）", source["url"])


def _generate(*, provider, model, api_key, investat_token, analysis_available):
    today = datetime.now(timezone(timedelta(hours=8))).date()
    topic = st.text_input("研究主題", value="行業擁擠度與美股槓桿集中度", key="research_topic")
    left, right = st.columns(2)
    start = left.date_input("新聞起始日期", value=today - timedelta(days=30), key="research_start")
    end = right.date_input("新聞結束日期", value=today, key="research_end")
    keywords = st.text_input("選材關鍵字（逗號分隔，留空顯示全部）", value="擁擠,集中,槓桿,融資,科技,AI", key="research_keywords")
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
        filtered = filter_articles(articles, keywords=keywords, start_date=start, end_date=end)
        if include_unknown:
            filtered += filter_articles([item for item in articles if not item.get("published_at")], keywords=keywords)
    except ResearchError as error:
        st.warning(str(error))
        filtered = []
    usable = [item for item in filtered if item.get("content_scope") not in {"title_only", "filing_notice"}
              and (item.get("content") or item.get("summary"))][:200]
    by_id = {str(item["id"]): item for item in usable}
    if "research_article_ids" in st.session_state:
        previous = st.session_state["research_article_ids"]
        valid = [key for key in previous if key in by_id]
        if valid != previous:
            st.session_state["research_article_ids"] = valid
    chosen_ids = st.multiselect("確認報告選材（最多30篇）", list(by_id),
                               default=None if "research_article_ids" in st.session_state else list(by_id)[:8],
                               format_func=lambda key: f"{by_id[key].get('published_at') or '日期未知'}｜{by_id[key]['title']}",
                               key="research_article_ids", max_selections=30)
    st.caption(f"找到{len(usable)}篇可用候選；僅標題及SEC申報通知不作研究正文。沒有結果時可調整日期或關鍵字。")

    extra = st.checkbox("加入公開核對資料或手動證據", value=False, key="research_use_evidence")
    evidence = []
    if extra:
        st.caption("補充來源與新聞分開標示。自動摘錄方便核對，並不等於數字已經過人工驗證。")
        official_keys = st.multiselect("公開資料", list(CATALOG), default=list(CATALOG),
                                      format_func=lambda key: CATALOG[key][0], key="research_official_keys")
        if st.button("讀取公開核對資料", key="research_fetch_evidence", disabled=not official_keys):
            with st.spinner("讀取選定網頁及PDF摘錄…"):
                outcome = collect_evidence(official_keys)
            st.session_state["research_official_evidence"] = outcome["evidence"]
            for status in outcome["sources"]:
                if status["status"] == "error":
                    st.warning(f"{status['name']}：{status['error']}")
            st.success(f"取得{len(outcome['evidence'])}份補充資料；請在下方確認內容和日期。")
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
                        st.success("已加入本次證據。")
                    else:
                        st.warning("請提供標題與正文。")
            if st.button("清除手動補充證據", key="research_clear_evidence"):
                st.session_state["research_manual_evidence"] = []
        evidence += st.session_state.get("research_manual_evidence", [])
    try:
        sources = prepare_sources([by_id[key] for key in chosen_ids]) if chosen_ids else []
        if evidence:
            sources = prepare_evidence(sources, evidence)
    except ResearchError as error:
        st.warning(str(error))
        sources = []
    with st.expander(f"預覽即將送給AI的資料（{len(sources)}個來源）", expanded=False):
        _sources_preview(sources)
    instructions = st.text_area("寫作要求（可選）", placeholder="例如：區分持倉與資金流；突出相反證據；列出下一步觀察指標。", height=90,
                                key="research_instructions")
    st.caption(f"生成使用左側選定的{provider}／{model}，會調用其API。內容不足時保留資料缺口，不會自動補造數字或圖表。")
    if st.button("生成並保存報告草稿", type="primary", key="research_generate",
                 disabled=not analysis_available or not sources or not topic.strip()):
        try:
            with st.spinner("AI整理所選證據並生成報告…"):
                draft = generate_research_report(topic, sources, api_key=api_key, provider=provider,
                                                 model=model, as_of=today.isoformat(), instructions=instructions)
                metadata = {**draft["metadata"], "source_count": len(sources),
                            "selection": {"start": start.isoformat(), "end": end.isoformat(), "keywords": keywords,
                                          "source_ids": selected_sources},
                            "coverage": {"news": len(chosen_ids), "evidence": len(evidence)}}
                with ResearchStore() as store:
                    saved = store.create_report(topic=topic, title=draft["title"], body=draft["body"], sources=sources,
                                                provider=provider, model=model, as_of=today.isoformat(), metadata=metadata)
            st.session_state["research_active_report"] = saved["id"]
            _flash("草稿已保存；請在「報告庫與編輯」修改和匯出。再次生成會建立另一份報告。")
            st.rerun()
        except Exception as error:
            st.error(_safe_message(error))


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
    for warning in report.get("metadata", {}).get("review_warnings", []):
        st.warning(warning)
    with st.expander("歷史版本與還原"):
        with ResearchStore() as store:
            revisions = store.list_revisions(selected)
        choices = {item["id"]: item for item in revisions}
        revision_id = st.selectbox("歷史版本", list(choices), key=f"research_revision_{selected}",
                                   format_func=lambda key: f"v{choices[key]['version']}｜{choices[key]['created_at']}｜{choices[key]['note']}")
        st.text(choices[revision_id]["body"])
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
        st.json(report["sources"])
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
    st.caption("選材 → 確認證據 → AI草稿 → 你的修改 → 保存版本與匯出")
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
