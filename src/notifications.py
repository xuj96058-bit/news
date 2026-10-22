"""Durable, source-specific Telegram delivery without retaining credentials.

Each input database owns its outbox, so saving news and queuing its delivery are
one transaction. A route is explicitly activated after an initial baseline.
Telegram has no idempotency key: uncertain sends are held for inspection rather
than retried blindly. The same sender may service both input databases.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import html
import json
import os
import re
import sqlite3
import threading
import time
from urllib.parse import urlsplit
import uuid
from zoneinfo import ZoneInfo

import requests


SOURCES = frozenset({"telegram", "investat"})
SHORT_LIMIT = 250
LEASE_SECONDS = 120
SAFE_ERRORS = {
    "transport": "無法連接 Telegram，稍後重試。",
    "uncertain": "Telegram 發送結果未確認；已暫停此項，請先核對目的頻道。",
    "permission": "Bot 沒有目的頻道發文權限，請檢查頻道管理員設定。",
    "authentication": "推送 Bot Token 無效，請檢查本機設定。",
    "rate_limit": "Telegram 暫時限制發送，將按要求等待後重試。",
    "rejected": "Telegram 拒絕這項推送，請檢查目的地與內容。",
    "internal": "推送處理暫時失敗，稍後重試。",
}


@dataclass(frozen=True)
class NotificationConfig:
    token: str = field(repr=False)
    destination_chat_id: int

    def __post_init__(self):
        if not isinstance(self.token, str) or not re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,100}", self.token):
            raise ValueError("TELEGRAM_NOTIFY_BOT_TOKEN 格式不正確。")
        if (isinstance(self.destination_chat_id, bool)
                or not isinstance(self.destination_chat_id, int)
                or not -1997852516352 <= self.destination_chat_id <= -1000000000001):
            raise ValueError("TELEGRAM_NOTIFY_CHAT_ID 必須是目的頻道的數字 Chat ID。")

    @property
    def bot_id(self):
        return int(self.token.split(":", 1)[0])


def load_notification_config():
    from . import env  # noqa: F401
    if os.getenv("TELEGRAM_NOTIFY_ENABLED", "1").strip().casefold() in {"0", "false", "no", "off"}:
        return None
    token = os.getenv("TELEGRAM_NOTIFY_BOT_TOKEN", "").strip()
    destination = os.getenv("TELEGRAM_NOTIFY_CHAT_ID", "").strip()
    if not token and not destination:
        return None
    if not token or not re.fullmatch(r"-[0-9]+", destination):
        raise ValueError("請同時設定推送 Bot Token 與目的頻道 Chat ID。")
    if destination == os.getenv("TELEGRAM_CHAT_ID", "").strip():
        raise ValueError("新聞來源頻道與推送目的頻道必須不同。")
    return NotificationConfig(token, int(destination))


def initialize_notifications(connection):
    """Call during store initialization, before any ingestion transaction."""
    if connection.in_transaction:
        raise ValueError("Notification initialization must precede ingestion")
    if connection.row_factory is None:
        connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS notification_routes (
            source TEXT PRIMARY KEY CHECK(source IN ('telegram','investat')),
            bot_id INTEGER NOT NULL, destination_chat_id INTEGER NOT NULL,
            activated_at REAL NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
            dispatch_owner TEXT, dispatch_until REAL);
        CREATE TABLE IF NOT EXISTS notification_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL CHECK(source IN ('telegram','investat')),
            source_key TEXT NOT NULL, bot_id INTEGER NOT NULL,
            destination_chat_id INTEGER NOT NULL,
            payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK(state IN ('pending','sending','retry','sent','uncertain','expired')),
            attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL DEFAULT 0,
            last_error TEXT, lease_owner TEXT, lease_until REAL,
            sent_message_id INTEGER, sent_kind TEXT, sent_hash TEXT,
            prepared_json TEXT, prepared_hash TEXT,
            created_at REAL NOT NULL, updated_at REAL NOT NULL, sent_at REAL,
            UNIQUE(source,source_key,bot_id,destination_chat_id));
        CREATE INDEX IF NOT EXISTS notification_due
            ON notification_outbox(bot_id,destination_chat_id,state,next_attempt_at,id);
    """)


