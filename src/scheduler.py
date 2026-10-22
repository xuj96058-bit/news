"""Durable foreground collection scheduler; no OS task/service is installed.

Each source is independently committed. Expiring leases with renewal prevent
concurrent workers from collecting the same job. A crashed worker's source is
retried after its lease expires; inbox deduplication makes repeats safe.
Checkpoints describe completed windows, never an unsupported archive cursor.
"""

import argparse
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import math
import re
import sqlite3
import threading
import time
import uuid

from .feeds import DEFAULT_SOURCES, load_sources
from .news_store import DEFAULT_DB_PATH, NewsStore
from .source_health import record_collection, safe_collection_error


def _now(value=None):
    value = value or datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _schema(store):
    store.connection.execute("""CREATE TABLE IF NOT EXISTS collection_schedules (
        source_id TEXT PRIMARY KEY, interval_minutes INTEGER NOT NULL,
        enabled INTEGER NOT NULL, options_json TEXT NOT NULL, next_run_at TEXT NOT NULL,
        last_run_at TEXT, last_status TEXT, consecutive_failures INTEGER NOT NULL DEFAULT 0,
        lease_owner TEXT, lease_until TEXT, checkpoint_json TEXT NOT NULL DEFAULT '{}')""")
    store.connection.commit()


