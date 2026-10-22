"""A compact real-time news reader, without model requests."""

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import re
import threading
from zoneinfo import ZoneInfo

import streamlit as st

from .telegram_source import DEFAULT_DB_PATH, RETENTION, TelegramStore, load_config, run_worker


HONG_KONG = ZoneInfo("Asia/Hong_Kong")
PAGE_SIZE = 20
HEADLINE_LIMIT = 52


def _utc_now():
    return datetime.now(timezone.utc)


def _local_time(value):
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(HONG_KONG).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return "時間未知"


def _available_dates(now):
    """Include both partial local dates at the edges of the retention window."""
    latest = now.astimezone(HONG_KONG).date()
    earliest = (now - RETENTION).astimezone(HONG_KONG).date()
    return [latest - timedelta(days=index) for index in range((latest - earliest).days + 1)]


def _post_link(post):
    """Only generate a Telegram permalink from validated numeric identifiers."""
    try:
        chat_id, message_id = int(post["chat_id"]), int(post["message_id"])
    except (KeyError, TypeError, ValueError):
        return None
    channel_id = -chat_id - 1_000_000_000_000
    if not 1 <= channel_id <= 997_852_516_352 or message_id <= 0:
        return None
    return f"https://t.me/c/{channel_id}/{message_id}"


class ReceiverManager:
    """One receiver per app process; the backend also leases across processes."""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._stop_event = None
        self._identity = None
        self.last_error = None

    def stop(self):
        with self._lock:
            if self._stop_event is not None:
                self._stop_event.set()

    def ensure(self, config, db_path):
        # Never give the token to a Streamlit cache key, widget, or thread name.
        identity = hashlib.sha256(
            f"{config.token}\0{config.chat_id}\0{Path(db_path).resolve()}".encode()
        ).digest()
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                if self._identity == identity and not self._stop_event.is_set():
                    return True
                self._stop_event.set()
                # The old request can finish in the background. Do not overlap it
                # with a new consumer or block the Streamlit script while waiting.
                return False
            self._identity = identity
            self._stop_event = threading.Event()
            self.last_error = None
            self._thread = threading.Thread(
                target=self._run,
                args=(config, db_path, self._stop_event),
                name="news-telegram-receiver",
                daemon=True,
            )
            self._thread.start()
            return True

    def _run(self, config, db_path, stop_event):
        try:
            run_worker(config, db_path=db_path, stop_event=stop_event, poll_seconds=30)
        except Exception:
            # Exception text from an HTTP client can contain the Bot API URL.
            self.last_error = "Telegram 接收器暫時停止，頁面更新後會再嘗試連線。"


@st.cache_resource(max_entries=1, show_spinner=False, on_release=lambda value: value.stop())
def _receiver_manager():
    return ReceiverManager()


def ensure_telegram_receiver(db_path=DEFAULT_DB_PATH):
    """Start collection once when the app loads, including outside the daily tab."""
    manager = _receiver_manager()
    try:
        config = load_config()
    except ValueError:
        manager.stop()
        return "Telegram 設定未完成，請檢查本機的 Bot Token 與頻道 Chat ID。"
    if config is None:
        manager.stop()
        return None
    if not manager.ensure(config, db_path):
        return "正在切換 Telegram 接收設定，稍後會自動更新。"
    return manager.last_error


def _reset_feed():
    st.session_state["telegram_feed_limit"] = PAGE_SIZE
    st.session_state.pop("telegram_feed_anchor", None)
    st.session_state.pop("telegram_feed_selected", None)


def _show_latest():
    # Explicitly accepting new items may move the list; background refresh never
    # prepends them underneath someone who is reading an older article.
    st.session_state.pop("telegram_feed_anchor", None)


def _load_more():
    st.session_state["telegram_feed_limit"] = st.session_state.get("telegram_feed_limit", PAGE_SIZE) + PAGE_SIZE


def _select_post(message_id):
    st.session_state["telegram_feed_selected"] = message_id


def _headline(post):
    content = str(post.get("text") or "").strip()
    first = next((line.strip() for line in content.splitlines() if line.strip()), "沒有文字內容")
    return first[:HEADLINE_LIMIT] + ("…" if len(first) > HEADLINE_LIMIT else "")