def _source(source):
    if source not in SOURCES:
        raise ValueError("Only Telegram and Investat may produce notifications")


def has_route(connection, source, config):
    _source(source)
    return connection.execute("""SELECT 1 FROM notification_routes
        WHERE source=? AND bot_id=? AND destination_chat_id=? AND enabled=1""",
        (source, config.bot_id, config.destination_chat_id)).fetchone() is not None


def configure_route(connection, source, config):
    """Activate delivery of future records only; never scan historical articles."""
    _source(source)
    if connection.in_transaction:
        raise ValueError("Activate notification routes outside ingestion transactions")
    with connection:
        connection.execute("""INSERT INTO notification_routes
            (source,bot_id,destination_chat_id,activated_at) VALUES(?,?,?,?)
            ON CONFLICT(source) DO UPDATE SET bot_id=excluded.bot_id,
            destination_chat_id=excluded.destination_chat_id,enabled=1,
            activated_at=CASE WHEN notification_routes.bot_id=excluded.bot_id
                AND notification_routes.destination_chat_id=excluded.destination_chat_id
                THEN notification_routes.activated_at ELSE excluded.activated_at END,
            dispatch_owner=CASE WHEN notification_routes.bot_id=excluded.bot_id
                AND notification_routes.destination_chat_id=excluded.destination_chat_id
                THEN notification_routes.dispatch_owner ELSE NULL END,
            dispatch_until=CASE WHEN notification_routes.bot_id=excluded.bot_id
                AND notification_routes.destination_chat_id=excluded.destination_chat_id
                THEN notification_routes.dispatch_until ELSE NULL END""",
            (source, config.bot_id, config.destination_chat_id, time.time()))


def _text(value):
    # Replace invalid surrogate code points before writing UTF-8 attachments.
    return value.strip().encode("utf-8", "replace").decode("utf-8") if isinstance(value, str) else ""


def _safe_url(value):
    value = _text(value)
    try:
        parts = urlsplit(value)
        if (parts.scheme.lower() in {"http", "https"} and parts.hostname
                and parts.username is None and parts.password is None):
            parts.port
            return value
    except ValueError:
        pass
    return ""


def _payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("Notification payload must be a mapping")
    value = {key: _text(payload.get(key)) for key in
             ("title", "text", "summary", "published_at", "content_scope")}
    value["url"] = _safe_url(payload.get("url"))
    segments = _segments(payload)
    if segments:
        value["segments"] = segments
    return value


def _link_href(value):
    value = _text(value)
    if re.match(r"(?i)^tg://", value) and not re.search(r"[\s<>\"']", value):
        return value
    return _safe_url(value)


def _segments(payload):
    """Named-link segments [[text, href|None], ...] covering the raw text."""
    raw = payload.get("segments")
    if isinstance(raw, list):
        result = []
        for item in raw:
            if (isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[0], str)
                    and (item[1] is None or isinstance(item[1], str))):
                result.append([item[0].encode("utf-8", "replace").decode("utf-8"),
                               _link_href(item[1]) or None if item[1] else None])
        return result if any(href for _, href in result) else []
    links, text = payload.get("links"), payload.get("text")
    if not isinstance(links, list) or not isinstance(text, str) or not links:
        return []
    encoded = text.encode("utf-16-le", "surrogatepass")
    result, cursor = [], 0
    for link in links:
        if not (isinstance(link, (list, tuple)) and len(link) == 3):
            continue
        offset, length, href = link
        if (isinstance(offset, bool) or isinstance(length, bool) or not isinstance(offset, int)
                or not isinstance(length, int) or offset < cursor or length <= 0
                or (offset + length) * 2 > len(encoded)):
            continue
        href = _link_href(href)
        if not href:
            continue
        part = lambda a, b: encoded[a * 2:b * 2].decode("utf-16-le", "replace")
        if offset > cursor:
            result.append([part(cursor, offset), None])
        result.append([part(offset, offset + length), href])
        cursor = offset + length
    if not result:
        return []
    result.append([encoded[cursor * 2:].decode("utf-16-le", "replace"), None])
    # Match _text(): the stored text is stripped.
    result[0][0] = result[0][0].lstrip()
    result[-1][0] = result[-1][0].rstrip()
    return [item for item in result if item[0]]


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(payload):
    # Re-fetch times and publication corrections alone do not create alerts.
    return hashlib.sha256(_json({key: value for key, value in payload.items()
                                if key != "published_at"}).encode("utf-8")).hexdigest()


