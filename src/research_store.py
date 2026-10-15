"""Local research reports with immutable originals and editable revision history."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4


DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "research_reports.db"
# Deliberately store report facts, not client settings or provider request arguments.
METADATA_FIELDS = frozenset({
    "article_count", "source_count", "warnings", "limitations", "coverage",
    "selection", "generated_at", "duration_seconds", "usage", "report_scope", "review_warnings", "citation_repaired",
    "workflow_stage", "review_status", "structured_draft", "review_findings", "review_summary",
    "review_error", "parent_report_id", "reviewed_version", "workflow_schema_version", "validation_warnings",
    "research_brief", "report_mode", "quality_status",
    "writing_style", "argument_plan", "quality", "retrieval_outcomes", "api_call_count",
    "daily_selection", "input_sources", "manually_verified",
    "review_error_reason", "review_history",
    "data_preparation",
    "deep_research", "research_packet", "research_budget", "research_execution",
    "answer_status", "claim_audit", "restoration",
    "report_has_body", "failure_stage", "failure_reason", "preparation_warnings",
})
_PRIVATE_FIELDS = frozenset({
    "key", "apikey", "authorization", "proxyauthorization", "auth", "authkey",
    "authentication", "credentials", "credential", "cookie", "cookies",
    "setcookie", "headers", "requestheaders", "responseheaders", "password",
    "passwd", "secret", "secrets", "secretkey", "privatekey", "privatekeypem", "clientsecret", "token",
    "bearer", "bearertoken", "accesstoken", "refreshtoken", "sessiontoken",
    "connectionstring", "request", "requestparams", "requestparameters",
    "requestpayload", "requestbody", "providerparams", "providerparameters",
    "parameters", "params",
})


class ResearchStoreError(Exception):
    """An actionable, non-sensitive local report storage error."""


class ResearchConflictError(ResearchStoreError):
    """The saved report changed since the editor loaded it."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _private_field(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    return (normalized in _PRIVATE_FIELDS or normalized.endswith(
        ("apikey", "password", "passwd", "secret", "credentials", "credential",
         "authorization", "token", "accesskey", "secretkey", "privatekey", "privatekeypem")
    ))


