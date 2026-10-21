"""Durable event judgements and source snapshots in the local news database.

One revision represents one set of event inputs. A reviewer may complete that
revision, but its initial draft remains available separately. New inputs always
create a new revision; ordinary reads never start model or network requests.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from . import news_store
from .research_store import ResearchStoreError, _clean_json


MAX_LIST_LIMIT = 100
MAX_SNAPSHOT_CHARS = 500_000
MAX_SOURCES = 50
MAX_ARTICLE_IDS = 50


class EventStoreError(Exception):
    """A non-sensitive local event storage or validation error."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: object, *, maximum: int = 128) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= maximum:
        raise EventStoreError("事件欄位必須是長度合適的非空文字。")
    return value.strip()


def _limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EventStoreError("筆數必須是非負整數。")
    return min(value, MAX_LIST_LIMIT)


def _snapshot(value: object) -> str:
    try:
        result = json.dumps(_clean_json(value), ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError, ResearchStoreError):
        raise EventStoreError("事件快照必須是有效的 JSON 資料。") from None
    if len(result) > MAX_SNAPSHOT_CHARS:
        raise EventStoreError("事件快照超過儲存大小上限。")
    return result


def _judgement(brief: dict, metadata: dict) -> tuple[str, str]:
    if not isinstance(brief, dict) or not isinstance(metadata, dict):
        raise EventStoreError("研判與附加資料必須是物件。")
    return _snapshot(brief), _snapshot(metadata)