def enqueue_notification(connection, source, key, payload, *, is_new):
    """Queue in the caller's transaction, retaining the exact source snapshot."""
    _source(source)
    if not isinstance(key, str) or not key or len(key) > 512:
        raise ValueError("Notification source identity is invalid")
    route = connection.execute("SELECT * FROM notification_routes WHERE source=? AND enabled=1", (source,)).fetchone()
    if route is None:
        return False
    target = (source, key, route["bot_id"], route["destination_chat_id"])
    old = connection.execute("""SELECT * FROM notification_outbox
        WHERE source=? AND source_key=? AND bot_id=? AND destination_chat_id=?""", target).fetchone()
    # Editing a pre-activation article must not backfill it into the channel.
    if old is None and not is_new:
        return False
    value = _payload(payload)
    if not any(value.get(name) for name in ("title", "text", "summary")):
        return False
    signature, now = _digest(value), time.time()
    if old is not None and (old["state"] == "expired" or old["payload_hash"] == signature):
        return False
    if old is None:
        connection.execute("""INSERT INTO notification_outbox
            (source,source_key,bot_id,destination_chat_id,payload_json,payload_hash,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?)""", (*target, _json(value), signature, now, now))
    else:
        connection.execute("""UPDATE notification_outbox SET payload_json=?,payload_hash=?,
            revision=revision+1,updated_at=?,state=CASE WHEN state IN ('sending','uncertain')
                THEN state ELSE 'pending' END,next_attempt_at=0,
            last_error=CASE WHEN state='uncertain' THEN last_error ELSE NULL END WHERE id=?""",
            (_json(value), signature, now, old["id"]))
    return True


def prune_notifications(connection, now=None):
    """Scrub expired Telegram text while retaining only delivery identities."""
    if connection.in_transaction:
        raise ValueError("Prune notifications outside ingestion transactions")
    if now is None:
        current = datetime.now(timezone.utc)
    elif isinstance(now, str):
        current = datetime.fromisoformat(now.replace("Z", "+00:00"))
    else:
        current = now
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    cutoff = (current - timedelta(days=7)).timestamp()
    expired = []
    for row in connection.execute("""SELECT id,payload_json,created_at FROM notification_outbox
        WHERE source='telegram' AND state!='expired'"""):
        try:
            value = json.loads(row["payload_json"]).get("published_at", "")
            published = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            stamp = published.timestamp()
        except (ValueError, TypeError, AttributeError, OverflowError):
            stamp = row["created_at"]
        if stamp <= cutoff:
            expired.append(row["id"])
    with connection:
        connection.executemany("""UPDATE notification_outbox SET state='expired',payload_json='{}',
            prepared_json=NULL,prepared_hash=NULL,last_error=NULL,lease_owner=NULL,lease_until=NULL
            WHERE id=?""", ((identity,) for identity in expired))
    return len(expired)


def _units(text):
    return len(text.encode("utf-16-le")) // 2


def _clip(text, limit):
    if _units(text) <= limit:
        return text
    encoded = text.encode("utf-16-le")[:max(0, limit - 1) * 2]
    return encoded.decode("utf-16-le", "ignore") + "…"


