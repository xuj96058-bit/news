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
from src.report import generate_daily_report, generate_daily_essay, load_report_events
from src.feeds import load_sources
from src.news_store import NewsStore
from src.rss_pipeline import collect_sources, collect_sec, analyze_pending
from src.sec_universe import FeedFetchError, SecConfigError, refresh_universe
from src.investat_pipeline import collect_investat
from src.research_ui import render_research_tab
from src.research_ui import _generation_progress
from src.research_progress import run_generation
from src.research_store import ResearchStore
from src.workbench_ui import render_reader_tab, render_investat_filters, render_source_operations, render_article
from src.chartbook_ui import render_chartbook_tab
from src.report_gallery_ui import render_report_gallery, show_report

st.set_page_config(page_title="News Impact Analyzer", layout="wide")


def clear_api_key():
    st.session_state["ai_api_key"] = ""


def clear_investat_token():
    st.session_state["investat_token"] = ""
    st.session_state.pop("investat_filter_labels", None)


def clear_investat_filters():
    st.session_state.pop("investat_filter_labels", None)


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

    st.subheader("Investat 設定")
    entered_investat_token = st.text_input(
        "Investat MCP token", type="password", key="investat_token",
        on_change=clear_investat_filters,
        placeholder="貼上 Investat 帳號內產生的 MCP token",
        help="只保留於目前工作階段，僅送往 Investat；不寫入檔案。輸入後按 Enter 套用。",
    ).strip()
    st.button("清除輸入的 Investat token", on_click=clear_investat_token,
              disabled=not entered_investat_token)
    environment_investat_token = os.getenv("INVESTAT_MCP_TOKEN", "").strip()
    effective_investat_token = entered_investat_token or environment_investat_token
    if effective_investat_token:
        st.caption("已設定 Investat token；勾選 Investat 後抓取，以確認可用。")
    else:
        st.caption("Investat 需獨立 MCP token；AI API key 不適用。")
    st.link_button("開啟 Investat 帳號設定", "https://investat.ai/account")

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
st.caption("以新聞為論據，說清楚論點、影響途徑、反證和下一個驗證點")

pending_report_display = st.session_state.pop("pending_report_display", None)
if pending_report_display:
    show_report(pending_report_display)

tab1, tab2, tab3, reader_tab, chartbook_tab, tab4, report_gallery_tab = st.tabs(
    ["新聞查詢", "日報", "新聞來源", "研究閱讀", "圖表庫", "研究報告", "報告展示"],
    key="main_navigation", on_change="rerun")

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
    st.subheader("今日市場論說")
    st.caption("按香港日期選材，合併重複事件，以新聞為論據形成論點、反證及可驗證的觀察。")
    if st.button("生成議論文日報", disabled=not analysis_available, key="daily_essay_generate"):
        bar = st.progress(0.0, text="準備今日市場論說")
        initial_daily = None

        def save_daily_initial(draft):
            global initial_daily
            day = draft["metadata"]["daily_selection"]["hong_kong_date"]
            with ResearchStore() as store:
                initial_daily = store.create_report(topic="今日金融市場的重要變化與影響",
                    title=draft["title"], body=draft["body"], sources=draft.get("sources", []),
                    provider=provider_id, model=selected_model, as_of=day, metadata=draft["metadata"])
            st.session_state["daily_essay"] = draft
            st.session_state["daily_essay_report_id"] = initial_daily["id"]
            return initial_daily["id"]

        try:
            draft = run_generation(lambda **kwargs: generate_daily_essay(api_key=effective_api_key,
                provider=provider_id, model=selected_model, **kwargs),
                lambda stage,size,elapsed: _generation_progress(bar,stage,size,elapsed),
                on_artifact=save_daily_initial, timeout=720)
            if initial_daily is not None:
                with ResearchStore() as store:
                    store.save_review(initial_daily["id"], title=draft["title"], body=draft["body"],
                        expected_version=initial_daily["version"], metadata=draft["metadata"], note="議論文日報審稿")
            st.session_state["daily_essay"] = draft
            bar.progress(1.0, text="日報已生成並保存至報告庫")
        except Exception as exc:
            bar.empty()
            st.error(analysis_error_message(exc))
    daily_report_id = st.session_state.get("daily_essay_report_id")
    if daily_report_id:
        with ResearchStore() as store:
            saved_daily = store.get_report(daily_report_id)
        if saved_daily is None:
            st.session_state.pop("daily_essay", None)
            st.session_state.pop("daily_essay_report_id", None)
        else:
            # Always show the saved version after edits in the report editor.
            st.session_state["daily_essay"] = saved_daily
    daily = st.session_state.get("daily_essay")
    if daily:
        st.markdown(daily["body"])
        st.download_button("下載議論文日報", daily["body"].encode("utf-8"),
                           file_name="daily-essay.md", mime="text/markdown")
    with st.expander("查看今日已分析事件摘要"):
        min_conf = st.slider("最低置信度", 0.5, 1.0, 0.7, 0.05)
        if st.button("生成日報"):
            report = generate_daily_report(min_conf)
            st.markdown(report)
    st.caption(f"目前庫中共 {len(load_report_events())} 則已分析記錄")

