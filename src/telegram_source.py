"""A small, restart-safe Telegram channel inbox with a rolling seven-day TTL.

This database is deliberately separate from the permanent research/news store.
Only normalized channel text is retained; credentials and raw updates are never
written to it. Bot API polling receives new updates, not channel history.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid
from zoneinfo import ZoneInfo

import requests

from .notifications import initialize_notifications, enqueue_notification, prune_notifications


DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "telegram.sqlite3"
RETENTION = timedelta(days=7)
LEASE_SECONDS = 120


def _now(value=None):
    value = value or datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


@dataclass(frozen=True)
class TelegramConfig:
    token: str = field(repr=False)
    chat_id: int

    def __post_init__(self):
        if not isinstance(self.token, str) or not re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,100}", self.token):
            raise ValueError("TELEGRAM_BOT_TOKEN 格式不正確。")
        if isinstance(self.chat_id, bool) or not isinstance(self.chat_id, int) or not -1997852516352 <= self.chat_id <= -1000000000001:
            raise ValueError("TELEGRAM_CHAT_ID 必須是頻道的數字 Chat ID。")

    @property
    def bot_id(self):
        return int(self.token.split(":", 1)[0])


def load_config():
    from . import env  # noqa: F401 — existing project-local .env loading
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token and not chat:
        return None
    if not token or not chat:
        raise ValueError("請同時設定 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID。")
    if not re.fullmatch(r"-[0-9]+", chat):
        raise ValueError("TELEGRAM_CHAT_ID 必須是頻道的數字 Chat ID。")
    return TelegramConfig(token, int(chat))


class TelegramError(Exception):
    """Only predefined, credential-free messages may be put in this exception."""


class TelegramClient:
    def __init__(self, config):
        self.config = config

    def _request(self, method, payload=None):
        try:
            response = requests.post(
                "https://api.telegram.org/bot" + self.config.token + "/" + method,
                json=payload or {}, timeout=(5, 35), allow_redirects=False,
            )
            try:
                data = response.json()
            except (ValueError, requests.RequestException):
                raise TelegramError("Telegram 回傳無法解析的資料。") from None
            code = data.get("error_code", response.status_code) if isinstance(data, dict) else response.status_code
            errors = {
                401: "Telegram Token 無效，請檢查本機設定。",
                403: "Telegram 拒絕存取，請確認 Bot 的頻道權限。",
                409: "這個 Bot 已有其他接收程式或 Webhook，請先停止重複接收。",
                429: "Telegram 暫時限制請求，接收程式稍後會重試。",
            }
            if not isinstance(data, dict) or response.status_code != 200 or data.get("ok") is not True:
                raise TelegramError(errors.get(code, "Telegram 請求失敗，接收程式稍後會重試。"))
            return data.get("result")
        except requests.RequestException:
            # Request exceptions can embed the token-bearing URL. Never retain them.
            raise TelegramError("無法連接 Telegram，請檢查網路。") from None

    def has_webhook(self):
        result = self._request("getWebhookInfo")
        if not isinstance(result, dict):
            raise TelegramError("Telegram 回傳無法解析的資料。")
        return bool(result.get("url"))

    def get_updates(self, offset):
        result = self._request("getUpdates", {
            "offset": offset, "limit": 100, "timeout": 0,
            "allowed_updates": ["channel_post", "edited_channel_post"],
        })
        if not isinstance(result, list):
            raise TelegramError("Telegram 回傳無法解析的資料。")
        return result


class TelegramStore:
    def __init__(self, db_path=DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.db_path, timeout=15)
        self.connection.row_factory = sqlite3.Row
        # DELETE journaling plus secure_delete avoids keeping expired text in
        # unused pages or a long-lived WAL. This does not erase external backups.
        self.connection.execute("PRAGMA journal_mode=DELETE")
        self.connection.execute("PRAGMA secure_delete=ON")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS telegram_posts (
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                title TEXT NOT NULL, text TEXT NOT NULL, published_ts REAL NOT NULL,
                edited_ts REAL, media_type TEXT, url TEXT NOT NULL,
                PRIMARY KEY(chat_id,message_id));
            CREATE INDEX IF NOT EXISTS telegram_posts_date ON telegram_posts(published_ts);
            CREATE TABLE IF NOT EXISTS telegram_state (
                id INTEGER PRIMARY KEY CHECK(id=1), bot_id INTEGER, chat_id INTEGER,
                next_update_id INTEGER NOT NULL DEFAULT 0, last_success_at TEXT,
                last_error TEXT, last_poll_at TEXT, lease_owner TEXT, lease_until REAL);
            INSERT OR IGNORE INTO telegram_state(id) VALUES(1);
        """)
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(telegram_posts)")}
        if "links_json" not in columns:
            self.connection.execute("ALTER TABLE telegram_posts ADD COLUMN links_json TEXT NOT NULL DEFAULT '[]'")
        self.connection.commit()
        initialize_notifications(self.connection)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.connection.close()

    def prune(self, now=None):
        cutoff = (_now(now) - RETENTION).timestamp()
        # Delivery snapshots obey the same retention window as the inbox.
        # This helper owns its transaction; keep it outside the post deletion.
        prune_notifications(self.connection, now=now)
        with self.connection:
            return self.connection.execute("DELETE FROM telegram_posts WHERE published_ts<=?", (cutoff,)).rowcount

    def _filters(self, day=None, now=None, chat_id=None, timezone_name="Asia/Hong_Kong", query="", before=None):
        self.prune(now)
        clauses, values = [], []
        if chat_id is not None:
            clauses.append("chat_id=?")
            values.append(chat_id)
        if day is not None:
            if isinstance(day, str):
                day = date.fromisoformat(day)
            if isinstance(day, datetime):
                day = day.date()
            start = datetime.combine(day, datetime.min.time(), tzinfo=ZoneInfo(timezone_name))
            end = start + timedelta(days=1)
            clauses.append("published_ts>=? AND published_ts<?")
            values.extend((start.timestamp(), end.timestamp()))
        if not isinstance(query, str):
            raise ValueError("Invalid Telegram search query")
        if query:
            # User input is a literal substring, including SQL wildcard characters.
            clauses.append("text LIKE ? ESCAPE '\\'")
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            values.append(f"%{escaped}%")
        if before is not None:
            if (not isinstance(before, (tuple, list)) or len(before) != 2
                    or isinstance(before[0], bool) or not isinstance(before[0], (int, float))
                    or not math.isfinite(before[0]) or isinstance(before[1], bool)
                    or not isinstance(before[1], int) or before[1] <= 0):
                raise ValueError("Invalid Telegram news anchor")
            # Include the anchor, with message_id as the deterministic tie breaker.
            clauses.append("(published_ts<? OR (published_ts=? AND message_id<=?))")
            values.extend((before[0], before[0], before[1]))
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), values

    def list_posts(self, day=None, now=None, limit=100, offset=0, timezone_name="Asia/Hong_Kong", chat_id=None, query="", before=None):
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000 or not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValueError("Invalid Telegram pagination")
        where, values = self._filters(day, now, chat_id, timezone_name, query, before)
        rows = self.connection.execute("SELECT * FROM telegram_posts" + where + " ORDER BY published_ts DESC,message_id DESC LIMIT ? OFFSET ?", (*values, limit, offset)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["published_at"] = _iso(item.pop("published_ts"))
            edited = item.pop("edited_ts")
            item["edited_at"] = _iso(edited) if edited is not None else None
            result.append(item)
        return result

    def count_posts(self, day=None, now=None, chat_id=None, timezone_name="Asia/Hong_Kong", query="", before=None):
        where, values = self._filters(day, now, chat_id, timezone_name, query, before)
        return self.connection.execute("SELECT COUNT(*) FROM telegram_posts" + where, values).fetchone()[0]

    def get_status(self):
        state = dict(self.connection.execute("SELECT * FROM telegram_state WHERE id=1").fetchone())
        return {key: value for key, value in state.items() if key not in {"id", "lease_owner", "lease_until"}}

    def claim(self, config, owner, now=None):
        current = _now(now).timestamp()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute("SELECT * FROM telegram_state WHERE id=1").fetchone()
            if row["lease_owner"] and (row["lease_until"] or 0) > current:
                self.connection.rollback()
                return False
            if row["chat_id"] is not None and row["chat_id"] != config.chat_id:
                self.connection.execute("DELETE FROM telegram_posts")
            reset = row["bot_id"] != config.bot_id
            reset_status = reset or row["chat_id"] != config.chat_id
            self.connection.execute("""UPDATE telegram_state SET bot_id=?,chat_id=?,lease_owner=?,lease_until=?,
                next_update_id=CASE WHEN ? THEN 0 ELSE next_update_id END,
                last_success_at=CASE WHEN ? THEN NULL ELSE last_success_at END,
                last_error=NULL WHERE id=1""", (config.bot_id, config.chat_id, owner, current + LEASE_SECONDS, reset, reset_status))
            self.connection.commit()
            return True
        except Exception:
            self.connection.rollback()
            raise

    def renew(self, owner):
        with self.connection:
            changed = self.connection.execute("UPDATE telegram_state SET lease_until=? WHERE id=1 AND lease_owner=?", (time.time() + LEASE_SECONDS, owner)).rowcount
        if not changed:
            raise TelegramError("Telegram 接收鎖已由另一個程式接管。")

    def finish(self, owner, error=None, now=None):
        stamp = _now(now).isoformat()
        with self.connection:
            self.connection.execute("""UPDATE telegram_state SET lease_owner=NULL,lease_until=NULL,
                last_poll_at=?,last_error=?,last_success_at=CASE WHEN ? IS NULL THEN ? ELSE last_success_at END
                WHERE id=1 AND lease_owner=?""", (stamp, error, error, stamp, owner))

    def apply_batch(self, updates, config, owner, now=None):
        current = _now(now)
        counts = {"added": 0, "updated": 0, "ignored": 0}
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute("SELECT * FROM telegram_state WHERE id=1").fetchone()
            if row["lease_owner"] != owner:
                raise TelegramError("Telegram 接收鎖已由另一個程式接管。")
            cursor = row["next_update_id"]
            # An invalid update ID must not acknowledge/drop the rest of a batch.
            if any(not isinstance(update, dict) or isinstance(update.get("update_id"), bool) or not isinstance(update.get("update_id"), int) or update["update_id"] < 0 for update in updates):
                raise TelegramError("Telegram 回傳無法解析的更新編號。")
            for update in sorted(updates, key=lambda item: item["update_id"]):
                update_id = update["update_id"]
                if update_id < cursor:
                    continue
                post = _normalize_post(update, config, current)
                if post:
                    old = self.connection.execute("SELECT * FROM telegram_posts WHERE chat_id=? AND message_id=?", (post["chat_id"], post["message_id"])).fetchone()
                    notify = False
                    if old is None:
                        self.connection.execute("""INSERT INTO telegram_posts(chat_id,message_id,title,text,published_ts,edited_ts,media_type,url,links_json)
                            VALUES(:chat_id,:message_id,:title,:text,:published_ts,:edited_ts,:media_type,:url,:links_json)""", post)
                        counts["added"] += 1
                        notify = True
                    elif (post["edited_ts"] or post["published_ts"]) >= (old["edited_ts"] or old["published_ts"]):
                        changed = any(old[key] != post[key] for key in ("title", "text", "edited_ts", "media_type", "url", "links_json"))
                        if changed:
                            # Original publication time is immutable on edits.
                            self.connection.execute("""UPDATE telegram_posts SET title=:title,text=:text,edited_ts=:edited_ts,media_type=:media_type,url=:url,links_json=:links_json
                                WHERE chat_id=:chat_id AND message_id=:message_id""", post)
                            counts["updated"] += 1
                            notify = any(old[key] != post[key] for key in ("title", "text", "media_type", "url", "links_json"))
                    if notify:
                        # Keep the original timestamp on edits, and save the
                        # delivery record atomically with both post and offset.
                        published_ts = old["published_ts"] if old is not None else post["published_ts"]
                        enqueue_notification(self.connection, "telegram", f"{post['chat_id']}:{post['message_id']}", {
                            "title": post["text"].strip().splitlines()[0],
                            "text": post["text"],
                            "summary": "",
                            "published_at": _iso(published_ts),
                            "url": post["url"],
                            "links": json.loads(post["links_json"]),
                            "content_scope": "provided_text",
                        }, is_new=old is None)
                else:
                    counts["ignored"] += 1
                cursor = update_id + 1
            # Saving posts and the next offset is one transaction. Only the next
            # getUpdates request acknowledges this committed batch to Telegram.
            self.connection.execute("UPDATE telegram_state SET next_update_id=? WHERE id=1 AND lease_owner=?", (cursor, owner))
        return counts


def _normalize_post(update, config, now):
    message = update.get("edited_channel_post") or update.get("channel_post")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    if not isinstance(chat, dict) or chat.get("type") != "channel" or chat.get("id") != config.chat_id:
        return None
    message_id, published = message.get("message_id"), message.get("date")
    if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
        return None
    if isinstance(published, bool) or not isinstance(published, (int, float)) or not (now - RETENTION).timestamp() < published <= (now + timedelta(minutes=5)).timestamp():
        return None
    if isinstance(message.get("text"), str):
        text, entities = message.get("text"), message.get("entities")
    else:
        text, entities = message.get("caption"), message.get("caption_entities")
    links = _text_links(text, entities)
    media = next((name for name in ("photo", "video", "document", "audio", "voice", "animation", "sticker", "video_note", "poll", "location") if name in message), None)
    if not isinstance(text, str) or not text.strip():
        if media is None:
            return None
        text = "附件（沒有文字說明）"
        links = []
    edited = message.get("edit_date")
    if isinstance(edited, bool) or not isinstance(edited, (int, float)) or edited < published or edited > (now + timedelta(minutes=5)).timestamp():
        edited = None
    title = chat.get("title")
    return {"chat_id": config.chat_id, "message_id": message_id,
            "title": title if isinstance(title, str) else "Telegram", "text": text,
            "published_ts": published, "edited_ts": edited, "media_type": media,
            "url": f"https://t.me/c/{-config.chat_id - 1000000000000}/{message_id}",
            "links_json": json.dumps(links, ensure_ascii=False, separators=(",", ":"))}


def _text_links(text, entities):
    """Keep only named hyperlinks: [offset, length, url] in UTF-16 units."""
    if not isinstance(text, str) or not isinstance(entities, list):
        return []
    total = len(text.encode("utf-16-le")) // 2
    links = []
    for entity in entities:
        if not isinstance(entity, dict) or entity.get("type") != "text_link":
            continue
        offset, length, url = entity.get("offset"), entity.get("length"), entity.get("url")
        if (isinstance(offset, int) and isinstance(length, int) and not isinstance(offset, bool)
                and not isinstance(length, bool) and offset >= 0 and length > 0
                and offset + length <= total and isinstance(url, str)
                and re.match(r"(?i)^(https?|tg)://", url)):
            links.append([offset, length, url])
    links.sort()
    result, end = [], 0
    for link in links:  # drop overlapping links
        if link[0] >= end:
            result.append(link)
            end = link[0] + link[1]
    return result


def sync_once(config, db_path=DEFAULT_DB_PATH, now=None, client=None, max_batches=10):
    if not isinstance(max_batches, int) or isinstance(max_batches, bool) or not 1 <= max_batches <= 100:
        raise ValueError("max_batches must be between 1 and 100")
    result = {"status": "ok", "added": 0, "updated": 0, "ignored": 0, "pruned": 0, "error": None}
    client = client or TelegramClient(config)
    owner = uuid.uuid4().hex
    with TelegramStore(db_path) as store:
        result["pruned"] = store.prune(now)
        if not store.claim(config, owner):
            result["status"] = "busy"
            return result
        try:
            if client.has_webhook():
                raise TelegramError("這個 Bot 已設定 Webhook；請使用專用 Bot，或先處理原有接收設定。")
            for index in range(max_batches):
                store.renew(owner)
                updates = client.get_updates(store.get_status()["next_update_id"])
                if not isinstance(updates, list) or len(updates) > 100:
                    raise TelegramError("Telegram 回傳無法解析的資料。")
                counts = store.apply_batch(updates, config, owner, now)
                for key, value in counts.items():
                    result[key] += value
                if len(updates) < 100:
                    break
                if index == max_batches - 1:
                    result["status"] = "partial"
        except Exception as exc:
            result["status"] = "error"
            result["error"] = str(exc) if isinstance(exc, TelegramError) else "Telegram 接收或儲存失敗，稍後會重試。"
        finally:
            result["pruned"] += store.prune(now)
            store.finish(owner, result["error"], now)
    return result


def run_worker(config, db_path=DEFAULT_DB_PATH, stop_event=None, poll_seconds=30):
    if not isinstance(poll_seconds, (int, float)) or not 1 <= poll_seconds <= 300:
        raise ValueError("poll_seconds must be between 1 and 300")
    stop_event = stop_event or threading.Event()
    failures = 0
    while not stop_event.is_set():
        try:
            result = sync_once(config, db_path)
        except Exception:
            # Storage errors must not create a thread traceback with request data.
            result = {"status": "error"}
        failures = min(failures + 1, 5) if result["status"] == "error" else 0
        delay = 1 if result["status"] == "partial" else min(300, poll_seconds * (2 ** failures))
        stop_event.wait(delay)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Receive a Telegram channel into a seven-day local inbox")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--worker", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = load_config()
        if config is None:
            raise ValueError("請設定 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID。")
    except ValueError as exc:
        parser.error(str(exc))
    if args.worker:
        try:
            run_worker(config, args.db)
        except KeyboardInterrupt:
            pass
        return 0
    result = sync_once(config, args.db)
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
