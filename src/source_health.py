"""Append-only collection attempts and honest rolling source health metrics."""

from datetime import datetime, timedelta, timezone
import json
import re


def _now(value=None):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    value = value or datetime.now(timezone.utc)
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc))


def _schema(store):
    store.connection.execute("""CREATE TABLE IF NOT EXISTS collection_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL,
        source_name TEXT NOT NULL, run_at TEXT NOT NULL, status TEXT NOT NULL,
        fetched INTEGER NOT NULL, selected INTEGER NOT NULL, added INTEGER NOT NULL,
        updated INTEGER NOT NULL, duplicates INTEGER NOT NULL, usable_text INTEGER NOT NULL,
        limited INTEGER NOT NULL, older_skipped INTEGER NOT NULL, gap INTEGER NOT NULL,
        error TEXT, details_json TEXT NOT NULL)""")
    store.connection.execute("CREATE INDEX IF NOT EXISTS collection_runs_source_time ON collection_runs(source_id,run_at)")
    store.connection.commit()


def usable_text_count(items):
    """Count usable supplied prose; metadata and document links are not bodies."""
    return sum(1 for item in items
               if item.get("content_scope") not in {"title_only", "document_link", "announcement_metadata", "notice_only", "filing_notice"}
               and any(str(item.get(key) or "").strip() and str(item.get(key) or "").strip() != str(item.get("title") or "").strip()
                       for key in ("content", "summary")))


def safe_collection_error(value):
    """Retain only status codes and error classes, never arbitrary API text."""
    value = str(value or "")
    match = re.search(r"\bHTTP ([1-5][0-9]{2})\b", value)
    if match:
        return "HTTP " + match[1]
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,60}(?:Error|Exception)", value):
        return value
    return "Collection error (details omitted)" if value else None


def record_collection(store, outcome, run_at=None):
    """Record a sanitized per-source outcome, never request options or credentials.

    Success means a complete response (including an explicitly empty response),
    while partial runs retain their counts and are reported separately.
    """
    _schema(store)
    source_id = str(outcome.get("source_id") or outcome.get("id") or "").strip()
    if not source_id:
        raise ValueError("Collection outcome requires a source ID")
    status = outcome.get("status", "error")
    if status not in {"ok", "empty", "error", "partial"}:
        raise ValueError("Unknown collection status")
    def count(key):
        return max(0, int(outcome.get(key) or 0))
    selected = count("selected")
    gap = bool(outcome.get("gap") or count("limited") or count("upstream_limited") or status == "partial")
    # Allowlist keeps arbitrary API responses, URLs and options out of history.
    details = {key: outcome[key] for key in ("coverage", "available", "upstream_limited", "window_exhausted", "pages", "window_from", "window_to") if key in outcome}
    error = safe_collection_error(outcome.get("error"))
    with store.connection:
        cursor = store.connection.execute("""INSERT INTO collection_runs
            (source_id,source_name,run_at,status,fetched,selected,added,updated,duplicates,
             usable_text,limited,older_skipped,gap,error,details_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (source_id, str(outcome.get("name") or outcome.get("source_name") or source_id),
             _now(run_at).isoformat(), status, count("fetched"), selected, count("added"), count("updated"),
             count("duplicates"), min(selected, count("usable_text")), count("limited") + count("upstream_limited"),
             count("older_skipped"), int(gap), error, json.dumps(details, ensure_ascii=False)))
    return cursor.lastrowid


def health_summary(store, days=30, source_ids=None, *, now=None):
    """Return 30-day counts and all-time last success; unseen sources stay unknown.

    usable_text_ratio measures selected observations (including repeat runs),
    not unique articles. New-article totals use actual ingest results.
    """
    if not isinstance(days, int) or days < 1:
        raise ValueError("days must be a positive integer")
    _schema(store)
    current = _now(now)
    start = (current - timedelta(days=days)).isoformat()
    ids = list(dict.fromkeys(source_ids)) if source_ids is not None else [row[0] for row in store.connection.execute("SELECT DISTINCT source_id FROM collection_runs ORDER BY source_id")]
    result = []
    for source_id in ids:
        rows = store.connection.execute("SELECT * FROM collection_runs WHERE source_id=? AND run_at>=? AND run_at<=? ORDER BY run_at,id",
                                        (source_id, start, current.isoformat())).fetchall()
        latest = store.connection.execute("SELECT * FROM collection_runs WHERE source_id=? AND run_at<=? ORDER BY run_at DESC,id DESC LIMIT 1",
                                          (source_id, current.isoformat())).fetchone()
        last_success = store.connection.execute("SELECT max(run_at) FROM collection_runs WHERE source_id=? AND status IN ('ok','empty') AND run_at<=?",
                                               (source_id, current.isoformat())).fetchone()[0]
        successes = sum(row["status"] in {"ok", "empty"} for row in rows)
        selected = sum(row["selected"] for row in rows)
        usable = sum(row["usable_text"] for row in rows)
        item = {"source_id": source_id, "source_name": latest["source_name"] if latest else source_id,
                "runs": len(rows), "successes": successes, "success_rate": successes / len(rows) if rows else None,
                "empty_runs": sum(row["status"] == "empty" for row in rows),
                "errors": sum(row["status"] == "error" for row in rows),
                "partial_runs": sum(row["status"] == "partial" for row in rows),
                "last_run": latest["run_at"] if latest else None, "last_success": last_success,
                "last_status": latest["status"] if latest else "never_run", "last_error": latest["error"] if latest else None,
                "usable_text_ratio": usable / selected if selected else None,
                "gap_runs": sum(row["gap"] for row in rows), "window_days": days,
                "last_coverage": json.loads(latest["details_json"]) if latest else {}}
        for key in ("fetched", "selected", "added", "updated", "duplicates", "usable_text", "limited", "older_skipped"):
            item[key] = sum(row[key] for row in rows)
        result.append(item)
    return result