def _preview(post):
    lines = [line.strip() for line in str(post.get("text") or "").splitlines() if line.strip()]
    text = " ".join([lines[0][HEADLINE_LIMIT:], *lines[1:]]).strip() if lines else ""
    return text[:150] + ("…" if len(text) > 150 else "")


def _article_body(post):
    content = str(post.get("text") or "").strip()
    if not content:
        return "此則新聞沒有文字內容，請查看原文。"
    first, separator, rest = content.partition("\n")
    # The complete first line already appears in the headline. If it had to be
    # shortened, retain the complete original in the body instead.
    return rest.lstrip() if len(first.strip()) <= HEADLINE_LIMIT else content


def _escape_label(text):
    """Keep untrusted news literal in native widgets' Markdown labels."""
    # Escape punctuation supported by CommonMark, including colon directives and
    # HTML/auto-link delimiters. Full articles are always rendered with st.text.
    return re.sub(r"([\\`*_{}\[\]()<>#+\-.!|~:$])", r"\\\1", str(text))


def _anchor(post):
    return (datetime.fromisoformat(post["published_at"].replace("Z", "+00:00")).timestamp(), post["message_id"])


def _day_label(day, now):
    if day is None:
        return "最近 7 天"
    today = now.astimezone(HONG_KONG).date()
    if day == today:
        return "今天"
    if day == today - timedelta(days=1):
        return "昨天"
    return day.strftime("%m月%d日")


_FEED_STYLE = """<style>
.st-key-realtime_news_list {padding-right: 1.1rem; border-right: 1px solid color-mix(in srgb, currentColor 14%, transparent);}
.st-key-realtime_news_list [class*="st-key-news_row_"] {padding: .8rem .8rem .85rem; border-bottom: 1px solid color-mix(in srgb, currentColor 10%, transparent); gap:.2rem;}
.st-key-realtime_news_list [class*="st-key-news_row_selected_"] {background:color-mix(in srgb, #c43b50 7%, transparent); border-left:2px solid #c43b50; padding-left:calc(.8rem - 2px);}
.st-key-realtime_news_list [class*="st-key-telegram_open_"] button {padding:0; min-height:0; border:0; border-radius:0; justify-content:flex-start; text-align:left; background:transparent;}
.st-key-realtime_news_list [class*="st-key-telegram_open_"] button:hover {color:#c43b50; background:transparent;}
.st-key-realtime_news_list [class*="st-key-telegram_open_"] button>div {justify-content:flex-start; width:100%; text-align:left;}
.st-key-realtime_news_list [class*="st-key-telegram_open_"] button p {font-weight:600; font-size:1rem; line-height:1.6; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden;}
.st-key-realtime_news_list [data-testid="stText"] {font-size:.875rem; line-height:1.65; opacity:.72;}
.st-key-realtime_news_list [data-testid="stText"]>span {display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden;}
.st-key-realtime_news_list [data-testid="stCaptionContainer"] {margin:0;}
.st-key-realtime_news_list [data-testid="stCaptionContainer"] p {font-size:.78rem; margin:0;}
.st-key-realtime_news_reader {padding:.8rem .5rem .8rem .5rem;}
.st-key-realtime_news_reader [data-testid="stText"] {font-family:inherit; line-height:1.85; overflow-wrap:anywhere; white-space:pre-wrap;}
.st-key-realtime_news_reader h3 {font-size:1.2rem; line-height:1.5; padding-top:.25rem; padding-bottom:.5rem;}
@media(max-width:640px) {.st-key-realtime_news_list {border-right:0; padding-right:0;} .st-key-realtime_news_reader {padding:.4rem .8rem;}}
</style>"""


