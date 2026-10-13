"""
簡單前端：查詢框 + 日報瀏覽
啟動：streamlit run app.py
"""

import streamlit as st
from src import env  # 自動載入本機 .env
from src.analyzer import analyze_news, format_result
from src.collector import save_event, load_events
from src.report import generate_daily_report, load_report_events
from src.feeds import load_sources
from src.news_store import NewsStore
from src.rss_pipeline import collect_sources, collect_sec, analyze_pending
from src.sec_universe import FeedFetchError, SecConfigError, refresh_universe

st.set_page_config(page_title="News Impact Analyzer", layout="wide")

st.title("金融新聞影響分析")
st.caption("分析師水平：說清楚影響誰、為什麼、幅度、置信度")

tab1, tab2, tab3 = st.tabs(["新聞查詢", "日報", "新聞來源"])

with tab1:
    news_input = st.text_area("貼上新聞內容", height=200, placeholder="輸入中文或英文新聞...")
    if st.button("分析", type="primary") and news_input.strip():
        with st.spinner("分析中..."):
            try:
                result = analyze_news(news_input)
                st.markdown("### 分析結果")
                st.text(format_result(result))
                if st.checkbox("儲存到日報庫", value=True):
                    saved = save_event(news_input, result.model_dump(), source="web")
                    if saved:
                        st.success("已儲存")
                    else:
                        st.info("已存在（去重）")
            except Exception as e:
                st.error(f"分析失敗：{e}")

with tab2:
    st.subheader("高置信度日報")
    min_conf = st.slider("最低置信度", 0.5, 1.0, 0.7, 0.05)
    if st.button("生成日報"):
        report = generate_daily_report(min_conf)
        st.markdown(report)
    st.caption(f"目前庫中共 {len(load_report_events())} 則已分析記錄")

with tab3:
    st.subheader("全球財經新聞")
    st.caption("抓取公開 RSS／Atom 和金管局公告，先存本機。內容可能僅有摘要；抓取不使用 LLM 額度。")
    available = [source for source in load_sources() if source.enabled]
    labels = {source.id: source.name for source in available}
    selected = st.multiselect("新聞來源", list(labels), default=list(labels),
                              format_func=lambda identifier: labels[identifier])
    if st.button("抓取最新新聞", disabled=not selected):
        with st.spinner("抓取新聞中…"), NewsStore() as inbox:
            outcome = collect_sources([s for s in available if s.id in selected], inbox)
            st.success(f"新增 {outcome['added']} 則，更新 {outcome['updated']} 則，重複 {outcome['duplicates']} 則")
            for item in outcome["sources"]:
                if item["status"] == "error":
                    st.warning(f"{item['name']}：{item['error']}")
    if st.button("AI 分析待處理新聞（最多 5 則）"):
        with st.spinner("分析中，將使用你設定的 LLM API…"), NewsStore() as inbox:
            outcome = analyze_pending(inbox, limit=5)
            st.info(f"完成 {outcome['analyzed']} 則；失敗 {outcome['failed']} 則。只有標題的項目暫不分析。")
    st.caption("SEC 公告追蹤標普 500＋納斯達克 100，需在本機 .env 設定 SEC_USER_AGENT。申報通知不等於財報全文。")
    if st.button("更新 SEC 追蹤名單"):
        with st.spinner("更新成分股及 SEC 公司識別碼…"):
            try:
                universe = refresh_universe()
                st.success(f"追蹤 {universe['counts']['symbols']} 個股票代碼／{universe['counts']['companies']} 家公司")
            except (SecConfigError, FeedFetchError) as exc:
                st.warning(str(exc))
    if st.button("抓取 SEC 最新公告"):
        with st.spinner("按公司及申報類型篩選 SEC 公告…"), NewsStore() as inbox:
            outcome = collect_sec(inbox)
            st.info(f"新增 {outcome['added']} 則 SEC 申報通知")
            for item in outcome["sources"]:
                if item["status"] in {"error", "partial"}:
                    st.warning(item.get("error") or "；".join(item.get("warnings", [])))
    with NewsStore() as inbox:
        counts = inbox.stats()
        articles = inbox.export_articles()[:50]
    st.caption(f"新聞庫 {counts['total']} 則，已分析 {counts['analyzed']} 則。顯示最新 50 則。")
    for item in articles:
        with st.expander(item["title"]):
            st.write("來源：" + "、".join(dict.fromkeys(s["source_name"] for s in item["sources"])))
            st.caption(f"{item.get('published_at') or '未提供發布時間'} · {item['content_scope']}")
            st.write(item.get("content") or item.get("summary") or "來源只提供標題，請閱讀原文。")
            st.link_button("閱讀原文", item["url"])