class EventStore:
    """Additive event tables sharing NewsStore's default SQLite location."""

    def __init__(self, path: str | Path | None = None):
        path = news_store.DEFAULT_DB_PATH if path is None else path
        self.path = Path(path) if str(path) != ":memory:" else path
        connection = None
        try:
            if str(path) != ":memory:":
                self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(path), timeout=30)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS news_events (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS event_revisions (
                    id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL REFERENCES news_events(id),
                    version INTEGER NOT NULL CHECK (version > 0),
                    input_hash TEXT NOT NULL,
                    article_ids_json TEXT NOT NULL,
                    sources_json TEXT NOT NULL,
                    brief_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    original_brief_json TEXT NOT NULL,
                    original_metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (event_id, input_hash),
                    UNIQUE (event_id, version)
                );
                CREATE INDEX IF NOT EXISTS events_updated
                    ON news_events(updated_at DESC);
                CREATE INDEX IF NOT EXISTS event_revision_order
                    ON event_revisions(event_id, version DESC);
            """)
        except (sqlite3.Error, OSError):
            if connection is not None:
                connection.close()
            raise EventStoreError("無法開啟事件資料庫，請檢查資料目錄。") from None
        self.connection = connection

    def __enter__(self) -> "EventStore":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    @staticmethod
    def _revision(row: sqlite3.Row) -> dict:
        record = {name: row[name] for name in (
            "id", "event_id", "version", "input_hash", "created_at", "updated_at")}
        for field in ("article_ids", "sources", "brief", "metadata", "original_brief", "original_metadata"):
            record[field] = json.loads(row[field + "_json"])
        return record

    def create_event(self, title: str) -> dict:
        title = _text(title, maximum=300)
        identifier, now = str(uuid4()), _now()
        record = {"id": identifier, "title": title, "created_at": now, "updated_at": now}
        try:
            with self.connection:
                self.connection.execute("INSERT INTO news_events VALUES (?, ?, ?, ?)",
                                        (identifier, title, now, now))
        except sqlite3.Error:
            raise EventStoreError("無法儲存事件。") from None
        return record

    def list_events(self, limit: int = 50) -> list[dict]:
        limit = _limit(limit)
        try:
            rows = self.connection.execute(
                "SELECT * FROM news_events ORDER BY updated_at DESC, rowid DESC LIMIT ?",
                (limit,)).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error:
            raise EventStoreError("無法讀取事件列表。") from None

    def get_event(self, event_id: str) -> dict | None:
        identifier = _text(event_id)
        try:
            row = self.connection.execute("SELECT * FROM news_events WHERE id = ?", (identifier,)).fetchone()
            return dict(row) if row else None
        except sqlite3.Error:
            raise EventStoreError("無法讀取事件。") from None

    def list_revisions(self, event_id: str, limit: int = 5) -> list[dict]:
        identifier, limit = _text(event_id), _limit(limit)
        try:
            rows = self.connection.execute(
                "SELECT * FROM event_revisions WHERE event_id = ? ORDER BY version DESC LIMIT ?",
                (identifier, limit)).fetchall()
            return [self._revision(row) for row in rows]
        except (sqlite3.Error, ValueError, KeyError):
            raise EventStoreError("無法讀取事件研判紀錄。") from None

    def find_revision(self, event_id: str, input_hash: str) -> dict | None:
        identifier, digest = _text(event_id), _text(input_hash)
        try:
            row = self.connection.execute(
                "SELECT * FROM event_revisions WHERE event_id = ? AND input_hash = ?",
                (identifier, digest)).fetchone()
            return self._revision(row) if row else None
        except (sqlite3.Error, ValueError, KeyError):
            raise EventStoreError("無法讀取事件研判紀錄。") from None

    def create_revision(self, event_id: str, *, article_ids: list[str], sources: list[dict],
                        brief: dict, metadata: dict, input_hash: str) -> dict:
        identifier, digest = _text(event_id), _text(input_hash)
        if (not isinstance(article_ids, list) or len(article_ids) > MAX_ARTICLE_IDS
                or any(not isinstance(item, str) or not 1 <= len(item) <= 128 for item in article_ids)):
            raise EventStoreError("新聞識別碼必須是有長度限制的文字列表。")
        if (not isinstance(sources, list) or len(sources) > MAX_SOURCES
                or any(not isinstance(source, dict) for source in sources)):
            raise EventStoreError("來源必須是有筆數限制的快照列表。")
        brief_json, metadata_json = _judgement(brief, metadata)
        article_json = _snapshot(list(dict.fromkeys(article_ids)))
        sources_json = _snapshot(sources)
        revision_id, now = str(uuid4()), _now()
        try:
            with self.connection:
                # Allocate version and write both tables under one lock. This
                # also deduplicates concurrent submissions of identical inputs.
                self.connection.execute("BEGIN IMMEDIATE")
                if self.connection.execute("SELECT id FROM news_events WHERE id = ?", (identifier,)).fetchone() is None:
                    raise EventStoreError("事件不存在，請先建立或選擇事件。")
                existing = self.connection.execute(
                    "SELECT * FROM event_revisions WHERE event_id = ? AND input_hash = ?",
                    (identifier, digest)).fetchone()
                if existing is not None:
                    return self._revision(existing)
                version = self.connection.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM event_revisions WHERE event_id = ?",
                    (identifier,)).fetchone()[0]
                self.connection.execute("""
                    INSERT INTO event_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (revision_id, identifier, version, digest, article_json, sources_json,
                      brief_json, metadata_json, brief_json, metadata_json, now, now))
                self.connection.execute("UPDATE news_events SET updated_at = ? WHERE id = ?", (now, identifier))
                row = self.connection.execute("SELECT * FROM event_revisions WHERE id = ?", (revision_id,)).fetchone()
                result = self._revision(row)
            return result
        except (sqlite3.Error, ValueError, KeyError):
            raise EventStoreError("無法儲存事件研判，原有紀錄未變更。") from None

    def update_revision(self, revision_id: str, *, brief: dict, metadata: dict) -> dict:
        """Complete a saved draft without replacing its source or original text."""
        identifier = _text(revision_id)
        brief_json, metadata_json = _judgement(brief, metadata)
        now = _now()
        try:
            with self.connection:
                self.connection.execute("BEGIN IMMEDIATE")
                row = self.connection.execute("SELECT * FROM event_revisions WHERE id = ?", (identifier,)).fetchone()
                if row is None:
                    raise EventStoreError("研判紀錄不存在。")
                self.connection.execute(
                    "UPDATE event_revisions SET brief_json = ?, metadata_json = ?, updated_at = ? WHERE id = ?",
                    (brief_json, metadata_json, now, identifier))
                self.connection.execute("UPDATE news_events SET updated_at = ? WHERE id = ?", (now, row["event_id"]))
                result = self._revision(self.connection.execute(
                    "SELECT * FROM event_revisions WHERE id = ?", (identifier,)).fetchone())
            return result
        except (sqlite3.Error, ValueError, KeyError):
            raise EventStoreError("無法更新事件研判，原有紀錄未變更。") from None
