"""Research reader and collection operations; network work is always explicit."""
from __future__ import annotations

from datetime import date
import json
from urllib.parse import urlsplit

import streamlit as st

from .analysis_errors import analysis_error_message
from .investat import InvestatClient, InvestatError
from .news_store import NewsStore


SCOPE_LABELS = {
    "summary": "摘要", "provided_text": "來源提供的內文（未確認是否全文）",
    "partial_text": "部分內文", "title_only": "僅標題", "filing_notice": "申報通知",
    "fragment": "片段",
    "document_link": "文件連結（未擷取全文）", "announcement_metadata": "公司公告資料（未擷取全文）",
    "notice_only": "公告通知",
}
FILTER_FIELDS = ("query", "institution", "ticker", "asset_class", "topic")


def _safe_link(url):
    try:
        parts = urlsplit(str(url or ""))
        return bool(parts.scheme in {"http", "https"} and parts.hostname
                    and not parts.username and not parts.password)
    except ValueError:
        return False


def render_article(article):
    """Show the publisher separately from the channel which supplied the text."""
    sources = article.get("sources", [])
    provider = article.get("provider") or "、".join(dict.fromkeys(
        str(item.get("source_name") or item.get("source_id") or "") for item in sources))
    publisher = article.get("publisher") or article.get("institution") or "未提供"
    with st.expander(article.get("title") or "未提供標題"):
        st.caption(f"原發布者：{publisher} · 取得渠道：{provider or article.get('source_name') or '未提供'}")
        st.caption(f"{article.get('published_at') or '未提供發布時間'} · "
                   f"{SCOPE_LABELS.get(article.get('content_scope'), article.get('content_scope') or '未標示範圍')}")
        if article.get("author"):
            st.caption("作者：" + str(article["author"]))
        tags = [*(article.get("tickers") or []), *(article.get("asset_classes") or []),
                *(article.get("topics") or [])]
        if tags:
            st.caption(" · ".join(str(tag) for tag in tags))
        st.write(article.get("content") or article.get("summary") or "來源只提供標題，請閱讀原文。")
        if _safe_link(article.get("url")):
            st.link_button("閱讀原文", article["url"])
        elif article.get("source_id") == "investat":
            st.caption(f"Investat 文章 ID：{article.get('external_id', '')}（接口未提供文章連結）")
            st.link_button("前往 Investat", "https://investat.ai/")


def _filter_values(data, key):
    """Accept string or labelled option objects from the MCP filter endpoint."""
    raw = data.get(key, [])
    if isinstance(raw, dict):
        raw = list(raw)
    if not isinstance(raw, list):
        return []
    values = []
    for item in raw:
        value = (item.get("name") or item.get("label") or item.get("value") or item.get("id")) if isinstance(item, dict) else item
        if isinstance(value, (str, int)) and str(value).strip():
            values.append(str(value).strip())
    return list(dict.fromkeys(values))[:300]


def render_investat_filters(token, enabled):
    """Return only explicit filters; never cache a token or make calls on rerender."""
    with st.expander("Investat 收集條件"):
        st.caption("條件傳給 Investat。資產類別、主題、日期可在「研究閱讀」篩選已入庫的文章。")
        if st.button("載入 Investat 可用篩選", key="investat_load_filters", disabled=not enabled or not token):
            try:
                with st.spinner("讀取機構及股票清單…"):
                    filters = InvestatClient(token=token).get_filters()
                # Keep useful labels only, never raw response diagnostics or credentials.
                st.session_state["investat_filter_labels"] = {
                    key: _filter_values(filters, key) for key in ("institutions", "tickers", "topics")}
                st.success("已載入可用篩選。")
            except InvestatError as exc:
                if exc.category in {"authentication", "missing_token", "invalid_token"}:
                    st.warning("Investat 驗證未通過，請檢查帳號權限及 MCP token。")
                else:
                    st.warning("暫時無法載入 Investat 篩選，稍後可以重試。")
            except Exception:
                st.warning("暫時無法載入 Investat 篩選，稍後可以重試。")
        labels = st.session_state.get("investat_filter_labels", {})
        options = {}
        for label, field, hint in (("Investat 關鍵字", "search", ""),
                                   ("Investat 股票代碼", "ticker", "tickers"),
                                   ("Investat 機構", "institution", "institutions")):
            value = st.text_input(label, key="investat_collect_" + field, disabled=not enabled).strip()
            if value:
                options[field] = value
            if hint and labels.get(hint):
                st.caption("可用值：" + "、".join(labels[hint][:30]))
        return options


