"""A source-linked chart library, with explicit data provenance and report handoff."""
from __future__ import annotations

import io

import pandas as pd
import streamlit as st

from .chartbook import ChartbookStore, chart_evidence, parse_chart_csv
from .news_store import NewsStore


METHOD_LABELS = {"manual": "人工抄錄／整理", "source_data": "來源提供的原始數據", "ocr": "外部 OCR 結果（需核對）"}


def _attach_evidence(chart):
    evidence = chart_evidence(chart)
    rows = st.session_state.get("research_manual_evidence", [])
    st.session_state["research_manual_evidence"] = [
        row for row in rows if row.get("chart_id") != chart["id"]] + [evidence]
    st.session_state["research_use_evidence"] = True
    st.session_state["chartbook_flash"] = "圖表來源與數據已加入本次研究報告的補充證據。"


def _change_page(delta):
    st.session_state["chartbook_page"] = max(0, st.session_state.get("chartbook_page", 0) + delta)


def _create_chart(store):
    with st.expander("加入圖表與來源"):
        mode = st.radio("圖表材料", ["原始數據／CSV", "來源圖表圖片"], horizontal=True, key="chartbook_mode")
        st.caption("保留來源連結、單位、觀測期及取得方式。圖片不會自動當作可驗證的數字。")
        with st.form("chartbook_create"):
            title = st.text_input("圖表標題", key="chartbook_title")
            cols = st.columns(2)
            publisher = cols[0].text_input("圖表發布機構", key="chartbook_publisher")
            publication = cols[1].date_input("圖表發布日（可留空）", value=None, key="chartbook_publication")
            source_url = st.text_input("圖表原始來源連結", key="chartbook_source_url")
            article_id = st.text_input("關聯新聞 ID（可留空）", key="chartbook_article_id",
                                       help="可從比較資料下載取得；填寫後會核對文章及原文摘錄。")
            cols = st.columns(2)
            metric = cols[0].text_input("指標名稱", key="chartbook_metric")
            unit = cols[1].text_input("單位", key="chartbook_unit", placeholder="例如：USD billion、%、百分點")
            image_upload, csv_upload, csv_text, start, end = None, None, "", None, None
            if mode == "原始數據／CSV":
                st.caption("CSV 欄位：date,value,series。日期用 YYYY-MM-DD；series 可省略。觀測期由數據計算。")
                csv_upload = st.file_uploader("上傳 CSV（可選）", type=["csv"], key="chartbook_csv_upload")
                csv_text = st.text_area("或貼上 CSV 數據", key="chartbook_csv", height=150,
                                        placeholder="date,value,series\n2026-08-01,10,收入\n2026-09-01,12,收入")
            else:
                image_upload = st.file_uploader("上傳來源圖表（PNG／JPEG／WebP，最多 8 MiB）",
                                                type=["png", "jpg", "jpeg", "webp"], key="chartbook_image")
                cols = st.columns(2)
                start = cols[0].date_input("數據觀測起日", value=None, key="chartbook_start")
                end = cols[1].date_input("數據觀測迄日", value=None, key="chartbook_end")
            method = st.selectbox("數據取得方式", list(METHOD_LABELS), format_func=METHOD_LABELS.get, key="chartbook_method")
            verified = st.checkbox("我已對照來源人工核對數值、單位及期間", key="chartbook_verified")
            excerpt = st.text_area("來源原文摘錄（可選）", key="chartbook_excerpt", height=90)
            notes = st.text_area("圖表說明", key="chartbook_notes", height=90)
            conclusion = st.text_area("這張圖支持的研究判斷", key="chartbook_conclusion", height=90)
            if st.form_submit_button("保存圖表", key="chartbook_save", type="primary"):
                try:
                    payload = {"title": title, "publisher": publisher, "source_url": source_url,
                        "publication_date": publication.isoformat() if publication else "",
                        "metric": metric, "unit": unit, "method": method,
                        "verification": "human_verified" if verified else "unverified",
                        "source_article_id": article_id.strip(), "source_excerpt": excerpt,
                        "notes": notes, "conclusion": conclusion}
                    if article_id.strip():
                        with NewsStore() as inbox:
                            article = inbox.get_article(article_id.strip())
                        if not article:
                            raise ValueError("找不到關聯新聞 ID，請核對後再保存。")
                        if excerpt.strip() and not any(excerpt.strip() in (article.get(field) or "")
                                                      for field in ("title", "summary", "content")):
                            raise ValueError("摘錄未在關聯新聞中找到，請貼上連續的原文。")
                        payload["source_content_hash"] = article["content_hash"]
                    image_bytes = None
                    if mode == "原始數據／CSV":
                        if csv_upload is not None:
                            if csv_upload.size > 2_000_000:
                                raise ValueError("CSV 不能超過 2 MB。")
                            csv_text = csv_upload.getvalue().decode("utf-8-sig")
                        payload["points"] = parse_chart_csv(csv_text)
                    else:
                        payload["observation_start"] = start.isoformat() if start else ""
                        payload["observation_end"] = end.isoformat() if end else ""
                        image_bytes = image_upload.getvalue() if image_upload is not None else None
                    chart = store.create_chart(payload, image_bytes=image_bytes)
                    st.session_state["chartbook_selected"] = chart["id"]
                    st.session_state["chartbook_flash"] = "圖表已保存，可在下方查看並加入研究證據。"
                    st.session_state["chartbook_page"] = 0
                    st.rerun()
                except UnicodeDecodeError:
                    st.warning("請提供 UTF-8 編碼的 CSV。")
                except ValueError as exc:
                    st.warning(str(exc))


