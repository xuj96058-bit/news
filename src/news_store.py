"""Persistent feed inbox, exact deduplication, and analysis state (stdlib only)."""

from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "news.sqlite3"
_TRACKING = {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid"}
_ARTICLE_FIELDS = (
    "source_id", "source_name", "source_kind", "region", "title", "url",
    "published_at", "summary", "content", "content_scope", "external_id",
)
_CONTENT_FIELDS = ("title", "summary", "content", "published_at", "content_scope")


def canonicalize_url(url: str) -> str:
    """Remove known tracking parameters, preserving meaningful query arguments."""
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError("Article URL must be an absolute HTTP(S) URL")
    if parts.username is not None or parts.password is not None:
        raise ValueError("Article URL must not contain credentials")
    scheme = parts.scheme.lower()
    host = parts.hostname.lower()
    if ":" in host:  # IPv6 literals need their brackets restored.
        host = f"[{host}]"
    port = parts.port
    if port and (scheme, port) not in {("http", 80), ("https", 443)}:
        host += f":{port}"
    query = [
        (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in _TRACKING
    ]
    return urlunsplit((scheme, host, parts.path or "/", urlencode(query), ""))


def _article_identity(article: dict) -> str:
    """Investat IDs remain stable even when a detail has no public article URL."""
    if article.get("source_id") == "investat":
        external_id = str(article.get("external_id") or "").strip()
        if not external_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", external_id):
            raise ValueError("Investat articles require a stable external_id")
        if article.get("url"):
            canonicalize_url(str(article["url"]))
        return "investat:" + external_id
    return canonicalize_url(str(article.get("url") or ""))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _normalize(value: object) -> str:
    return " ".join(unicodedata.normalize("NFKC", html.unescape(str(value or ""))).casefold().split())


def _publication_time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        try:
            result = parsedate_to_datetime(str(value))
        except (ValueError, TypeError, OverflowError):
            return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _signature(article: dict) -> str:
    return _hash({field: _normalize(article.get(field)) for field in _CONTENT_FIELDS})


def _retain_missing_text(article: dict, previous: dict) -> None:
    """Retain the richest already supplied text if a feed temporarily omits it."""
    if article.get("source_id") == "investat":
        if not article.get("url"):
            article["url"] = previous.get("url")
        incoming = str(article.get("content") or "")
        stored = str(previous.get("content") or "")
        if (article.get("content_scope") == "partial_text" and incoming
                and len(stored) > len(incoming) and stored.startswith(incoming)):
            article["content"] = stored
            article["content_scope"] = previous.get("content_scope")
    if (not article.get("content") and previous.get("content")) or (
        not article.get("content") and not article.get("summary") and previous.get("summary")
    ):
        article["content_scope"] = previous.get("content_scope")
    for field in _CONTENT_FIELDS:
        if not str(article.get(field) or "").strip():
            article[field] = previous.get(field)


def _fingerprint(article: dict) -> str | None:
    title = _normalize(article.get("title"))
    bodies = [_normalize(article.get(field)) for field in ("summary", "content")]
    bodies = [body for body in bodies if body and body != title]
    if not title or not bodies:
        return None  # A repeated headline alone is not evidence of the same event.
    published = _publication_time(article.get("published_at"))
    # The day keeps identically worded periodic releases separate. Invalid dates
    # remain distinct rather than silently being treated as missing dates.
    day = published.date().isoformat() if published else str(article.get("published_at") or "")
    return _hash([title, max(bodies, key=len), day])


class NewsStore:
    """A local inbox. Ingest never calls an AI service or truncates feed content."""

    def __init__(self, path: str | Path = DEFAULT_DB_PATH):
        self.path = Path(path) if str(path) != ":memory:" else path
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path), timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS articles (
                id TEXT PRIMARY KEY,
                canonical_url TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                fingerprint TEXT,
                content_hash TEXT NOT NULL,
                published_sort TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'analyzed', 'error')),
                analysis_json TEXT,
                analyzed_at TEXT,
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS articles_fingerprint ON articles(fingerprint);
            CREATE TABLE IF NOT EXISTS article_sources (
                source_id TEXT NOT NULL,
                canonical_url TEXT NOT NULL,
                article_id TEXT NOT NULL REFERENCES articles(id),
                metadata_json TEXT NOT NULL,
                raw_metadata_json TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                PRIMARY KEY (source_id, canonical_url)
            );
            CREATE INDEX IF NOT EXISTS sources_url ON article_sources(canonical_url);
            CREATE INDEX IF NOT EXISTS sources_article ON article_sources(article_id);
        """)

    def __enter__(self) -> "NewsStore":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def ingest(self, articles) -> dict:
        """Atomically ingest articles; counts classify each input record.

        New source attribution for an unchanged story counts as a duplicate.
        Empty feed fields never erase previously obtained text. A changed source
        record becomes the article's current version; other source versions and
        the complete original incoming metadata remain in article_sources.
        """
        counts = {"added": 0, "updated": 0, "duplicates": 0}
        now = _now()
        with self.connection:
            for raw in articles:
                article = {field: raw.get(field) for field in _ARTICLE_FIELDS}
                if not article["source_id"] or not str(article["title"] or "").strip():
                    raise ValueError("Each article requires source_id and title")
                canonical = _article_identity(article)
                source = self.connection.execute(
                    "SELECT * FROM article_sources WHERE source_id = ? AND canonical_url = ?",
                    (article["source_id"], canonical),
                ).fetchone()
                if source:
                    _retain_missing_text(article, json.loads(source["metadata_json"]))
                signature = _signature(article)
                fingerprint = _fingerprint(article)
                existing = self.connection.execute(
                    "SELECT a.* FROM articles a JOIN article_sources s ON s.article_id = a.id "
                    "WHERE s.canonical_url = ? LIMIT 1", (canonical,),
                ).fetchone()
                if existing is not None and source is None:
                    _retain_missing_text(article, json.loads(existing["metadata_json"]))
                    signature = _signature(article)
                    fingerprint = _fingerprint(article)
                if existing is None and fingerprint and article["source_id"] != "investat":
                    existing = self.connection.execute(
                        "SELECT * FROM articles WHERE fingerprint = ? ORDER BY first_seen, id LIMIT 1",
                        (fingerprint,),
                    ).fetchone()
                published = _publication_time(article.get("published_at"))
                published_sort = published.isoformat() if published else None
                if existing is None:
                    article_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                    self.connection.execute(
                        "INSERT INTO articles (id, canonical_url, metadata_json, fingerprint, "
                        "content_hash, published_sort, first_seen, last_seen, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (article_id, canonical, _json(article), fingerprint, signature,
                         published_sort, now, now, now),
                    )
                    counts["added"] += 1
                else:
                    article_id = existing["id"]
                    old_signature = source["content_hash"] if source else existing["content_hash"]
                    # Newly discovered syndicated copies may have different
                    # excerpt formatting despite an identical body fingerprint.
                    unchanged_copy = not source and fingerprint and fingerprint == existing["fingerprint"]
                    if signature != old_signature and not unchanged_copy:
                        self.connection.execute(
                            "UPDATE articles SET canonical_url = ?, metadata_json = ?, fingerprint = ?, "
                            "content_hash = ?, published_sort = ?, last_seen = ?, updated_at = ?, "
                            "status = 'pending', analysis_json = NULL, analyzed_at = NULL, error = NULL "
                            "WHERE id = ?",
                            (canonical, _json(article), fingerprint, signature, published_sort,
                             now, now, article_id),
                        )
                        counts["updated"] += 1
                    else:
                        if (source is not None and article["source_id"] == "investat"
                                and json.loads(existing["metadata_json"]).get("source_id") == "investat"):
                            # Link/attribution changes do not invalidate unchanged analysis.
                            self.connection.execute(
                                "UPDATE articles SET metadata_json = ?, last_seen = ? WHERE id = ?",
                                (_json(article), now, article_id),
                            )
                        else:
                            self.connection.execute("UPDATE articles SET last_seen = ? WHERE id = ?", (now, article_id))
                        counts["duplicates"] += 1
                self.connection.execute(
                    "INSERT INTO article_sources (source_id, canonical_url, article_id, metadata_json, "
                    "raw_metadata_json, content_hash, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(source_id, canonical_url) DO UPDATE SET "
                    "metadata_json = excluded.metadata_json, raw_metadata_json = excluded.raw_metadata_json, "
                    "content_hash = excluded.content_hash, last_seen = excluded.last_seen",
                    (article["source_id"], canonical, article_id, _json(article), _json(raw), signature, now, now),
                )
        return counts

    def _record(self, row: sqlite3.Row) -> dict:
        article = json.loads(row["metadata_json"])
        article.update({
            "id": row["id"], "article_id": row["id"], "canonical_url": row["canonical_url"],
            "content_hash": row["content_hash"], "first_seen": row["first_seen"],
            "last_seen": row["last_seen"], "updated_at": row["updated_at"],
            "status": row["status"], "error": row["error"], "analyzed_at": row["analyzed_at"],
            "analysis": json.loads(row["analysis_json"]) if row["analysis_json"] else None,
        })
        article["sources"] = []
        for source in self.connection.execute(
            "SELECT * FROM article_sources WHERE article_id = ? ORDER BY source_id, canonical_url", (row["id"],),
        ):
            attribution = json.loads(source["metadata_json"])
            attribution.update({"canonical_url": source["canonical_url"], "first_seen": source["first_seen"],
                                "last_seen": source["last_seen"], "raw_metadata": json.loads(source["raw_metadata_json"])})
            article["sources"].append(attribution)
        return article

    def pending(self, limit: int = 20, *, require_text: bool = False) -> list[dict]:
        """Return unattempted articles before retries, newest in each group first.

        ``require_text`` filters metadata-only entries before applying the limit,
        so a busy feed of headlines cannot starve older analyzable articles.
        Only selected records load source attribution; skipped rows are streamed.
        """
        if limit < 0:
            raise ValueError("limit must be non-negative")
        if limit == 0:
            return []
        query = ("SELECT * FROM articles WHERE status IN ('pending', 'error') "
                 "ORDER BY status = 'error', published_sort IS NULL, published_sort DESC, first_seen DESC, id")
        rows = self.connection.execute(query, ()) if require_text else self.connection.execute(query + " LIMIT ?", (limit,))
        selected = []
        for row in rows:
            if require_text:
                article = json.loads(row["metadata_json"])
                body = article.get("content") or article.get("summary") or ""
                if (article.get("content_scope") in {"title_only", "filing_notice"} or not body.strip()
                        or body.strip() == article["title"].strip()):
                    continue
            selected.append(self._record(row))
            if len(selected) == limit:
                break
        return selected

    def mark_analyzed(self, article_id: str, analysis: dict, *, expected_content_hash: str | None = None) -> None:
        """Save a result; optional hash prevents a concurrent edit getting a stale result."""
        if not isinstance(analysis, dict):
            raise TypeError("analysis must be a dictionary")
        with self.connection:
            row = self.connection.execute("SELECT content_hash FROM articles WHERE id = ?", (article_id,)).fetchone()
            if row is None:
                raise KeyError(article_id)
            if expected_content_hash is not None and expected_content_hash != row["content_hash"]:
                raise ValueError("Article changed during analysis; result was not saved")
            result = self.connection.execute(
                "UPDATE articles SET status = 'analyzed', analysis_json = ?, analyzed_at = ?, error = NULL "
                "WHERE id = ? AND (? IS NULL OR content_hash = ?)",
                (_json(analysis), _now(), article_id, expected_content_hash, expected_content_hash),
            )
            if not result.rowcount:
                raise ValueError("Article changed during analysis; result was not saved")

    def mark_error(self, article_id: str, safe_error: str) -> None:
        """Keep the article retryable. Pass a safe error category, never a raw API exception."""
        # Defense in depth for familiar credential formats. Callers should still
        # supply a short, non-sensitive category such as 'TimeoutError'.
        safe_error = re.sub(r"(?i)\b(?:sk-[A-Za-z0-9_-]+|bearer\s+\S+)", "[redacted]", str(safe_error))
        safe_error = re.sub(r"(?i)((?:api[_-]?key|authorization|token)\s*[:=]\s*)[^\s,;]+", r"\1[redacted]", safe_error)
        with self.connection:
            result = self.connection.execute(
                "UPDATE articles SET status = 'error', error = ?, analysis_json = NULL, analyzed_at = NULL WHERE id = ?",
                (safe_error[:500], article_id),
            )
            if not result.rowcount:
                raise KeyError(article_id)

    def export_articles(self) -> list[dict]:
        rows = self.connection.execute(
            "SELECT * FROM articles ORDER BY published_sort IS NULL, published_sort DESC, first_seen DESC, id",
        ).fetchall()
        return [self._record(row) for row in rows]

    def stats(self) -> dict:
        result = {"total": 0, "pending": 0, "analyzed": 0, "error": 0, "attributions": 0}
        for row in self.connection.execute("SELECT status, COUNT(*) AS count FROM articles GROUP BY status"):
            result[row["status"]] = row["count"]
            result["total"] += row["count"]
        result["attributions"] = self.connection.execute("SELECT COUNT(*) FROM article_sources").fetchone()[0]
        return result