def _options(source_id, options):
    options = dict(options or {})
    allowed = ({"limit", "max_body_pages", "timeout", "search", "ticker", "institution", "incremental"} if source_id == "investat"
               else {"max_pages", "timeout"} if source_id == "sec"
               else {"limit_per_source", "max_age_days", "timeout"})
    if set(options) - allowed:
        raise ValueError("Unsupported schedule options; credentials and arbitrary URLs cannot be saved")
    ranges = {"limit": (1, 50), "max_body_pages": (1, 20), "timeout": (1, 120),
              "max_pages": (1, 100), "limit_per_source": (1, 1000), "max_age_days": (0, 3650)}
    for key, value in options.items():
        if key == "incremental":
            if not isinstance(value, bool):
                raise ValueError("Investat incremental must be a boolean")
        elif key in ranges:
            low, high = ranges[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError("Invalid numeric schedule option")
            if key != "timeout" and not isinstance(value, int):
                raise ValueError("Count options require integers")
        else:
            if not isinstance(value, str) or len(value) > 500:
                raise ValueError("Invalid schedule filter")
            # Same guard used by saved watchlists; reject tokens even in text.
            from .entities import safe_saved_text
            options[key] = safe_saved_text(value)
    return options


def configure_schedule(store, source_id, interval_minutes, enabled=True, options=None):
    if not isinstance(source_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,99}", source_id):
        raise ValueError("Invalid source ID")
    if isinstance(interval_minutes, bool) or not isinstance(interval_minutes, int) or not 1 <= interval_minutes <= 525600:
        raise ValueError("interval_minutes must be an integer from 1 to 525600")
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")
    _schema(store)
    previous = store.connection.execute("SELECT options_json,enabled FROM collection_schedules WHERE source_id=?", (source_id,)).fetchone()
    saved = _options(source_id, json.loads(previous["options_json"]) if options is None and previous else options)
    with store.connection:
        store.connection.execute("""INSERT INTO collection_schedules
            (source_id,interval_minutes,enabled,options_json,next_run_at) VALUES (?,?,?,?,?)
            ON CONFLICT(source_id) DO UPDATE SET interval_minutes=excluded.interval_minutes,
                enabled=excluded.enabled,options_json=excluded.options_json,
                next_run_at=CASE WHEN collection_schedules.enabled=0 AND excluded.enabled=1
                    THEN excluded.next_run_at ELSE collection_schedules.next_run_at END""",
            (source_id, interval_minutes, int(enabled), json.dumps(saved, ensure_ascii=False), _now().isoformat()))
    return next(item for item in list_schedules(store) if item["source_id"] == source_id)


def list_schedules(store):
    _schema(store)
    result = []
    for row in store.connection.execute("SELECT * FROM collection_schedules ORDER BY source_id"):
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        item["options"] = json.loads(item.pop("options_json"))
        item["checkpoint"] = json.loads(item.pop("checkpoint_json"))
        item.pop("lease_owner", None)
        result.append(item)
    return result


def _claim(store, current, owner, lease_seconds, excluded):
    # UPDATE and selection occur under one SQLite write lock across processes.
    with store.connection:
        store.connection.execute("BEGIN IMMEDIATE")
        rows = store.connection.execute("""SELECT * FROM collection_schedules
            WHERE enabled=1 AND next_run_at<=? AND (lease_until IS NULL OR lease_until<=?)
            ORDER BY next_run_at,source_id""", (current.isoformat(), current.isoformat())).fetchall()
        row = next((row for row in rows if row["source_id"] not in excluded), None)
        if row is None:
            return None
        store.connection.execute("UPDATE collection_schedules SET lease_owner=?,lease_until=? WHERE source_id=?",
                                 (owner, (current + timedelta(seconds=lease_seconds)).isoformat(), row["source_id"]))
        return dict(row)


@contextmanager
def _renew_lease(store, source_id, owner, lease_seconds):
    stop = threading.Event()
    # A separate connection is necessary because NewsStore's connection belongs
    # to the calling thread. In-memory databases are used only in synchronous tests.
    def renew():
        connection = sqlite3.connect(str(store.path), timeout=30)
        try:
            while not stop.wait(max(0.1, lease_seconds / 3)):
                try:
                    with connection:
                        connection.execute("UPDATE collection_schedules SET lease_until=? WHERE source_id=? AND lease_owner=?",
                                           ((_now() + timedelta(seconds=lease_seconds)).isoformat(), source_id, owner))
                except sqlite3.Error:
                    # A busy database is retried on the next renewal. The long
                    # default lease provides room for ordinary transient locks.
                    continue
        finally:
            connection.close()
    thread = None
    if str(store.path) != ":memory:":
        thread = threading.Thread(target=renew, daemon=True)
        thread.start()
    try:
        yield
    finally:
        stop.set()
        if thread:
            thread.join(timeout=35)


def _collect(store, source_id, options, sources, investat_token):
    if source_id == "investat":
        from .investat_pipeline import collect_investat
        return collect_investat(store, token=investat_token, **options)
    from .rss_pipeline import collect_sec, collect_sources
    if source_id == "sec":
        return collect_sec(store, **options)
    source = next((source for source in sources if source.id == source_id), None)
    if source is None:
        raise ValueError("Scheduled source is absent from configuration")
    return collect_sources([replace(source, enabled=True)], store, workers=1, **options)


def run_due(store, *, sources=None, investat_token=None, now=None, max_jobs=10,
            lease_seconds=900, runner=None):
    """Run at most max_jobs, each source once, using currently due persisted jobs.

    runner is a deterministic test seam: runner(store, source_id, options).
    Production collectors record their own source health; only unexpected worker
    errors are recorded here. Tokens are request-local and never persisted.
    """
    if not isinstance(max_jobs, int) or not 1 <= max_jobs <= 100 or lease_seconds < 1:
        raise ValueError("Invalid scheduler bounds")
    _schema(store)
    sources = load_sources() if sources is None else list(sources)
    results, visited = [], set()
    owner = uuid.uuid4().hex
    for _ in range(max_jobs):
        current = _now(now)
        claimed = _claim(store, current, owner, lease_seconds, visited)
        if claimed is None:
            break
        source_id = claimed["source_id"]
        visited.add(source_id)
        error, report = None, {}
        with _renew_lease(store, source_id, owner, lease_seconds):
            try:
                options = _options(source_id, json.loads(claimed["options_json"]))
                report = runner(store, source_id, options) if runner else _collect(store, source_id, options, sources, investat_token)
                outcomes = report.get("sources", [])
                if not outcomes:
                    raise ValueError("Collector returned no source outcomes")
                statuses = {item.get("status") for item in outcomes}
                if not statuses.issubset({"ok", "empty", "error", "partial"}):
                    raise ValueError("Collector returned an invalid status")
                status = "error" if statuses == {"error"} else "partial" if statuses & {"error", "partial"} else "empty" if statuses == {"empty"} else "ok"
                error = safe_collection_error(next((item.get("error") for item in outcomes if item.get("error")), None))
            except Exception as exc:
                status, error = "error", type(exc).__name__
                record_collection(store, {"source_id": source_id, "status": status, "error": error}, run_at=current)
            finished = _now(now)
            # A healthy but capped public window needs its normal cadence;
            # slowing it down would create larger gaps. Back off actual errors.
            retry_failure = status == "error" or (status == "partial" and bool(error))
            failures = claimed["consecutive_failures"] + 1 if retry_failure else 0
            # Backoff never grows beyond 24 hours or the configured interval for
            # infrequent jobs. Partial responses retain their committed content.
            delay = max(claimed["interval_minutes"] * 60, min(86400, 60 * 2 ** min(failures, 10))) if failures else claimed["interval_minutes"] * 60
            if source_id == "investat" and retry_failure:
                # Only normalized list/init 429 outcomes carry this field;
                # detail-only throttling has its own persistent detail timer.
                retry_after = [item.get("retry_after") for item in report.get("sources", [])
                               if item.get("provider_status_code") == 429]
                waits = [min(86400, math.ceil(value)) for value in retry_after
                         if isinstance(value, (int, float)) and not isinstance(value, bool)
                         and math.isfinite(value) and value >= 0]
                if waits:
                    delay = max(delay, max(waits))
            next_run = finished + timedelta(seconds=delay)
            checkpoint = json.loads(claimed["checkpoint_json"])
            checkpoint.update(last_attempt_at=finished.isoformat(), status=status,
                              continuation="Re-fetch current window with deduplication; no historical cursor")
            if status in {"ok", "empty"}:
                checkpoint["last_success_at"] = finished.isoformat()
            newest = [item.get("newest_published_at") for item in report.get("sources", []) if item.get("newest_published_at")]
            if newest:
                checkpoint["newest_published_at"] = max(newest)
            with store.connection:
                changed = store.connection.execute("""UPDATE collection_schedules SET last_run_at=?,last_status=?,
                    consecutive_failures=?,next_run_at=?,lease_owner=NULL,lease_until=NULL,checkpoint_json=?
                    WHERE source_id=? AND lease_owner=?""",
                    (finished.isoformat(), status, failures, next_run.isoformat(), json.dumps(checkpoint), source_id, owner)).rowcount
            results.append({"source_id": source_id, "status": status if changed else "lease_lost", "error": error,
                            "report": report, "next_run_at": next_run.isoformat() if changed else None})
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description="Persistent news collection scheduler (foreground only)")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--config", default=str(DEFAULT_SOURCES))
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--run-once", action="store_true")
    modes.add_argument("--worker", action="store_true")
    modes.add_argument("--list", action="store_true")
    modes.add_argument("--configure", metavar="SOURCE_ID")
    parser.add_argument("--interval-minutes", type=int, default=60)
    parser.add_argument("--disable", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=30)
    parser.add_argument("--max-jobs", type=int, default=10)
    args = parser.parse_args(argv)
    if not 1 <= args.poll_seconds <= 60:
        parser.error("poll-seconds must be between 1 and 60")
    sources = load_sources(args.config)
    with NewsStore(args.db) as store:
        if args.configure:
            if args.configure not in {source.id for source in sources} | {"sec", "investat"}:
                parser.error("Unknown configured source")
            print(json.dumps(configure_schedule(store, args.configure, args.interval_minutes, enabled=not args.disable), ensure_ascii=False, indent=2))
            return 0
        if args.list:
            print(json.dumps(list_schedules(store), ensure_ascii=False, indent=2))
            return 0
        try:
            while True:
                results = run_due(store, sources=sources, max_jobs=args.max_jobs)
                if results or args.run_once:
                    print(json.dumps(results, ensure_ascii=False, indent=2), flush=True)
                if args.run_once:
                    return 2 if any(item["status"] in {"error", "partial", "lease_lost"} for item in results) else 0
                time.sleep(args.poll_seconds)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