def _load_search(filters):
    for field in FILTER_FIELDS:
        st.session_state["reader_" + field] = filters.get(field, "")
    st.session_state["reader_source_ids"] = filters.get("source_ids") or []
    for field in ("start_date", "end_date"):
        value = filters.get(field)
        st.session_state["reader_" + field] = date.fromisoformat(value[:10]) if value else None
    st.session_state["reader_include_unknown"] = bool(filters.get("include_unknown", False))
    st.session_state["reader_filters"] = dict(filters)
    st.session_state["reader_page"] = 0


def _move_page(delta):
    st.session_state["reader_page"] = max(0, st.session_state.get("reader_page", 0) + delta)


def _send_to_research(identifiers):
    st.session_state["research_pending_article_ids"] = list(identifiers)
    st.session_state["reader_flash"] = "文章已送到「研究報告」的選文清單。"


def _reader_filters(store):
    facets = store.facets()
    saved = store.list_saved_searches()
    if saved:
        by_id = {row["id"]: row for row in saved}
        row = st.columns([3, 1, 1], vertical_alignment="bottom")
        selected = row[0].selectbox("已保存的關注清單", list(by_id),
                                   format_func=lambda identifier: by_id[identifier]["name"], key="reader_saved_search")
        row[1].button("套用關注清單", key="reader_apply_search", on_click=_load_search,
                      args=(by_id[selected]["filters"],))
        if row[2].button("刪除關注清單", key="reader_delete_search"):
            store.delete_search(selected)
            st.rerun()
    with st.form("reader_filter_form"):
        st.text_input("全文搜尋", key="reader_query", placeholder="例如：AI 資本開支、通脹、銀行流動性")
        cols = st.columns(2)
        cols[0].text_input("機構（支援中英文別名）", key="reader_institution", placeholder="例如：摩根士丹利 / Morgan Stanley")
        cols[1].text_input("股票代碼", key="reader_ticker", placeholder="例如：NVDA、AAPL、0700.HK")
        cols = st.columns(2)
        cols[0].text_input("資產類別", key="reader_asset_class")
        cols[1].text_input("主題", key="reader_topic")
        for key, label in (("institutions", "機構"), ("asset_classes", "資產"), ("topics", "主題")):
            if facets.get(key):
                st.caption(label + "：" + "、".join(facets[key][:15]))
        source_names = {item["id"]: item["name"] for item in facets.get("sources", [])}
        # A saved source remains selectable even before its first successful ingest.
        for source_id in st.session_state.get("reader_source_ids", []):
            source_names.setdefault(source_id, source_id)
        st.multiselect("取得渠道", list(source_names), format_func=source_names.get, key="reader_source_ids")
        cols = st.columns(2)
        cols[0].date_input("發布日期起（可留空）", value=None, key="reader_start_date")
        cols[1].date_input("發布日期止（可留空）", value=None, key="reader_end_date")
        st.caption("發布日期以 UTC 日界篩選；空白表示不限制日期。")
        st.checkbox("日期篩選時也包含發布日不明的文章", key="reader_include_unknown")
        submitted = st.form_submit_button("搜尋新聞庫", key="reader_search", type="primary")
    if submitted:
        filters = {field: st.session_state.get("reader_" + field, "").strip() for field in FILTER_FIELDS}
        filters["source_ids"] = st.session_state.get("reader_source_ids", [])
        for field in ("start_date", "end_date"):
            value = st.session_state.get("reader_" + field)
            filters[field] = value.isoformat() if value else None
        filters["include_unknown"] = st.session_state.get("reader_include_unknown", False)
        if filters["start_date"] and filters["end_date"] and filters["start_date"] > filters["end_date"]:
            st.warning("開始日期不能晚於結束日期；搜尋條件尚未變更。")
        else:
            try:
                store.search_articles(**filters, limit=0)
            except ValueError:
                st.warning("搜尋條件無效，請檢查股票代碼、機構及日期格式；已保留上次有效搜尋。")
            else:
                st.session_state["reader_filters"] = filters
                st.session_state["reader_page"] = 0
    with st.form("reader_save_form"):
        name = st.text_input("關注清單名稱", placeholder="例如：AI 半導體", key="reader_search_name")
        if st.form_submit_button("保存目前搜尋", key="reader_save_search"):
            try:
                store.save_search(name.strip(), st.session_state.get("reader_filters", {}))
                st.success("已保存關注清單；重新開啟也可使用。")
                st.rerun()
            except ValueError as exc:
                st.warning(str(exc))
    return st.session_state.get("reader_filters", {})