def _local_time(value):
    try:
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            stamp = parsedate_to_datetime(value)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d %H:%M 香港時間")
    except (ValueError, TypeError, AttributeError, OverflowError):
        return "發布時間未提供"


def strip_markdown(value):
    """Remove Markdown syntax so plain-text Telegram messages read cleanly."""
    if not value:
        return value
    lines = []
    in_fence = False
    for line in value.split("\n"):
        if re.match(r"^\s*(```|~~~)", line):
            in_fence = not in_fence
            continue
        if not in_fence:
            line = re.sub(r"^\s{0,3}#{1,6}\s+", "", line)
            line = re.sub(r"^\s{0,3}>\s?", "", line)
            line = re.sub(r"^(\s*)[*+]\s+", r"\1- ", line)
            if re.fullmatch(r"\s*([-*_])(\s*\1){2,}\s*", line):
                line = ""
            line = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", line)
            line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)
            line = re.sub(r"`+([^`]*)`+", r"\1", line)
            line = re.sub(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", r"\2", line)
            line = re.sub(r"(?<![\w*])\*(?=\S)([^*]+?)(?<=\S)\*(?![\w*])", r"\1", line)
            line = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"\1", line)
            line = line.replace("**", "").replace("`", "")
        lines.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def format_notification(source, payload, *, updated=False):
    """Return a single plain-text message or a UTF-8 document plus caption."""
    _source(source)
    value = _payload(payload)
    if source == "investat":
        for key in ("title", "text", "summary"):
            value[key] = strip_markdown(value[key])
    source_name = "Investat" if source == "investat" else "實時新聞"
    title = value["title"]
    heading = f"{'【更新】' if updated else ''}【{source_name}】" + (" " + title if title else "")
    scope = {"partial_text": "內容範圍：部分正文", "summary": "內容範圍：原有摘要",
             "title_only": "內容範圍：僅標題"}.get(value["content_scope"], "")
    metadata = [_local_time(value["published_at"])] + ([scope] if scope else [])
    text = value["text"] or value["summary"]
    segments = [list(item) for item in value.get("segments", [])] if value["text"] else []
    # A Telegram source may store its first line as title. Avoid duplication.
    drop = 0
    if title and text == title:
        text, segments = "", []
    elif title and text.startswith(title + "\n"):
        drop = len(text) - len(text[len(title):].lstrip("\n"))
        text = text[drop:]
    if segments and drop:
        trimmed = []
        for chunk, href in segments:
            if drop >= len(chunk):
                drop -= len(chunk)
                continue
            trimmed.append([chunk[drop:], href])
            drop = 0
        segments = trimmed
    url = "" if _is_telegram_url(value["url"]) else value["url"]
    body = "\n\n".join(part for part in (heading, "\n".join(metadata), text, url) if part)
    if segments and any(href for _, href in segments):
        rendered = "".join(f'<a href="{html.escape(href, quote=True)}">{html.escape(chunk, quote=False)}</a>'
                           if href else html.escape(chunk, quote=False) for chunk, href in segments)
        html_body = "\n\n".join(part for part in (html.escape(heading, quote=False),
            html.escape("\n".join(metadata), quote=False), rendered,
            html.escape(url, quote=False)) if part)
        if _units(html_body) <= SHORT_LIMIT:
            return {"kind": "text", "text": html_body, "parse_mode": "HTML"}
        html_caption = True
    else:
        if _units(body) <= SHORT_LIMIT:
            return {"kind": "text", "text": body}
        html_caption = False
    caption_heading = _clip(heading, 300)
    caption_parts = [caption_heading, "\n".join(metadata)]
    if value["summary"]:
        caption_parts.append(_clip(value["summary"], 300))
    if url and _units(url) <= 250:
        caption_parts.append(url)
    caption_parts.append("正文見附上的 TXT 檔。")
    caption = _clip("\n\n".join(caption_parts), 1024)
    result = {}
    if html_caption:
        # Escape whole plain parts after clipping so no entity/tag is split.
        limit = 1024
        while True:
            caption = html.escape(_clip("\n\n".join(caption_parts), limit), quote=False)
            if _units(caption) <= 1024:
                break
            limit -= max(1, _units(caption) - 1024)
        result["parse_mode"] = "HTML"
    filename_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")[:60] or source_name
    result.update({"kind": "document", "caption": caption,
                   "filename": filename_title + ".txt", "document": body.encode("utf-8")})
    return result


