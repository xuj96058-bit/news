"""Browse saved local reports and delete exact, version-checked selections.

All mutations of editor/navigation state happen in callbacks (or in the
explicit pre-render show_report hook), before widgets in other tabs render.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re
from urllib.parse import urlsplit

import streamlit as st

from .research_export import export_markdown
from .research_store import ResearchConflictError, ResearchStore, ResearchStoreError


def show_report(report_id):
    """Select a saved report before navigation widgets render; never call rerun."""
    st.session_state["report_gallery_selected"] = report_id
    st.session_state["report_gallery_query"] = ""
    st.session_state["report_gallery_active_query"] = ""
    st.session_state["report_gallery_page"] = 0
    st.session_state["main_navigation"] = "報告展示"


def _navigate_editor(report_id=None, *, generate=False):
    st.session_state["main_navigation"] = "研究報告"
    st.session_state["research_report_view"] = "生成新報告" if generate else "報告庫與編輯"
    if report_id is not None:
        st.session_state["research_active_report"] = report_id


def _close_report():
    st.session_state.pop("report_gallery_selected", None)


def _apply_search():
    st.session_state["report_gallery_active_query"] = st.session_state.get("report_gallery_query", "").strip()
    st.session_state["report_gallery_page"] = 0
    st.session_state.pop("report_gallery_selected", None)
    _clear_checks()


def _reset_page():
    st.session_state["report_gallery_page"] = 0
    _clear_checks()


def _move_page(delta):
    st.session_state["report_gallery_page"] = max(0, st.session_state.get("report_gallery_page", 0) + delta)
    _clear_checks()


def _clear_checks():
    for key in list(st.session_state):
        if key.startswith("report_gallery_check_"):
            st.session_state.pop(key, None)


def _select_page(identifiers, selected):
    for identifier in identifiers:
        st.session_state["report_gallery_check_" + identifier] = selected


def _stage_delete(reports):
    if st.session_state.get("report_gallery_delete_plan"):
        st.session_state["report_gallery_flash"] = ("info", "請先確認或取消目前的刪除清單，再選擇其他報告。")
        return
    # Store only the exact visible identity, title and version, never live queries.
    st.session_state["report_gallery_delete_plan"] = [
        {"id": report["id"], "title": report["title"], "version": report["version"]}
        for report in reports]


def _cancel_delete():
    st.session_state.pop("report_gallery_delete_plan", None)


def _clean_deleted_session(identifiers):
    deleted = set(identifiers)
    for identifier in deleted:
        for prefix in ("research_editor_base_", "research_editor_title_", "research_editor_body_",
                       "research_edit_note_", "research_revision_", "research_restore_"):
            st.session_state.pop(prefix + identifier, None)
    drafts = st.session_state.get("research_editor_drafts")
    if isinstance(drafts, dict):
        st.session_state["research_editor_drafts"] = {
            identifier: value for identifier, value in drafts.items() if identifier not in deleted}
    for key in ("research_active_report", "report_gallery_selected", "pending_report_display"):
        if st.session_state.get(key) in deleted:
            st.session_state.pop(key, None)
    pdf = st.session_state.get("research_pdf")
    if isinstance(pdf, dict) and pdf.get("id") in deleted:
        st.session_state.pop("research_pdf", None)
    daily_id = st.session_state.get("daily_essay_report_id")
    if daily_id in deleted or ("daily_essay" in st.session_state and not daily_id):
        st.session_state.pop("daily_essay", None)
        st.session_state.pop("daily_essay_report_id", None)
    _clear_checks()
    st.session_state.pop("report_gallery_delete_plan", None)
    st.session_state["report_gallery_page"] = 0


def _confirm_delete():
    plan = st.session_state.get("report_gallery_delete_plan", [])
    if not plan:
        return
    if not _valid_plan(plan):
        st.session_state.pop("report_gallery_delete_plan", None)
        st.session_state["report_gallery_flash"] = ("warning", "刪除清單無效，沒有刪除任何報告。請重新選取。")
        return
    try:
        with ResearchStore() as store:
            count = store.delete_reports(expected_versions={row["id"]: row["version"] for row in plan})
    except ResearchConflictError:
        st.session_state["report_gallery_flash"] = ("warning", "清單內有報告已更新，本次完全未刪除。請取消後重新選取並核對版本。")
        return
    except ResearchStoreError:
        st.session_state["report_gallery_flash"] = ("warning", "刪除未完成，本次沒有刪除任何報告。部分報告可能已移除，或本機資料庫暫時無法寫入；請取消後重新選取。")
        return
    _clean_deleted_session(row["id"] for row in plan)
    st.session_state["report_gallery_flash"] = ("success", f"已永久刪除 {count} 份本機報告、來源快照與全部歷史版本。")


def _valid_plan(plan):
    if not isinstance(plan, list) or not 1 <= len(plan) <= 100:
        return False
    identifiers = set()
    for row in plan:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip()
                or not isinstance(row.get("title"), str) or not isinstance(row.get("version"), int)
                or isinstance(row["version"], bool) or row["version"] < 1 or row["id"] in identifiers):
            return False
        identifiers.add(row["id"])
    return True


def _report_type(report):
    metadata = report.get("metadata") or {}
    if metadata.get("daily_selection"):
        return "市場日報"
    if metadata.get("report_scope") == "imported" or report.get("provider") == "import":
        return "匯入報告"
    return {"gap": "資料缺口簡報", "research": "研究報告", "argumentative": "議論文研究報告"}.get(
        metadata.get("report_mode"), "研究報告")


def _display_time(value):
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError, OverflowError):
        return "時間未提供"


def _excerpt(body, limit=180):
    lines = [line.strip() for line in str(body or "").splitlines()
             if line.strip() and not re.match(r"^\s*(?:#{1,6}\s|```|[-=_]{3,}\s*$)", line)]
    text = " ".join(lines)
    text = re.sub(r"!?\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[*_`>]+", "", text)
    return text[:limit].rstrip() + ("…" if len(text) > limit else "")


def _valid_link(value):
    try:
        parts = urlsplit(str(value or ""))
        return bool(parts.scheme in {"http", "https"} and parts.hostname and not parts.username and not parts.password)
    except ValueError:
        return False


def _delete_confirmation():
    plan = st.session_state.get("report_gallery_delete_plan", [])
    if not plan:
        return
    if not _valid_plan(plan):
        st.warning("刪除清單無效，請取消後重新選取；沒有刪除任何報告。")
        st.button("取消刪除", key="report_gallery_cancel_delete", on_click=_cancel_delete)
        return
    with st.container(border=True):
        st.subheader("確認永久刪除")
        st.warning("以下報告會從本機報告庫永久刪除，包括來源快照及全部歷史版本，無法在程式內還原。")
        for row in plan:
            st.write(f"{row['title']} · v{row['version']}")
        st.caption("已下載至其他位置的檔案不受影響。新聞庫與圖表庫的原始資料仍會保留。")
        with st.container(horizontal=True):
            st.button(f"永久刪除以上 {len(plan)} 份報告", key="report_gallery_confirm_delete",
                      type="primary", on_click=_confirm_delete)
            st.button("取消刪除", key="report_gallery_cancel_delete", on_click=_cancel_delete)


def _details(store, report):
    identifier = report["id"]
    with st.container(border=True):
        st.subheader(report["title"])
        st.caption(f"{_report_type(report)} · 已保存 v{report['version']} · 資料截至 {report.get('as_of') or '未提供'} · "
                   f"更新 {_display_time(report.get('updated_at'))}（香港時間）")
        metadata = report.get("metadata") or {}
        if metadata.get("report_mode") == "gap":
            st.warning("這是資料缺口簡報，必要證據尚未齊備。")
        reviewed = metadata.get("reviewed_version")
        if reviewed and reviewed != report["version"]:
            st.caption(f"AI 審稿基於 v{reviewed}；目前版本有後續修改，仍須人工核對。")
        elif metadata.get("review_status") in {"pending", "draft", "review_failed", "needs_review"}:
            st.caption("此報告仍有待審稿或核對項目，可前往編輯頁查看。")
        with st.container(horizontal=True):
            st.button("編輯這份報告", key="report_gallery_detail_edit", on_click=_navigate_editor, args=(identifier,))
            st.button("關閉閱讀", key="report_gallery_close", on_click=_close_report)
            st.button("刪除這份報告", key="report_gallery_detail_delete", on_click=_stage_delete, args=([report],))
        # Saved Markdown is display data, never trusted HTML or executable code.
        st.markdown(report["body"])
        with st.expander(f"來源快照（{len(report.get('sources') or [])} 份）"):
            if not report.get("sources"):
                st.caption("這份報告沒有保存獨立來源快照。")
            for index, source in enumerate(report.get("sources") or [], 1):
                st.markdown(f"**[{source.get('id') or index}] {source.get('title') or '未提供標題'}**")
                st.caption(f"{source.get('publisher') or source.get('source_name') or '發布者未提供'} · "
                           f"{source.get('publication_date') or source.get('published_at') or '日期未提供'} · "
                           f"{source.get('content_scope') or source.get('source_type') or '範圍未標示'}")
                st.text(str(source.get("content") or source.get("summary") or "來源沒有保存文字內容。"))
                if _valid_link(source.get("url")):
                    st.link_button(f"核對來源 {index}", source["url"])
        revisions = store.list_revisions(identifier)
        export = {**report, "revisions": revisions}
        stem = f"research-{identifier}-v{report['version']}"
        with st.container(horizontal=True):
            st.download_button("下載 Markdown", export_markdown(report), file_name=stem + ".md",
                               mime="text/markdown", key="report_gallery_download_md")
            st.download_button("下載 JSON", json.dumps(export, ensure_ascii=False, indent=2).encode("utf-8"),
                               file_name=stem + ".json", mime="application/json", key="report_gallery_download_json")
        st.caption("下載目前保存的正文；JSON 同時包含原稿、來源快照與歷史版本。")


def render_report_gallery():
    st.subheader("報告展示")
    st.caption("所有已保存報告會自動出現在這裡；閱讀、搜尋與下載不會呼叫 AI。")
    st.session_state.setdefault("report_gallery_page", 0)
    st.session_state.setdefault("report_gallery_active_query", "")
    flash = st.session_state.pop("report_gallery_flash", None)
    if flash:
        getattr(st, flash[0])(flash[1])
    with st.container(horizontal=True):
        st.button("生成新報告", key="report_gallery_new", on_click=_navigate_editor, kwargs={"generate": True})
        st.button("匯入已有報告", key="report_gallery_import", on_click=_navigate_editor)
    _delete_confirmation()
    try:
        with ResearchStore() as store:
            selected = st.session_state.get("report_gallery_selected")
            if selected:
                report = store.get_report(selected)
                if report is not None:
                    _details(store, report)
                else:
                    st.info("這份報告已不在本機報告庫，可從下方選擇其他報告。")
            with st.form("report_gallery_search_form"):
                st.text_input("搜尋報告標題、主題或正文", key="report_gallery_query", placeholder="例如：AI 資本開支")
                st.form_submit_button("搜尋報告", key="report_gallery_search_submit", on_click=_apply_search)
            page_size = st.selectbox("每頁報告數", [6, 12], index=1, key="report_gallery_page_size", on_change=_reset_page)
            page = st.session_state["report_gallery_page"]
            query = st.session_state["report_gallery_active_query"]
            result = store.search_reports(query=query, limit=page_size, offset=page * page_size)
            pages = max(1, (result["total"] + page_size - 1) // page_size)
            if page >= pages:
                page = pages - 1
                st.session_state["report_gallery_page"] = page
                result = store.search_reports(query=query, limit=page_size, offset=page * page_size)
            nav = st.columns([1, 3, 1], vertical_alignment="bottom")
            nav[0].button("上一頁報告", key="report_gallery_previous", disabled=page == 0, on_click=_move_page, args=(-1,))
            nav[1].caption(f"共 {result['total']} 份報告 · 第 {page + 1}／{pages} 頁 · 按最近更新排序")
            nav[2].button("下一頁報告", key="report_gallery_next", disabled=page + 1 >= pages, on_click=_move_page, args=(1,))
            reports = result["items"]
            if not reports:
                st.info("沒有符合搜尋條件的報告。" if query else "尚未保存報告。可使用上方按鈕生成新報告或匯入已有報告。")
                return
            identifiers = [report["id"] for report in reports]
            with st.container(horizontal=True):
                st.button("選取本頁全部", key="report_gallery_select_all", on_click=_select_page, args=(identifiers, True))
                st.button("清除本頁選取", key="report_gallery_clear_selection", on_click=_select_page, args=(identifiers, False))
            for offset in range(0, len(reports), 3):
                columns = st.columns(3)
                for column, report in zip(columns, reports[offset:offset + 3]):
                    identifier = report["id"]
                    with column.container(border=True, height="stretch"):
                        st.caption(f"{_report_type(report)} · v{report['version']}")
                        st.markdown("**" + report["title"] + "**")
                        st.caption(f"更新 {_display_time(report.get('updated_at'))} · 資料截至 {report.get('as_of') or '未提供'}")
                        st.write(_excerpt(report.get("body")) or "報告正文尚未填寫。")
                        st.space("stretch")
                        st.checkbox("選取此報告", key="report_gallery_check_" + identifier)
                        with st.container(horizontal=True):
                            st.button("閱讀", key="report_gallery_read_" + identifier, on_click=show_report, args=(identifier,))
                            st.button("編輯", key="report_gallery_edit_" + identifier, on_click=_navigate_editor, args=(identifier,))
                            st.button("刪除", key="report_gallery_delete_" + identifier, on_click=_stage_delete, args=([report],))
            chosen = [report for report in reports if st.session_state.get("report_gallery_check_" + report["id"], False)]
            st.button(f"刪除選取報告（{len(chosen)} 份）", key="report_gallery_delete_selected", disabled=not chosen,
                      on_click=_stage_delete, args=(chosen,))
    except ResearchStoreError:
        st.warning("暫時無法讀取本機報告庫，請稍後重試；現有報告沒有被修改。")