def _comparison(store, *, provider, model, api_key, analysis_available):
    from .workbench import build_comparison, load_viewpoints, organize_comparison, save_viewpoints

    identifiers = st.session_state.get("reader_basket", [])
    articles = [item for identifier in identifiers if (item := store.get_article(identifier))]
    st.subheader("公司／主題觀點比較")
    st.caption("跨搜尋與分頁保留選文；最多 12 篇。先顯示來源摘要，按 AI 整理後才提取判斷、證據與成立條件。")
    if not articles:
        st.info("在搜尋結果選擇文章並加入比較。")
        return
    by_id = {article["id"]: article for article in articles}
    if "reader_comparison_ids" in st.session_state:
        st.session_state["reader_comparison_ids"] = [identifier for identifier in
            st.session_state["reader_comparison_ids"] if identifier in by_id]
    chosen = st.multiselect("比較清單", list(by_id),
                           default=None if "reader_comparison_ids" in st.session_state else list(by_id),
                           format_func=lambda identifier: by_id[identifier]["title"], key="reader_comparison_ids")
    if st.button("更新比較清單", key="reader_update_basket"):
        st.session_state["reader_basket"] = chosen
        st.rerun()
    articles = [by_id[identifier] for identifier in chosen]
    st.button("將比較選文加入研究報告", key="reader_to_report", disabled=not articles,
              on_click=_send_to_research, args=([article["id"] for article in articles],))
    if any(len("\n\n".join(str(article.get(field) or "") for field in ("title", "summary", "content"))) > 9000
           for article in articles):
        st.caption("較長文章只取前 9,000 字元供 AI 整理；閱讀器仍保留已取得的全部文字。")
    filters = st.session_state.get("reader_filters", {})
    topic = st.text_input("比較問題", value=filters.get("topic") or filters.get("query") or filters.get("ticker") or "",
                          key="reader_comparison_topic", placeholder="例如：各家對 AI 資本開支回報的判斷有何改變？")
    if st.button("AI 整理並保存觀點", key="reader_organize", disabled=not analysis_available or not articles or not topic.strip()):
        try:
            with st.spinner("依據選文整理觀點及原文證據…"):
                rows = organize_comparison(articles, topic=topic.strip(), provider=provider, model=model, api_key=api_key)
                saved = save_viewpoints(store, rows, topic=topic.strip())
            if saved:
                st.success(f"已保存 {saved} 則觀點；變更選文或問題後，須重新整理。")
            else:
                st.warning("未保存觀點：來源可能已更新，或缺少可核對的原文證據。請重新整理。")
        except Exception as exc:
            st.error(analysis_error_message(exc))
    if not articles:
        return
    rows = build_comparison(articles, topic=topic.strip(), viewpoints=load_viewpoints(store, articles, topic=topic.strip()))
    display = [{"機構": row.get("institution"), "日期": row.get("published_at"), "標題": row.get("title"),
                "核心判斷": row.get("judgement"), "證據": row.get("evidence"), "成立條件": row.get("conditions"),
                "預測期間": row.get("horizon"), "相較前次變化": row.get("change"),
                "整理狀態": row.get("status")} for row in rows]
    if display:
        st.dataframe(display, hide_index=True, alt="各機構核心判斷、證據、成立條件及歷次變化")
        st.download_button("下載比較資料", json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8"),
                           file_name="institution-comparison.json", mime="application/json", key="reader_export_comparison")
        with st.expander("按時間查看觀點及引用"):
            for row in sorted(rows, key=lambda item: item.get("published_at") or ""):
                st.markdown(f"**{row.get('published_at') or '日期未知'} · {row.get('institution') or '機構未知'}**")
                st.write(row.get("judgement") or row.get("title"))
                if row.get("evidence"):
                    st.write(row["evidence"])
                if row.get("previous_evidence"):
                    st.caption("較早文章的比較依據：" + row["previous_evidence"])
                if _safe_link(row.get("source_url")):
                    st.link_button("核對來源：" + str(row.get("title") or "原文"), row["source_url"])


