"""
簡單前端：查詢框 + 日報瀏覽
啟動：streamlit run app.py
"""

import os
import inspect
import streamlit as st
from src import env  # 自動載入本機 .env
from src.analyzer import analyze_news, format_result
from src.analysis_errors import AnalyzerReloadRequired, analysis_error_message
from src.llm_providers import PROVIDERS, provider_environment_key, validate_provider_model
from src.collector import save_event, load_events
from src.report import generate_daily_report, load_report_events
from src.feeds import load_sources
from src.news_store import NewsStore
from src.rss_pipeline import collect_sources, collect_sec, analyze_pending
from src.sec_universe import FeedFetchError, SecConfigError, refresh_universe

st.set_page_config(page_title="News Impact Analyzer", layout="wide")


def clear_api_key():
    st.session_state["ai_api_key"] = ""


def change_provider():
    selected = PROVIDERS[st.session_state["ai_provider"]]
    clear_api_key()
    st.session_state["ai_model"] = selected.models[0]
    st.session_state["ai_custom_model"] = ""


# Discard the previous DeepSeek-only widget; never carry a key to another service.
st.session_state.pop("deepseek_api_key", None)
default_provider = os.getenv("LLM_PROVIDER", "deepseek").strip().lower()
if default_provider not in PROVIDERS:
    default_provider = "deepseek"


with st.sidebar:
    st.subheader("AI 設定")
    provider_id = st.selectbox(
        "AI 服務商", list(PROVIDERS), index=list(PROVIDERS).index(default_provider),
        format_func=lambda identifier: PROVIDERS[identifier].label,
        key="ai_provider", on_change=change_provider,
    )
    provider = PROVIDERS[provider_id]
    model_options = [*provider.models, "自訂模型 ID"]
    if st.session_state.get("ai_model") not in model_options:
        st.session_state["ai_model"] = provider.models[0]
    model_choice = st.selectbox("AI 模型", model_options, key="ai_model")
    selected_model = (st.text_input("自訂模型 ID", key="ai_custom_model",
                                   placeholder="填入此服務商的完整模型 ID").strip()
                      if model_choice == "自訂模型 ID" else model_choice)
    try:
        validate_provider_model(provider_id, selected_model)
        model_available = True
    except ValueError:
        model_available = False
        st.info("請填入所選服務商的有效模型 ID。")

    entered_api_key = st.text_input(
        "API key", type="password", key="ai_api_key",
        placeholder=f"貼上你的 {provider.label} API key",
        help="只保留在目前網頁工作階段，不寫入本機檔案。輸入後按 Enter 套用。",
    ).strip()
    st.button("清除輸入的 key", on_click=clear_api_key, disabled=not entered_api_key)
    st.caption("輸入後按 Enter，再點分析。切換服務商會清除輸入的 key；重新載入頁面後可能需要重新輸入。")

    environment_api_key = provider_environment_key(provider_id)
    if entered_api_key:
        st.success("已套用本頁輸入的 key（尚未驗證）")
    elif environment_api_key:
        st.info(f"使用本機環境設定的 {provider.label} API key")
    else:
        st.info("請先輸入 API key 才能分析；抓取新聞不需要 key。")
    st.caption("模型是否可用取決於你的 API 帳戶權限；選單不會發出付費請求。")

effective_api_key = entered_api_key or environment_api_key
analysis_available = bool(effective_api_key) and model_available


def analyze_for_session(text):
    parameters = inspect.signature(analyze_news).parameters
    if not ({"api_key", "provider"} <= parameters.keys() or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())):
        raise AnalyzerReloadRequired()
    return analyze_news(text, model=selected_model, api_key=effective_api_key,
                        provider=provider_id)


st.title("金融新聞影響分析")
st.caption("分析師水平：說清楚影響誰、為什麼、幅度、置信度")

tab1, tab2, tab3 = st.tabs(["新聞查詢", "日報", "新聞來源"])

with tab1:
    news_input = st.text_area("貼上新聞內容", height=200, placeholder="輸入中文或英文新聞...")
    if st.button("分析", type="primary", disabled=not analysis_available) and news_input.strip():
        with st.spinner("分析中..."):
            try:
                result = analyze_for_session(news_input)
                st.markdown("### 分析結果")
                st.text(format_result(result))
                if st.checkbox("儲存到日報庫", value=True):
                    saved = save_event(news_input, result.model_dump(), source="web")
                    if saved:
                        st.success("已儲存")
                    else:
                        st.info("已存在（去重）")
            except Exception as exc:
                # Provider exceptions can echo request headers or credentials.
                st.error(analysis_error_message(exc))

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
    if st.button("AI 分析待處理新聞（最多 5 則）", disabled=not analysis_available):
        with st.spinner("分析中，將使用你設定的 LLM API…"), NewsStore() as inbox:
            outcome = analyze_pending(inbox, limit=5, analyzer=analyze_for_session)
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