def _private_query_field(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    return _private_field(key) or normalized in {"pass", "sig"} or normalized.endswith("signature")


def _safe_url(value: str) -> str:
    """Sanitize standalone snapshot URLs without rewriting source prose."""
    if not re.fullmatch(r"https?://[^\s<>\"'\\]+", value, re.I):
        return value
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
            return value
        original_query = parse_qsl(parts.query, keep_blank_values=True)
        query = [(key, val) for key, val in original_query if not _private_query_field(key)]
        removed_query_credentials = len(query) != len(original_query)
        if (parts.username is None and parts.password is None
                and not removed_query_credentials and not parts.fragment):
            return value
        netloc = parts.netloc.rsplit("@", 1)[-1]
        # A fragment is never fetched evidence and may carry a session token.
        clean_query = urlencode(query) if removed_query_credentials else parts.query
        return urlunsplit((parts.scheme, netloc, parts.path, clean_query, ""))
    except ValueError:
        raise ResearchStoreError("Source snapshot contains an invalid URL.") from None


def _clean_json(value: object) -> object:
    """Copy JSON values, stripping credential fields at every nesting level."""
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ResearchStoreError("Report snapshots require text field names.")
        return {key: _clean_json(item) for key, item in value.items() if not _private_field(key)}
    if isinstance(value, (list, tuple)):
        return [_clean_json(item) for item in value]
    if isinstance(value, str):
        return _safe_url(value)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    raise ResearchStoreError("Report snapshots must contain JSON-compatible values.")


def _json(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise ResearchStoreError("Report snapshots must contain valid JSON values.") from None


def _text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ResearchStoreError(f"{label} must be text.")
    return value


def _version(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ResearchStoreError("Expected version must be a positive integer.")
    return value


class ResearchStore:
    """A separate SQLite report library. All edits append a revision immediately.

    The generated title/body and original news snapshot are fixed at creation.
    Each revision records its own sources and metadata; retrieval may add sources
    to the current report without rewriting any earlier research snapshot.
    A user may edit the latest title/body freely without any AI approval or lock.
    Optimistic concurrency only prevents an older editor from losing newer edits.
    """

    def __init__(self, path: str | Path | None = None):
        path = DEFAULT_DB_PATH if path is None else path
        self.path = Path(path) if str(path) != ":memory:" else path
        try:
            if str(path) != ":memory:":
                self.path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(str(path), timeout=30)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.executescript("""
                CREATE TABLE IF NOT EXISTS research_reports (
                    id TEXT PRIMARY KEY,
                    topic TEXT NOT NULL,
                    original_title TEXT NOT NULL,
                    original_body TEXT NOT NULL,
                    sources_json TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    current_version INTEGER NOT NULL CHECK (current_version >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS research_revisions (
                    id TEXT PRIMARY KEY,
                    report_id TEXT NOT NULL REFERENCES research_reports(id),
                    version INTEGER NOT NULL CHECK (version >= 1),
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    note TEXT NOT NULL,
                    restored_from_revision_id TEXT REFERENCES research_revisions(id),
                    created_at TEXT NOT NULL,
                    UNIQUE (report_id, version)
                );
                CREATE TABLE IF NOT EXISTS research_snapshots (
                    report_id TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK (version >= 1),
                    sources_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    snapshot_status TEXT NOT NULL CHECK (snapshot_status IN
                        ('captured', 'legacy_current', 'legacy_sources_only')),
                    captured_at TEXT NOT NULL,
                    PRIMARY KEY (report_id, version),
                    FOREIGN KEY (report_id, version) REFERENCES research_revisions(report_id, version)
                );
                CREATE INDEX IF NOT EXISTS reports_updated ON research_reports(updated_at DESC);
                CREATE INDEX IF NOT EXISTS revisions_report ON research_revisions(report_id, version DESC);
            """)
        except (sqlite3.Error, OSError):
            if hasattr(self, "connection"):
                self.connection.close()
            raise ResearchStoreError("Could not open the local research report library.") from None

    def __enter__(self) -> "ResearchStore":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    @staticmethod
    def _record(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"], "topic": row["topic"], "title": row["title"],
            "body": row["body"], "version": row["current_version"],
            "revision_id": row["revision_id"], "original_title": row["original_title"],
            "original_body": row["original_body"], "sources": json.loads(row["sources_json"]),
            "original_sources": json.loads(row["original_sources_json"] or row["sources_json"]),
            "provider": row["provider"], "model": row["model"], "as_of": row["as_of"],
            "metadata": json.loads(row["metadata_json"]),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    _REPORT_QUERY = (
        "SELECT r.*, v.id AS revision_id, v.title, v.body, s.sources_json AS original_sources_json "
        "FROM research_reports r JOIN research_revisions v "
        "ON v.report_id = r.id AND v.version = r.current_version "
        "LEFT JOIN research_snapshots s ON s.report_id = r.id AND s.version = 1 "
    )

    def _insert_snapshot(self, report_id, version, sources_json, metadata_json, now, *, status="captured"):
        """Call only inside the revision transaction, after inserting that revision."""
        self.connection.execute(
            "INSERT INTO research_snapshots VALUES (?, ?, ?, ?, ?, ?)",
            (report_id, version, sources_json, metadata_json, status, now),
        )

    def _preserve_legacy_snapshots(self, report_id, version, row, now):
        """Capture the old current state before mutation, without inventing history.

        Before per-version snapshots existed, sources were immutable but metadata
        was overwritten on review. Old sources remain recoverable; old metadata
        is deliberately empty unless this is the version currently being saved.
        The caller has already acquired the optimistic write lock.
        """
        sources_json = _json(_clean_json(json.loads(row["sources_json"])))
        metadata_json = _json(_clean_json({key: value for key, value in json.loads(row["metadata_json"]).items()
                                          if key in METADATA_FIELDS}))
        for historical_version in sorted({1, version}):
            exists = self.connection.execute(
                "SELECT 1 FROM research_snapshots WHERE report_id = ? AND version = ?",
                (report_id, historical_version),
            ).fetchone()
            if not exists:
                current = historical_version == version
                self._insert_snapshot(report_id, historical_version, sources_json,
                                      metadata_json if current else "{}", now,
                                      status="legacy_current" if current else "legacy_sources_only")
        return sources_json, metadata_json

    def create_report(self, *, topic: str, title: str, body: str, sources: list[dict],
                      provider: str, model: str, as_of: str, metadata: dict | None = None) -> dict:
        """Save generation output and a detached, credential-free news snapshot."""
        topic, title, body = _text(topic, "Topic"), _text(title, "Title"), _text(body, "Body")
        provider, model, as_of = (_text(provider, "Provider"), _text(model, "Model"), _text(as_of, "As-of"))
        if not isinstance(sources, list) or not all(isinstance(source, dict) for source in sources):
            raise ResearchStoreError("Sources must be a list of news snapshots.")
        if metadata is not None and not isinstance(metadata, dict):
            raise ResearchStoreError("Report metadata must be a dictionary.")
        try:
            sources_json = _json(_clean_json(sources))
            metadata_json = _json(_clean_json({key: value for key, value in (metadata or {}).items()
                                             if key in METADATA_FIELDS}))
        except RecursionError:
            raise ResearchStoreError("Report snapshots contain too many nested fields.") from None
        report_id, revision_id, now = str(uuid4()), str(uuid4()), _now()
        try:
            with self.connection:
                self.connection.execute(
                    "INSERT INTO research_reports VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (report_id, topic, title, body, sources_json, provider, model, as_of,
                     metadata_json, 1, now, now),
                )
                self.connection.execute(
                    "INSERT INTO research_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (revision_id, report_id, 1, title, body, "Initial generated report", None, now),
                )
                self._insert_snapshot(report_id, 1, sources_json, metadata_json, now)
        except sqlite3.Error:
            raise ResearchStoreError("Could not save the research report.") from None
        return self.get_report(report_id)

    def list_reports(self, limit: int = 100) -> list[dict]:
        """Return latest contents in descending update order."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ResearchStoreError("Report limit must be a non-negative integer.")
        try:
            rows = self.connection.execute(
                self._REPORT_QUERY + "ORDER BY r.updated_at DESC, v.rowid DESC, r.id LIMIT ?", (limit,),
            ).fetchall()
            return [self._record(row) for row in rows]
        except sqlite3.Error:
            raise ResearchStoreError("Could not read the research report library.") from None

    def get_report(self, report_id: str) -> dict | None:
        report_id = _text(report_id, "Report ID")
        try:
            row = self.connection.execute(self._REPORT_QUERY + "WHERE r.id = ?", (report_id,)).fetchone()
            return self._record(row) if row else None
        except sqlite3.Error:
            raise ResearchStoreError("Could not read the research report.") from None

    def _append_revision(self, report_id: str, *, title: str, body: str, expected_version: int,
                         note: str, restored_from_revision_id: str | None = None) -> dict:
        report_id, title, body = (_text(report_id, "Report ID"), _text(title, "Title"), _text(body, "Body"))
        note, expected_version = _text(note, "Revision note"), _version(expected_version)
        now = _now()
        try:
            with self.connection:
                result = self.connection.execute(
                    "UPDATE research_reports SET current_version = current_version + 1, updated_at = ? "
                    "WHERE id = ? AND current_version = ?", (now, report_id, expected_version),
                )
                if not result.rowcount:
                    exists = self.connection.execute("SELECT 1 FROM research_reports WHERE id = ?", (report_id,)).fetchone()
                    if exists is None:
                        raise ResearchStoreError("Research report was not found.")
                    raise ResearchConflictError("This report has newer edits. Reload it before saving.")
                row = self.connection.execute(
                    "SELECT sources_json, metadata_json FROM research_reports WHERE id = ?", (report_id,),
                ).fetchone()
                sources_json, metadata_json = self._preserve_legacy_snapshots(report_id, expected_version, row, now)
                self.connection.execute(
                    "INSERT INTO research_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid4()), report_id, expected_version + 1, title, body, note,
                     restored_from_revision_id, now),
                )
                self._insert_snapshot(report_id, expected_version + 1,
                                      sources_json, metadata_json, now)
        except sqlite3.Error:
            raise ResearchStoreError("Could not save the report revision.") from None
        return self.get_report(report_id)

    def save_revision(self, report_id: str, *, title: str, body: str,
                      expected_version: int, note: str = "") -> dict:
        """Append a human edit, rejecting a stale editor without losing either draft."""
        return self._append_revision(report_id, title=title, body=body,
                                     expected_version=expected_version, note=note)

    def save_review(self, report_id: str, *, title: str, body: str, expected_version: int,
                    metadata: dict, note: str = "AI independent review", sources: list[dict] | None = None) -> dict:
        """Append review and its safe workflow facts atomically, preserving human edits.

        A reviewer works from a specific saved draft. If its author has edited that
        draft while review was running, no review text or metadata is written. The
        caller may save the review separately rather than replace the author's work.
        Optional sources are the exact source snapshot used by this new revision;
        omitting them preserves the current sources. Previous snapshots stay fixed.
        """
        report_id, title, body = (_text(report_id, "Report ID"), _text(title, "Title"), _text(body, "Body"))
        note, expected_version = _text(note, "Revision note"), _version(expected_version)
        if not isinstance(metadata, dict):
            raise ResearchStoreError("Report metadata must be a dictionary.")
        if sources is not None and (not isinstance(sources, list) or not all(isinstance(source, dict) for source in sources)):
            raise ResearchStoreError("Sources must be a list of news snapshots.")
        try:
            safe_metadata = _clean_json({key: value for key, value in metadata.items() if key in METADATA_FIELDS})
            sources_json = _json(_clean_json(sources)) if sources is not None else None
            # Validate before taking a write lock. This also rejects NaN and cycles.
            _json(safe_metadata)
        except RecursionError:
            raise ResearchStoreError("Report snapshots contain too many nested fields.") from None
        now = _now()
        try:
            with self.connection:
                result = self.connection.execute(
                    "UPDATE research_reports SET current_version = current_version + 1, updated_at = ? "
                    "WHERE id = ? AND current_version = ?", (now, report_id, expected_version),
                )
                if not result.rowcount:
                    exists = self.connection.execute("SELECT 1 FROM research_reports WHERE id = ?", (report_id,)).fetchone()
                    if exists is None:
                        raise ResearchStoreError("Research report was not found.")
                    raise ResearchConflictError("This report has newer edits. Reload it before saving.")
                row = self.connection.execute(
                    "SELECT sources_json, metadata_json FROM research_reports WHERE id = ?", (report_id,),
                ).fetchone()
                self._preserve_legacy_snapshots(report_id, expected_version, row, now)
                merged_metadata = json.loads(row["metadata_json"])
                merged_metadata.update(safe_metadata)
                if merged_metadata.get("review_status") in ("reviewed", "needs_review"):
                    merged_metadata["reviewed_version"] = expected_version + 1
                else:
                    merged_metadata.pop("reviewed_version", None)
                metadata_json = _json(_clean_json({key: value for key, value in merged_metadata.items()
                                                   if key in METADATA_FIELDS}))
                if sources_json is None:
                    sources_json = _json(_clean_json(json.loads(row["sources_json"])))
                self.connection.execute(
                    "UPDATE research_reports SET metadata_json = ?, sources_json = ? WHERE id = ?",
                    (metadata_json, sources_json, report_id),
                )
                self.connection.execute(
                    "INSERT INTO research_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid4()), report_id, expected_version + 1, title, body, note, None, now),
                )
                self._insert_snapshot(report_id, expected_version + 1, sources_json, metadata_json, now)
        except sqlite3.Error:
            raise ResearchStoreError("Could not save the report review.") from None
        return self.get_report(report_id)

    def get_research_snapshot(self, report_id: str, version: int) -> dict | None:
        """Return a detached version-bound research record, or None if absent.

        Legacy reports have no historical metadata. Their fallback is explicitly
        labelled and never attaches the latest review to an older body. Before
        enrichment, legacy sources were immutable; the original-source baseline
        remains available after the first write by this schema.
        """
        report_id, version = _text(report_id, "Report ID"), _version(version)
        try:
            row = self.connection.execute(
                "SELECT v.*, r.current_version, r.sources_json AS current_sources_json, "
                "r.metadata_json AS current_metadata_json, s.sources_json, s.metadata_json, "
                "s.snapshot_status, s.captured_at, original.sources_json AS original_sources_json "
                "FROM research_revisions v JOIN research_reports r ON r.id = v.report_id "
                "LEFT JOIN research_snapshots s ON s.report_id = v.report_id AND s.version = v.version "
                "LEFT JOIN research_snapshots original ON original.report_id = v.report_id AND original.version = 1 "
                "WHERE v.report_id = ? AND v.version = ?", (report_id, version),
            ).fetchone()
            if row is None:
                return None
            available = row["snapshot_status"] is not None
            current = version == row["current_version"]
            sources_json = row["sources_json"] if available else (
                row["original_sources_json"] or row["current_sources_json"])
            metadata_json = row["metadata_json"] if available else (row["current_metadata_json"] if current else "{}")
            metadata = _clean_json({key: value for key, value in json.loads(metadata_json).items() if key in METADATA_FIELDS})
            return {"report_id": report_id, "version": version, "revision_id": row["id"],
                    "title": row["title"], "body": row["body"], "note": row["note"],
                    "sources": _clean_json(json.loads(sources_json)), "metadata": metadata,
                    "snapshot_status": row["snapshot_status"] if available else "legacy_fallback",
                    "historical_metadata_available": row["snapshot_status"] != "legacy_sources_only" if available else current,
                    "created_at": row["created_at"], "captured_at": row["captured_at"]}
        except sqlite3.Error:
            raise ResearchStoreError("Could not read the research snapshot.") from None

    def list_research_snapshots(self, report_id: str) -> list[dict]:
        """Return every saved revision's research snapshot, newest first."""
        report_id = _text(report_id, "Report ID")
        return [self.get_research_snapshot(report_id, row["version"])
                for row in self.list_revisions(report_id)]

    def list_revisions(self, report_id: str) -> list[dict]:
        report_id = _text(report_id, "Report ID")
        try:
            rows = self.connection.execute(
                "SELECT * FROM research_revisions WHERE report_id = ? ORDER BY version DESC", (report_id,),
            ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error:
            raise ResearchStoreError("Could not read the report revision history.") from None

    def restore_revision(self, report_id: str, revision_id: str, *, expected_version: int) -> dict:
        """Restore a body with its own research snapshot in one locked revision.

        A restoration is not a new review. Its review-version marker remains the
        target snapshot's marker; absent historical research metadata must never
        inherit a newer version's evidence or audit.
        """
        report_id, revision_id = _text(report_id, "Report ID"), _text(revision_id, "Revision ID")
        expected_version = _version(expected_version)
        now = _now()
        try:
            with self.connection:
                revision = self.connection.execute(
                    "SELECT * FROM research_revisions WHERE id = ? AND report_id = ?", (revision_id, report_id),
                ).fetchone()
                if revision is None:
                    raise ResearchStoreError("Report revision was not found.")
                result = self.connection.execute(
                    "UPDATE research_reports SET current_version = current_version + 1, updated_at = ? "
                    "WHERE id = ? AND current_version = ?", (now, report_id, expected_version),
                )
                if not result.rowcount:
                    raise ResearchConflictError("This report has newer edits. Reload it before restoring.")
                current = self.connection.execute(
                    "SELECT sources_json, metadata_json FROM research_reports WHERE id = ?", (report_id,),
                ).fetchone()
                self._preserve_legacy_snapshots(report_id, expected_version, current, now)
                target = self.get_research_snapshot(report_id, revision["version"])
                if target is None:
                    raise ResearchStoreError("Report research snapshot was not found.")
                historical_metadata = target["historical_metadata_available"]
                if historical_metadata:
                    metadata = target["metadata"]
                    reviewed = metadata.get("reviewed_version")
                    if metadata.get("review_status") in {"reviewed", "needs_review"} and (
                            isinstance(reviewed, bool) or not isinstance(reviewed, int)
                            or not 1 <= reviewed <= revision["version"]):
                        metadata.pop("reviewed_version", None)
                        metadata.update(review_status="needs_review", quality_status="needs_review",
                            review_error="歷史審查未記錄有效的版本編號；還原不代表新版本已通過審查。")
                else:
                    metadata = {"workflow_stage": "restored_without_research_metadata",
                        "review_status": "needs_review", "quality_status": "needs_review",
                        "manually_verified": False,
                        "review_error": "此舊版本未保存歷史研究資料與審查紀錄；未套用最新版本的審查，還原稿需重新核對。"}
                metadata["restoration"] = {"from_version": revision["version"],
                    "from_revision_id": revision_id, "snapshot_status": target["snapshot_status"],
                    "historical_metadata_available": historical_metadata}
                sources_json = _json(_clean_json(target["sources"]))
                metadata_json = _json(_clean_json({key: value for key, value in metadata.items() if key in METADATA_FIELDS}))
                self.connection.execute(
                    "UPDATE research_reports SET sources_json = ?, metadata_json = ? WHERE id = ?",
                    (sources_json, metadata_json, report_id),
                )
                self.connection.execute(
                    "INSERT INTO research_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid4()), report_id, expected_version + 1, revision["title"], revision["body"],
                     f"Restored version {revision['version']}", revision_id, now),
                )
                self._insert_snapshot(report_id, expected_version + 1, sources_json, metadata_json, now)
        except sqlite3.Error:
            raise ResearchStoreError("Could not restore the report revision.") from None
        return self.get_report(report_id)