def render_chartbook_tab():
    st.subheader("研究圖表庫")
    st.caption("以原始數據繪圖，或保存來源圖表圖片；每張圖都保留來源、觀測期、單位與核對狀態。")
    st.session_state.setdefault("chartbook_page", 0)
    flash = st.session_state.pop("chartbook_flash", None)
    if flash:
        st.success(flash)
    with ChartbookStore() as store:
        _create_chart(store)
        with st.form("chartbook_search_form"):
            query = st.text_input("搜尋圖表、機構、指標或研究判斷", key="chartbook_query")
            if st.form_submit_button("搜尋圖表", key="chartbook_search"):
                st.session_state["chartbook_active_query"] = query.strip()
                st.session_state["chartbook_page"] = 0
        page = st.session_state["chartbook_page"]
        result = store.list_charts(query=st.session_state.get("chartbook_active_query", ""), limit=25, offset=page * 25)
        pages = max(1, (result["total"] + 24) // 25)
        if page >= pages:
            st.session_state["chartbook_page"] = pages - 1
            st.rerun()
        cols = st.columns([1, 3, 1], vertical_alignment="bottom")
        cols[0].button("圖表上一頁", key="chartbook_previous", disabled=page == 0, on_click=_change_page, args=(-1,))
        cols[1].caption(f"共 {result['total']} 張 · 第 {page + 1}／{pages} 頁")
        cols[2].button("圖表下一頁", key="chartbook_next", disabled=page + 1 >= pages, on_click=_change_page, args=(1,))
        if not result["items"]:
            st.info("尚無符合條件的圖表。可先加入一份數據或來源圖片。")
            return
        by_id = {chart["id"]: chart for chart in result["items"]}
        if st.session_state.get("chartbook_selected") not in by_id:
            st.session_state["chartbook_selected"] = next(iter(by_id))
        identifier = st.selectbox("查看圖表", list(by_id), key="chartbook_selected",
                                  format_func=lambda item: by_id[item]["title"])
        chart = store.get_chart(identifier, include_image=True)
        st.markdown("**" + chart["title"] + "**")
        st.caption(f"{chart['publisher']} · {chart['metric']}（{chart['unit']}） · "
                   f"觀測期：{chart['observation_start']} 至 {chart['observation_end']}")
        status = "使用者已標記人工核對" if chart["verification"] == "human_verified" else "尚未人工核對"
        st.caption(f"{METHOD_LABELS[chart['method']]} · {status} · 發布日：{chart.get('publication_date') or '未知'}")
        if chart.get("points"):
            frame = pd.DataFrame(chart["points"])
            frame["date"] = pd.to_datetime(frame["date"])
            st.line_chart(frame, x="date", y="value", color="series", x_label="觀測日期", y_label=chart["unit"],
                          alt=chart["title"] + "；單位：" + chart["unit"])
            with st.expander("核對圖表數據"):
                st.dataframe(chart["points"], hide_index=True, alt="圖表原始觀測值、日期與序列")
        if chart.get("image_bytes"):
            st.image(io.BytesIO(chart["image_bytes"]), caption=chart["title"], alt=chart["title"])
            if not chart.get("points"):
                st.info("這張圖只保存來源圖片，未辨識或驗證其中數字。")
        for label, field in (("圖表說明", "notes"), ("研究判斷", "conclusion"), ("來源摘錄", "source_excerpt")):
            if chart.get(field):
                st.markdown("**" + label + "**")
                st.write(chart[field])
        st.link_button("核對圖表來源", chart["source_url"])
        st.button("加入本次研究報告證據", key="chartbook_to_report", on_click=_attach_evidence, args=(chart,))
        st.download_button("下載圖表資料與來源", store.export_json(identifier),
                           file_name="chart-" + identifier[:8] + ".json", mime="application/json", key="chartbook_export")
        if st.button("封存這張圖表", key="chartbook_archive"):
            store.archive_chart(identifier)
            st.rerun()