def _is_telegram_url(value):
    if not value:
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return True
    host = (parts.hostname or "").lower()
    return (parts.scheme.lower() == "tg" or host in {"t.me", "telegram.me", "telegram.dog"}
            or host.endswith(".t.me"))


class DeliveryError(Exception):
    """Predefined, secret-free failures. Unknown delivery outcomes are explicit."""
    def __init__(self, category="internal", *, retry_after=None, uncertain=False):
        self.category = category if category in SAFE_ERRORS else "internal"
        self.uncertain = bool(uncertain)
        self.retry_after = min(86400, max(1, retry_after)) if isinstance(retry_after, (int, float)) else None
        super().__init__(SAFE_ERRORS[self.category])


class TelegramSender:
    def __init__(self, config, *, session=None, min_interval=1.1):
        self.config = config
        self.session = session or requests.Session()
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last_request = 0.0
        self._blocked_until = 0.0

    @property
    def cooldown_remaining(self):
        return max(0.0, self._blocked_until - time.monotonic())

    def _request(self, method, payload, *, files=None, changes_message=True):
        with self._lock:
            if self.cooldown_remaining > 0:
                raise DeliveryError("rate_limit", retry_after=self.cooldown_remaining)
            delay = self.min_interval - (time.monotonic() - self._last_request)
            if delay > 0:
                time.sleep(delay)
            try:
                kwargs = {"data": payload, "files": files} if files else {"json": payload}
                response = self.session.post("https://api.telegram.org/bot" + self.config.token + "/" + method,
                    timeout=(5, 45), allow_redirects=False, **kwargs)
            except requests.ConnectTimeout:
                raise DeliveryError("transport", retry_after=30) from None
            except requests.RequestException:
                raise DeliveryError("uncertain" if changes_message else "transport",
                                    uncertain=changes_message, retry_after=30) from None
            finally:
                self._last_request = time.monotonic()
        try:
            result = response.json()
        except (ValueError, requests.RequestException):
            raise DeliveryError("uncertain" if changes_message else "transport",
                                uncertain=changes_message, retry_after=60) from None
        finally:
            response.close()
        if isinstance(result, dict) and response.status_code == 200 and result.get("ok") is True:
            return result.get("result")
        code = result.get("error_code", response.status_code) if isinstance(result, dict) else response.status_code
        if code == 400 and method.startswith("edit") and isinstance(result, dict):
            # This Telegram response proves the idempotent edit already matches.
            description = result.get("description", "")
            if isinstance(description, str) and "message is not modified" in description.casefold():
                return {"message_id": payload["message_id"]}
        if code == 429:
            params = result.get("parameters", {}) if isinstance(result, dict) else {}
            retry = params.get("retry_after", 60) if isinstance(params, dict) else 60
            error = DeliveryError("rate_limit", retry_after=retry)
            self._blocked_until = time.monotonic() + (error.retry_after or 60)
            raise error
        if code == 401:
            raise DeliveryError("authentication", retry_after=900)
        if code == 403:
            raise DeliveryError("permission", retry_after=300)
        if code == 400:
            raise DeliveryError("rejected", retry_after=900)
        # Unparseable/5xx responses can follow a successful send. Be conservative.
        raise DeliveryError("uncertain" if changes_message else "transport",
                            uncertain=changes_message, retry_after=60)

    def check_destination(self):
        me = self._request("getMe", {}, changes_message=False)
        if not isinstance(me, dict) or me.get("id") != self.config.bot_id:
            raise DeliveryError("authentication", retry_after=900)
        chat = self._request("getChat", {"chat_id": self.config.destination_chat_id}, changes_message=False)
        member = self._request("getChatMember", {"chat_id": self.config.destination_chat_id,
                               "user_id": self.config.bot_id}, changes_message=False)
        if (not isinstance(chat, dict) or chat.get("type") != "channel"
                or not isinstance(member, dict)
                or not (member.get("status") == "creator" or
                        member.get("status") == "administrator" and member.get("can_post_messages") is True)):
            raise DeliveryError("permission", retry_after=300)
        return {"bot_id": me["id"], "username": me.get("username"),
                "chat_id": chat.get("id"), "title": chat.get("title"), "can_post_messages": True}

    def deliver(self, source, payload, *, message_id=None, previous_kind=None):
        content = format_notification(source, payload, updated=message_id is not None)
        base = {"chat_id": self.config.destination_chat_id}
        if message_id and previous_kind == "document":
            # Keep an existing document message as a document even if now short.
            if content["kind"] == "text":
                plain = content["text"]
                if content.get("parse_mode") == "HTML":
                    plain = html.unescape(re.sub(r"<[^>]*>", "", plain))
                content = {"kind": "document", "document": plain.encode("utf-8"),
                           "filename": "news-update.txt", "caption": _clip(plain, 1000)}
            media = {"type": "document", "media": "attach://document", "caption": content["caption"]}
            if content.get("parse_mode"):
                media["parse_mode"] = content["parse_mode"]
            result = self._request("editMessageMedia", {**base, "message_id": message_id, "media": _json(media)},
                files={"document": (content["filename"], content["document"], "text/plain; charset=utf-8")})
        elif message_id and content["kind"] == "text":
            result = self._request("editMessageText", {**base, "message_id": message_id,
                "text": content["text"], "link_preview_options": {"is_disabled": True}, **_mode(content)})
        elif content["kind"] == "document":
            # Text -> document needs a new post; explicitly mark it an update.
            payload_data = {**base, "caption": content["caption"], **_mode(content)}
            if message_id:
                payload_data["reply_parameters"] = _json({"message_id": message_id, "allow_sending_without_reply": True})
            result = self._request("sendDocument", payload_data,
                files={"document": (content["filename"], content["document"], "text/plain; charset=utf-8")})
        else:
            result = self._request("sendMessage", {**base, "text": content["text"],
                "link_preview_options": {"is_disabled": True}, **_mode(content)})
        if not isinstance(result, dict) or isinstance(result.get("message_id"), bool) or not isinstance(result.get("message_id"), int):
            raise DeliveryError("uncertain", uncertain=True)
        return {"message_id": result["message_id"], "kind": content["kind"]}


