"""Recent Telegram channel posts in the daily report, without model requests."""

from datetime import datetime, timedelta, timezone
import hashlib
import math
from pathlib import Path
import threading
from zoneinfo import ZoneInfo

import streamlit as st

from .telegram_source import DEFAULT_DB_PATH, TelegramStore, load_config, run_worker


HONG_KONG = ZoneInfo("Asia/Hong_Kong")
PAGE_SIZE = 20


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
    """A rolling 72-hour window may overlap four Hong Kong calendar dates."""
    latest = now.astimezone(HONG_KONG).date()
    earliest = (now - timedelta(hours=72)).astimezone(HONG_KONG).date()
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


def _reset_page():
    st.session_state["telegram_feed_page"] = 0


def _move_page(delta):
    st.session_state["telegram_feed_page"] = max(
        0, st.session_state.get("telegram_feed_page", 0) + delta
    )


@st.fragment(run_every="30s")
def render_telegram_feed(db_path=DEFAULT_DB_PATH):
    st.subheader("Telegram 頻道新聞")
    st.caption("保留發布後 72 小時內的頻道訊息，每 30 秒更新；日期與時間以香港時間顯示。")
    try:
        config = load_config()
    except ValueError:
        st.warning("Telegram 設定未完成，請檢查本機的 Bot Token 與頻道 Chat ID。")
        return
    if config is None:
        st.info("在本機 .env 設定 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID，並將 Bot 加入頻道後，重新啟動 News 即可接收。")
        return

    receiver_notice = ensure_telegram_receiver(db_path)
    if receiver_notice:
        st.warning(receiver_notice)

    now = _utc_now()
    dates = [None, *_available_dates(now)]
    if st.session_state.get("telegram_feed_day") not in dates:
        st.session_state["telegram_feed_day"] = None
        _reset_page()
    selected_day = st.selectbox(
        "頻道新聞日期", dates,
        format_func=lambda value: "最近 72 小時" if value is None else value.isoformat(),
        key="telegram_feed_day", on_change=_reset_page,
    )
    st.button("更新頻道列表", key="telegram_feed_refresh", icon=":material/refresh:")
    st.session_state.setdefault("telegram_feed_page", 0)
    try:
        with TelegramStore(db_path) as store:
            status = store.get_status()
            total = store.count_posts(day=selected_day, now=now, chat_id=config.chat_id)
            pages = max(1, math.ceil(total / PAGE_SIZE))
            page = min(max(0, st.session_state["telegram_feed_page"]), pages - 1)
            st.session_state["telegram_feed_page"] = page
            posts = store.list_posts(
                day=selected_day, now=now, chat_id=config.chat_id,
                limit=PAGE_SIZE, offset=page * PAGE_SIZE,
            )
    except Exception:
        st.warning("暫時無法讀取頻道新聞，稍後會自動重試。")
        return

    if status.get("last_error"):
        # The backend stores only static, sanitized API failure descriptions.
        st.warning(f"Telegram 接收暫時受阻：{status['last_error']}")
    if status.get("last_success_at"):
        st.caption(f"上次成功同步：{_local_time(status['last_success_at'])}（香港時間）")
    else:
        st.caption("正在等待首次同步。")

    if not posts:
        if selected_day is not None:
            st.info("這個日期沒有仍在保留期限內的頻道訊息。")
        else:
            st.info("目前沒有最近 72 小時的頻道新聞。加入 Bot 後，在頻道發布一則新訊息即可確認接收；不會自動補抓加入前的歷史。")
        return

    st.caption(f"共 {total} 則 · 第 {page + 1}／{pages} 頁 · 依發布時間由新到舊")
    with st.container(horizontal=True):
        st.button("上一頁", key="telegram_feed_previous", disabled=page == 0,
                  on_click=_move_page, args=(-1,))
        st.button("下一頁", key="telegram_feed_next", disabled=page + 1 >= pages,
                  on_click=_move_page, args=(1,))

    for post in posts:
        with st.container(border=True):
            st.text(str(post.get("title") or "Telegram 頻道"))
            stamp = _local_time(post.get("published_at"))
            edited = post.get("edited_at")
            st.caption(f"發布於 {stamp}" + (f" · 更新於 {_local_time(edited)}" if edited else ""))
            # Channel content is untrusted text, not Markdown/HTML or instructions.
            content = str(post.get("text") or "")
            st.text(content or "此則訊息沒有文字內容，請到 Telegram 查看附件。", width="stretch")
            if post.get("media_type"):
                st.caption("含附件；此處只顯示文字說明。")
            link = _post_link(post)
            if link:
                st.link_button("在 Telegram 查看原訊息", link)
