"""
簡單前端：查詢框 + 日報瀏覽
啟動：streamlit run app.py
"""

import streamlit as st
from src import env  # 自動載入本機 .env
from src.analyzer import analyze_news, format_result
from src.collector import save_event, load_events
from src.report import generate_daily_report

st.set_page_config(page_title="News Impact Analyzer", layout="wide")

st.title("金融新聞影響分析")
st.caption("分析師水平：說清楚影響誰、為什麼、幅度、置信度")

tab1, tab2 = st.tabs(["新聞查詢", "日報"])

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
    st.caption(f"目前庫中共 {len(load_events())} 則記錄")