def _mode(content):
    return {"parse_mode": content["parse_mode"]} if content.get("parse_mode") else {}


def _claim(connection, config):
    now, owner = time.time(), uuid.uuid4().hex
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("""UPDATE notification_outbox SET state='uncertain',last_error=?,
            lease_owner=NULL,lease_until=NULL WHERE bot_id=? AND destination_chat_id=?
            AND state='sending' AND lease_until<=?""",
            (SAFE_ERRORS["uncertain"], config.bot_id, config.destination_chat_id, now))
        row = connection.execute("""SELECT q.* FROM notification_outbox q JOIN notification_routes r
            ON r.source=q.source AND r.bot_id=q.bot_id AND r.destination_chat_id=q.destination_chat_id
            WHERE q.bot_id=? AND q.destination_chat_id=? AND r.enabled=1
            AND q.state IN ('pending','retry') AND q.next_attempt_at<=?
            AND (r.dispatch_owner IS NULL OR r.dispatch_until<=?)
            ORDER BY q.created_at,q.id LIMIT 1""",
            (config.bot_id, config.destination_chat_id, now, now)).fetchone()
        if row is None:
            connection.commit()
            return None
        connection.execute("UPDATE notification_routes SET dispatch_owner=?,dispatch_until=? WHERE source=?",
                           (owner, now + LEASE_SECONDS, row["source"]))
        connection.execute("""UPDATE notification_outbox SET state='sending',lease_owner=?,lease_until=?,
            attempts=attempts+1 WHERE id=?""", (owner, now + LEASE_SECONDS, row["id"]))
        connection.commit()
        return dict(row), owner
    except Exception:
        connection.rollback()
        raise