@st.fragment(run_every="30s")
def render_telegram_feed(db_path=DEFAULT_DB_PATH):
    try:
        config = load_config()
    except ValueError:
        st.warning("新聞連線設定未完成，請檢查本機設定。")
        return
    if config is None:
        st.caption("尚未連接新聞來源。")
        return

    receiver_notice = ensure_telegram_receiver(db_path)
    now = _utc_now()
    dates = [None, *_available_dates(now)]
    if st.session_state.get("telegram_feed_day") not in dates:
        st.session_state["telegram_feed_day"] = None
        _reset_feed()
    with st.container(horizontal=True, vertical_alignment="center"):
        selected_day = st.selectbox(
            "新聞日期", dates, format_func=lambda value: _day_label(value, now),
            key="telegram_feed_day", on_change=_reset_feed,
            label_visibility="collapsed", width=180, persist_state="session",
        )
        query = st.text_input(
            "搜尋新聞", placeholder="搜尋新聞、公司或關鍵字", type="search",
            key="telegram_feed_search", on_change=_reset_feed,
            label_visibility="collapsed", max_chars=200, persist_state="session",
        ).strip()[:200]
    identity = (config.chat_id, str(Path(db_path).resolve()), selected_day, query)
    if st.session_state.get("telegram_feed_filter") != identity:
        _reset_feed()
        st.session_state["telegram_feed_filter"] = identity
    st.session_state.setdefault("telegram_feed_limit", PAGE_SIZE)
    filters = dict(day=selected_day, now=now, chat_id=config.chat_id, query=query)
    try:
        with TelegramStore(db_path) as store:
            status = store.get_status()
            total = store.count_posts(**filters)
            if "telegram_feed_anchor" not in st.session_state:
                newest = store.list_posts(**filters, limit=1)
                if newest:
                    st.session_state["telegram_feed_anchor"] = _anchor(newest[0])
            anchor = st.session_state.get("telegram_feed_anchor")
            visible_total = store.count_posts(**filters, before=anchor)
            posts = []
            wanted = min(visible_total, st.session_state["telegram_feed_limit"])
            while len(posts) < wanted:
                batch = store.list_posts(**filters, before=anchor,
                                         limit=min(1000, wanted - len(posts)), offset=len(posts))
                if not batch:
                    break
                posts.extend(batch)
    except Exception:
        st.warning("暫時無法讀取新聞，稍後會自動重試。")
        return

    # Keep the reading panes at stable Streamlit element paths. Inserting a new
    # sibling above the columns would remount their scroll containers on refresh.
    connection_notice = st.empty()
    latest_notice = st.empty()
    if receiver_notice or status.get("last_error"):
        # Credentials, technical diagnostics and receiver internals never become
        # part of the reading interface. Existing items remain readable.
        connection_notice.caption("新聞更新暫時中斷，正在重試。")
    pending = max(0, total - visible_total)
    if pending:
        latest_notice.button(f"有 {pending} 則新消息 · 點此更新", key="telegram_feed_latest",
                             on_click=_show_latest, type="tertiary", icon=":material/arrow_upward:")
    if not posts:
        st.session_state.pop("telegram_feed_selected", None)
        st.caption("沒有符合條件的新聞。" if query or selected_day else "最近 7 天暫無新聞。新消息會自動出現。")
        return

    selected_id = st.session_state.get("telegram_feed_selected")
    selected = next((post for post in posts if post["message_id"] == selected_id), posts[0])
    st.session_state["telegram_feed_selected"] = selected["message_id"]
    st.html(_FEED_STYLE)
    left, right = st.columns([1.05, 1], gap="large")
    with left, st.container(key="realtime_news_list", gap=None, height=620, border=False):
        previous_day = None
        for post in posts:
            stamp = _local_time(post.get("published_at"))
            day = stamp[:10]
            if day != previous_day:
                st.caption(day)
                previous_day = day
            selected_marker = "selected_" if post["message_id"] == selected["message_id"] else ""
            with st.container(key=f"news_row_{selected_marker}{post['message_id']}", gap=None):
                st.caption(stamp[11:16])
                st.button(_escape_label(_headline(post)), key=f"telegram_open_{post['message_id']}",
                          on_click=_select_post, args=(post["message_id"],),
                          type="tertiary", width="stretch", wrap=True)
                preview = _preview(post)
                if preview:
                    st.text(preview)
        if len(posts) < visible_total:
            st.button("載入更多", key="telegram_feed_more", on_click=_load_more,
                      type="tertiary", width="stretch")
    with right, st.container(key="realtime_news_reader", height=620, border=False):
        stamp = _local_time(selected.get("published_at"))
        st.caption(f"{stamp[:16]} · 香港時間")
        st.subheader(_escape_label(_headline(selected)), anchor=False)
        # Never interpret source content as Markdown, HTML or instructions.
        body = _article_body(selected)
        if body:
            st.text(body)
        if selected.get("edited_at"):
            st.caption(f"更新於 {_local_time(selected['edited_at'])[:16]}")
        if selected.get("media_type"):
            st.caption("原文附有附件。")
        link = _post_link(selected)
        if link:
            st.link_button("查看原文", link, type="tertiary", icon=":material/open_in_new:")