with tab3:
    st.subheader("全球財經新聞")
    st.caption("抓取公開 RSS／Atom、金管局公告及勾選的 Investat，先存本機。內容可能僅有摘要或片段；抓取不使用 LLM 額度。")
    available = [source for source in load_sources() if source.enabled]
    labels = {source.id: source.name for source in available}
    public_defaults = list(labels)
    labels["investat"] = "Investat／機構研究"
    selected = st.multiselect("新聞來源", list(labels), default=public_defaults,
                              format_func=lambda identifier: labels[identifier], key="news_sources")
    if "investat" in selected and not effective_investat_token:
        st.info("請在左側填入 Investat MCP token；其餘來源仍可抓取。")
    investat_limit = st.number_input("每次抓取 Investat 文章數", min_value=1, max_value=50,
                                     value=20, step=1, disabled="investat" not in selected)
    investat_filters = render_investat_filters(effective_investat_token, "investat" in selected)
    if st.button("抓取最新新聞", disabled=not selected):
        with st.spinner("抓取新聞中…"), NewsStore() as inbox:
            outcome = collect_sources([s for s in available if s.id in selected], inbox)
            if "investat" in selected:
                investat_outcome = collect_investat(inbox, token=effective_investat_token,
                                                    limit=int(investat_limit), **investat_filters)
                for counter in ("added", "updated", "duplicates"):
                    outcome[counter] += investat_outcome[counter]
                outcome["sources"].extend(investat_outcome["sources"])
            if outcome["sources"] and all(item["status"] == "error" for item in outcome["sources"]):
                st.warning("未取得新聞；請查看下方來源錯誤。")
            else:
                st.success(f"新增 {outcome['added']} 則，更新 {outcome['updated']} 則，重複 {outcome['duplicates']} 則")
            for item in outcome["sources"]:
                if item["status"] in {"error", "partial"}:
                    st.warning(f"{item['name']}：{item.get('error') or '部分文章僅取得摘要或片段；可重新抓取。'}")
                elif item["id"] == "investat":
                    st.caption("Investat 抓取完成。" if item["status"] == "ok" else "Investat 暫無可取得的文章。")
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
        articles = inbox.search_articles(limit=10)["items"]
    render_source_operations(load_sources(), investat_token=effective_investat_token)
    st.caption(f"新聞庫 {counts['total']} 則，已分析 {counts['analyzed']} 則。以下預覽最新 10 則；完整搜尋與分頁請前往「研究閱讀」。")
    for item in articles:
        render_article(item)

with reader_tab:
    render_reader_tab(provider=provider_id, model=selected_model, api_key=effective_api_key,
                      analysis_available=analysis_available)

with chartbook_tab:
    render_chartbook_tab()

with tab4:
    render_research_tab(provider=provider_id, model=selected_model, api_key=effective_api_key,
                        investat_token=effective_investat_token, analysis_available=analysis_available)

with report_gallery_tab:
    render_report_gallery()