def process_pending(connection, config, client=None, limit=10, prepare_payload=None):
    """Deliver bounded work. Caller must not hold an ingestion transaction."""
    if connection.in_transaction:
        raise ValueError("Delivery must run outside ingestion transactions")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("Notification batch size must be 1–100")
    prune_notifications(connection)
    client = client or TelegramSender(config)
    counts = {"sent": 0, "retry": 0, "uncertain": 0}
    for _ in range(limit):
        if getattr(client, "cooldown_remaining", 0) > 0:
            break
        claim = _claim(connection, config)
        if claim is None:
            break
        row, owner = claim
        delivery_started = False
        try:
            if row["prepared_hash"] == row["payload_hash"] and row["prepared_json"]:
                payload = json.loads(row["prepared_json"])
            else:
                payload = json.loads(row["payload_json"])
                if prepare_payload is not None:
                    payload = _payload(prepare_payload(dict(payload)))
                with connection:
                    connection.execute("""UPDATE notification_outbox SET prepared_json=?,prepared_hash=?
                        WHERE id=? AND lease_owner=?""",
                        (_json(payload), row["payload_hash"], row["id"], owner))
            delivery_started = True
            delivered = client.deliver(row["source"], payload,
                message_id=row["sent_message_id"], previous_kind=row["sent_kind"])
            if (not isinstance(delivered, dict) or not isinstance(delivered.get("message_id"), int)
                    or isinstance(delivered.get("message_id"), bool)
                    or delivered.get("kind") not in {"text", "document"}):
                raise DeliveryError("uncertain", uncertain=True)
            with connection:
                changed = connection.execute("""UPDATE notification_outbox SET
                    state=CASE WHEN revision=? THEN 'sent' ELSE 'pending' END,
                    sent_message_id=?,sent_kind=?,sent_hash=?,sent_at=?,last_error=NULL,
                    next_attempt_at=0,lease_owner=NULL,lease_until=NULL
                    WHERE id=? AND lease_owner=?""",
                    (row["revision"], delivered["message_id"], delivered["kind"], row["payload_hash"],
                     time.time(), row["id"], owner)).rowcount
            counts["sent" if changed else "uncertain"] += 1
        except Exception as exc:
            error = exc if isinstance(exc, DeliveryError) else DeliveryError(
                "uncertain" if delivery_started else "internal", uncertain=delivery_started)
            state = "uncertain" if error.uncertain else "retry"
            delay = error.retry_after or min(900, 30 * 2 ** min(row["attempts"], 5))
            with connection:
                connection.execute("""UPDATE notification_outbox SET state=?,last_error=?,
                    next_attempt_at=?,lease_owner=NULL,lease_until=NULL WHERE id=? AND lease_owner=?""",
                    (state, str(error), time.time() + delay, row["id"], owner))
            counts[state] += 1
        finally:
            with connection:
                connection.execute("""UPDATE notification_routes SET dispatch_owner=NULL,dispatch_until=NULL
                    WHERE source=? AND dispatch_owner=?""", (row["source"], owner))
    return counts


def notification_status(connection, config):
    counts = {name: 0 for name in ("pending", "sending", "retry", "sent", "uncertain", "expired")}
    for row in connection.execute("""SELECT state,COUNT(*) AS count FROM notification_outbox
        WHERE bot_id=? AND destination_chat_id=? GROUP BY state""",
        (config.bot_id, config.destination_chat_id)):
        counts[row["state"]] = row["count"]
    return counts