def render_reader_tab(*, provider, model, api_key, analysis_available):
    st.subheader("機構研究閱讀")
    st.caption("搜尋完整本機新聞庫，按機構、公司及主題追蹤。搜尋與保存關注清單不使用 AI 額度。")
    st.session_state.setdefault("reader_filters", {})
    st.session_state.setdefault("reader_page", 0)
    st.session_state.setdefault("reader_basket", [])
    flash = st.session_state.pop("reader_flash", None)
    if flash:
        st.success(flash)
    with NewsStore() as store:
        filters = _reader_filters(store)
        page_size = st.selectbox("每頁文章數", [10, 25, 50], index=1, key="reader_page_size")
        page = st.session_state["reader_page"]
        result = store.search_articles(**filters, limit=page_size, offset=page * page_size)
        total = result["total"]
        pages = max(1, (total + page_size - 1) // page_size)
        if page >= pages:
            st.session_state["reader_page"] = pages - 1
            st.rerun()
        nav = st.columns([1, 3, 1], vertical_alignment="bottom")
        nav[0].button("上一頁", key="reader_previous", disabled=page == 0, on_click=_move_page, args=(-1,))
        nav[1].caption(f"共 {total:,} 則 · 第 {page + 1}／{pages} 頁")
        nav[2].button("下一頁", key="reader_next", disabled=page + 1 >= pages, on_click=_move_page, args=(1,))
        articles = result["items"]
        if not articles:
            st.info("沒有符合條件的文章。可調整搜尋，或先到「新聞來源」收集資料。")
        else:
            by_id = {article["id"]: article for article in articles}
            selection_key = f"reader_page_selection_{page}"
            if selection_key in st.session_state:
                st.session_state[selection_key] = [identifier for identifier in
                    st.session_state[selection_key] if identifier in by_id]
            selected = st.multiselect("選擇本頁文章作比較", list(by_id),
                                     format_func=lambda identifier: by_id[identifier]["title"],
                                     key=selection_key)
            if st.button("加入比較", key="reader_add_comparison", disabled=not selected):
                basket = list(dict.fromkeys([*st.session_state["reader_basket"], *selected]))
                if len(basket) > 12:
                    st.warning("最多比較 12 篇，請先移除部分文章。")
                else:
                    st.session_state["reader_basket"] = basket
                    st.session_state.pop("reader_comparison_ids", None)
                    st.success(f"比較清單已有 {len(basket)} 篇。")
            for article in articles:
                render_article(article)
        _comparison(store, provider=provider, model=model, api_key=api_key, analysis_available=analysis_available)


def render_source_operations(sources, *, investat_token):
    from .source_health import health_summary
    from .scheduler import configure_schedule, list_schedules, run_due

    with st.expander("來源健康狀態與更新排程"):
        st.caption("統計近 30 天的實際收集紀錄。尚未收集不代表故障；摘要／內文比例不代表全文覆蓋率。")
        with NewsStore() as store:
            source_names = {source.id: source.name for source in sources}
            source_names.update({"investat": "Investat／機構研究", "sec": "SEC 公司公告"})
            health = health_summary(store, days=30, source_ids=list(source_names))
            if health:
                st.dataframe([{"來源": source_names.get(row["source_id"], row["source_name"]), "收集次數": row["runs"],
                    "成功率": f"{row['success_rate']:.0%}" if row.get("success_rate") is not None else "—",
                    "最後成功": row.get("last_success") or "尚無", "新增文章": row["added"],
                    "更新": row["updated"], "重複": row["duplicates"],
                    "可用摘要／內文比例": f"{row['usable_text_ratio']:.0%}" if row.get("usable_text_ratio") is not None else "—",
                    "截斷或跳過紀錄": row.get("gap_runs", 0),
                    "最後狀態": "尚無紀錄" if row.get("last_status") == "never_run" else row.get("last_status"),
                    "最後錯誤": row.get("last_error") or ""} for row in health],
                    hide_index=True, alt="近三十天來源收集成功率、文章數、可用文字與缺口")
            else:
                st.info("尚無收集紀錄；完成一次抓取後會顯示來源健康狀態。")
            schedule_rows = list_schedules(store)
            active = st.selectbox("排程來源", list(source_names), format_func=source_names.get, key="schedule_source")
            existing = next((row for row in schedule_rows if row["source_id"] == active), {})
            with st.form("source_schedule_form"):
                interval = st.number_input("更新間隔（分鐘）", min_value=1, max_value=525600,
                    value=int(existing.get("interval_minutes", 60)), step=1, key=f"schedule_interval_{active}")
                enabled = st.checkbox("啟用這個來源的排程", value=bool(existing.get("enabled", True)), key=f"schedule_enabled_{active}")
                options = dict(existing.get("options") or {})
                if active == "investat":
                    for field, label in (("search", "排程關鍵字"), ("ticker", "排程股票代碼"), ("institution", "排程機構")):
                        options[field] = st.text_input(label, value=options.get(field, ""), key="schedule_investat_" + field).strip()
                    options["limit"] = st.number_input("排程每次文章數", min_value=1, max_value=50,
                        value=int(options.get("limit", 20)), key="schedule_investat_limit")
                if st.form_submit_button("保存來源排程", key="schedule_save"):
                    try:
                        configure_schedule(store, active, int(interval), enabled=enabled, options=options)
                        st.success("排程已保存。")
                        schedule_rows = list_schedules(store)
                    except ValueError as exc:
                        st.warning(str(exc))
            if schedule_rows:
                st.dataframe([{"來源": source_names.get(row["source_id"], row["source_id"]),
                    "啟用": row["enabled"], "間隔（分鐘）": row["interval_minutes"],
                    "下次執行（UTC）": row.get("next_run_at"), "最後狀態": row.get("last_status"),
                    "連續失敗": row.get("consecutive_failures", 0)} for row in schedule_rows],
                    hide_index=True, alt="已保存的來源排程及下一次執行時間")
            st.caption("網頁不會自行啟動常駐程序。持續收集請在本機啟動排程 worker；Investat worker 從環境讀取 token，這裡只保存篩選條件。")
            st.code("python -m src.scheduler --worker", language="shell")
            if st.button("執行已到期的排程", key="schedule_run_due"):
                try:
                    with st.spinner("執行到期來源，失敗會保留重試狀態…"):
                        outcomes = run_due(store, sources=sources, investat_token=investat_token)
                    st.info(f"完成 {len(outcomes)} 個到期來源；可重新整理查看健康狀態。")
                    for row in outcomes:
                        if row.get("status") == "error":
                            st.warning(f"{source_names.get(row['source_id'], row['source_id'])}：收集失敗，將按重試規則再嘗試。")
                except Exception:
                    st.warning("排程執行未完成；已保存的排程與文章仍會保留。")
