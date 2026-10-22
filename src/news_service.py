"""Independent local collection and delivery service, with one process per checkout.

No credentials are persisted here. A held OS lock, rather than a PID file, owns
the service; each network loop uses its own SQLite connection. The initial
current-window collection establishes a baseline before delivery is enabled.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
import time

from dotenv import dotenv_values

from .news_store import NewsStore
from .notifications import (
    TelegramSender, configure_route, has_route, load_notification_config,
    notification_status, process_pending,
)
from .notification_language import prepare_payload
from .scheduler import configure_schedule, list_schedules, run_due
from .telegram_source import TelegramStore, load_config, sync_once


ROOT = Path(__file__).resolve().parents[1]
INVESTAT_INTERVAL_MINUTES = 5
INVESTAT_OPTIONS = {"limit": 50, "max_body_pages": 5, "timeout": 15, "incremental": True}
# Capture the process environment before the project .env is loaded by config
# resolution. File values are read again each cycle and are never logged.
_PROCESS_INVESTAT_TOKEN = os.getenv("INVESTAT_MCP_TOKEN", "")


class ServiceLock:
    """Crash-released, nonblocking OS file lock, also used by the launcher."""

    def __init__(self, root=ROOT):
        self.path = Path(root) / "data" / "news-service.lock"
        self.handle = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        return True

    def release(self):
        if self.handle is not None:
            try:
                self.handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.handle.close()
                self.handle = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *args):
        self.release()


def service_running(root=ROOT):
    lock = ServiceLock(root)
    if lock.acquire():
        lock.release()
        return False
    return True


def read_status(root=ROOT):
    try:
        data = json.loads((Path(root) / "data" / "news-service-status.json").read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return data
    except (OSError, ValueError):
        return {}


def request_stop(root=ROOT):
    path = Path(root) / "data" / "news-service.stop"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def _write_status(root, value):
    path = Path(root) / "data" / "news-service-status.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def can_activate_investat(report):
    """A partial body is a valid baseline; a missing article ID/save is not."""
    outcomes = report.get("sources", []) if isinstance(report, dict) else []
    item = next((item for item in outcomes if item.get("id") == "investat"), None)
    if not item or item.get("status") not in {"ok", "empty", "partial"}:
        return False
    if item.get("skipped", 0):
        return False
    selected = item.get("selected", item.get("fetched", 0))
    saved = sum(item.get(name, 0) for name in ("added", "updated", "duplicates"))
    return saved == selected and (selected > 0 or item.get("status") in {"ok", "empty"})


def load_investat_token(root=ROOT):
    values = dotenv_values(Path(root) / ".env")
    value = values.get("INVESTAT_MCP_TOKEN", _PROCESS_INVESTAT_TOKEN)
    token = value.strip() if isinstance(value, str) else ""
    return "" if token == "ift_your-token-here" else token


def make_investat_due(store):
    now = datetime.now(timezone.utc).isoformat()
    with store.connection:
        store.connection.execute(
            "UPDATE collection_schedules SET next_run_at=?,consecutive_failures=0 "
            "WHERE source_id='investat' AND (lease_until IS NULL OR lease_until<=?)", (now, now),
        )


def configure_investat_schedule(store, notify_config):
    """Set only Investat's requested schedule; leave other sources untouched."""
    previous = next((item for item in list_schedules(store) if item["source_id"] == "investat"), None)
    changed = (not previous or previous["interval_minutes"] != INVESTAT_INTERVAL_MINUTES
               or not previous["enabled"] or previous["options"] != INVESTAT_OPTIONS)
    now = datetime.now(timezone.utc).isoformat()
    preserve_backoff = bool(previous and previous.get("consecutive_failures", 0) > 0
                            and previous.get("last_status") in {"error", "partial"}
                            and previous["next_run_at"] > now)
    configure_schedule(store, "investat", INVESTAT_INTERVAL_MINUTES, options=INVESTAT_OPTIONS)
    if preserve_backoff:
        # Deploying new collection options or restarting must respect an
        # existing rate-limit/outage delay, even before the baseline is ready.
        with store.connection:
            store.connection.execute(
                "UPDATE collection_schedules SET next_run_at=MAX(next_run_at,?) WHERE source_id='investat' "
                "AND consecutive_failures>0 AND (lease_until IS NULL OR lease_until<=?)",
                (previous["next_run_at"], now),
            )
    elif changed or not has_route(store.connection, "investat", notify_config):
        # A previously hourly schedule must not delay the first baseline. Do
        # not steal an in-progress schedule lease held by another collector.
        with store.connection:
            store.connection.execute(
                "UPDATE collection_schedules SET next_run_at=? WHERE source_id='investat' "
                "AND (lease_until IS NULL OR lease_until<=?) AND next_run_at>?", (now, now, now),
            )


def telegram_cycle(root, telegram_config, notify_config):
    path = Path(root) / "data" / "telegram.sqlite3"
    with TelegramStore(path) as store:
        active = has_route(store.connection, "telegram", notify_config)
    # Receiver leases are shared with the existing Streamlit receiver; no
    # second receiver is ever created for the outbound bot.
    result = sync_once(telegram_config, path, max_batches=100 if not active else 10)
    if not active and result["status"] == "ok":
        with TelegramStore(path) as store:
            configure_route(store.connection, "telegram", notify_config)
        active = True
    return {"status": result["status"], "route_active": active,
            "added": result.get("added", 0), "updated": result.get("updated", 0)}


def collection_cycle(store, notify_config, *, investat_token=""):
    # An explicit empty token prevents a removed/changed .env value from
    # falling back to the stale value imported when this process started.
    outcomes = run_due(store, investat_token=investat_token)
    active = has_route(store.connection, "investat", notify_config)
    for outcome in outcomes:
        if (outcome["source_id"] == "investat" and not active
                and outcome["status"] != "lease_lost"
                and can_activate_investat(outcome.get("report", {}))):
            configure_route(store.connection, "investat", notify_config)
            active = True
    investat = next((item for item in outcomes if item["source_id"] == "investat"), None)
    schedule = next((item for item in list_schedules(store) if item["source_id"] == "investat"), {})
    if (investat_token and investat and investat["status"] == "partial"
            and can_activate_investat(investat.get("report", {}))
            and schedule.get("last_run_at")
            and schedule.get("next_run_at") == investat.get("next_run_at")):
        # All current article IDs were saved: incomplete detail bodies should
        # not slow discovery of the next new article. Keep the honest partial
        # status, while retaining five-minute detection. Match this completed
        # run so a concurrent collector's newer schedule is never overwritten.
        next_run = (datetime.fromisoformat(schedule["last_run_at"])
                    + timedelta(minutes=INVESTAT_INTERVAL_MINUTES)).isoformat()
        with store.connection:
            store.connection.execute(
                "UPDATE collection_schedules SET consecutive_failures=0,next_run_at=? "
                "WHERE source_id='investat' AND lease_owner IS NULL AND last_run_at=? AND next_run_at=?",
                (next_run, schedule["last_run_at"], schedule["next_run_at"]),
            )
        schedule = next(item for item in list_schedules(store) if item["source_id"] == "investat")
    if not investat_token:
        if investat:
            # Missing configuration is not a provider outage. Keep the normal
            # cadence, with immediate retry when a local token appears.
            next_run = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
            with store.connection:
                store.connection.execute(
                    "UPDATE collection_schedules SET consecutive_failures=0,next_run_at=? "
                    "WHERE source_id='investat' AND lease_owner IS NULL", (next_run,),
                )
        return {"status": "missing_token", "route_active": active, "jobs": len(outcomes)}
    result = {"status": investat["status"] if investat else schedule.get("last_status") or "waiting",
              "route_active": active, "jobs": len(outcomes), "waiting": investat is None,
              "last_run_at": schedule.get("last_run_at"), "next_run_at": schedule.get("next_run_at")}
    if investat:
        source = next((item for item in investat.get("report", {}).get("sources", [])
                       if item.get("id") == "investat"), {})
        for key in ("body_pages", "details_skipped"):
            value = source.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                result[key] = value
    return result


class NewsService:
    def __init__(self, root, notify_config, telegram_config=None, *, stop_event=None):
        self.root = Path(root)
        self.notify_config = notify_config
        self.telegram_config = telegram_config
        if telegram_config and telegram_config.chat_id == notify_config.destination_chat_id:
            raise ValueError("Telegram source and destination channels must differ.")
        self.stop = stop_event or threading.Event()
        self._status_lock = threading.Lock()
        self._states = {}

    def status(self, name, value):
        with self._status_lock:
            previous = self._states.get(name, {})
            counts = ({key: previous[key] for key in ("body_pages", "details_skipped") if key in previous}
                      if name == "investat" and value.get("waiting") else {})
            self._states[name] = {**counts, **value, "checked_at": datetime.now(timezone.utc).isoformat()}

    def snapshot(self, state="running"):
        with self._status_lock:
            states = dict(self._states)
        return {"state": state, "pid": os.getpid(), "root": str(self.root.resolve()),
                "heartbeat_at": time.time(), "destination_chat_id": self.notify_config.destination_chat_id,
                "sender_bot_id": self.notify_config.bot_id, "workers": states}

    def telegram_loop(self):
        if self.telegram_config is None:
            self.status("telegram", {"status": "not_configured", "route_active": False})
            return
        while not self.stop.is_set():
            try:
                result = telegram_cycle(self.root, self.telegram_config, self.notify_config)
            except Exception:
                result = {"status": "error"}
            self.status("telegram", result)
            self.stop.wait(1 if result.get("status") == "partial" else 30)

    def collection_loop(self):
        configured = False
        previous_token_digest = None
        token_observed = False
        while not self.stop.is_set():
            try:
                with NewsStore(self.root / "data" / "news.sqlite3") as store:
                    if not configured:
                        configure_investat_schedule(store, self.notify_config)
                        configured = True
                    token = load_investat_token(self.root)
                    token_digest = hashlib.sha256(token.encode("utf-8")).digest() if token else None
                    if token_observed and token_digest is not None and token_digest != previous_token_digest:
                        make_investat_due(store)
                    # Only an in-memory digest is retained for detecting a
                    # corrected credential; it never enters status or SQLite.
                    previous_token_digest = token_digest
                    token_observed = True
                    result = collection_cycle(store, self.notify_config, investat_token=token)
            except Exception:
                result = {"status": "error"}
            self.status("investat", result)
            self.stop.wait(30)

    def sender_loop(self):
        client = TelegramSender(self.notify_config)
        while not self.stop.is_set():
            for name, store_class, filename in (
                ("telegram", TelegramStore, "telegram.sqlite3"),
                ("investat", NewsStore, "news.sqlite3"),
            ):
                if self.stop.is_set():
                    return
                try:
                    with store_class(self.root / "data" / filename) as store:
                        result = process_pending(store.connection, self.notify_config, client=client,
                                                 limit=1, prepare_payload=prepare_payload)
                        result["backlog"] = notification_status(store.connection, self.notify_config)
                    self.status("delivery_" + name, result)
                except Exception:
                    self.status("delivery_" + name, {"status": "error"})
                # One common sender loop spaces sends to the same channel,
                # independently of incoming source speed or collection delays.
                self.stop.wait(1.1)

    def run(self):
        stop_path = self.root / "data" / "news-service.stop"
        stop_path.unlink(missing_ok=True)
        threads = [threading.Thread(target=target, name=name, daemon=True) for name, target in (
            ("news-telegram", self.telegram_loop), ("news-collection", self.collection_loop),
            ("news-delivery", self.sender_loop),
        )]
        _write_status(self.root, self.snapshot("starting"))
        for thread in threads:
            thread.start()
        try:
            while not self.stop.is_set():
                if stop_path.exists():
                    self.stop.set()
                    break
                _write_status(self.root, self.snapshot())
                self.stop.wait(2)
        finally:
            self.stop.set()
            # Requests have bounded timeouts. The service lock remains held
            # until process exit even if a daemon worker is finishing a call.
            for thread in threads:
                thread.join(timeout=1)
            _write_status(self.root, self.snapshot("stopping"))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Background news collection and Telegram delivery")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--worker", action="store_true")
    group.add_argument("--status", action="store_true")
    group.add_argument("--stop", action="store_true")
    args = parser.parse_args(argv)
    if args.status:
        print(json.dumps({"running": service_running(), **read_status()}, ensure_ascii=False))
        return 0
    if args.stop:
        request_stop()
        print("News service stop requested.")
        return 0
    try:
        config = load_notification_config()
        if config is None:
            print("Telegram delivery is not configured; background service was not started.")
            return 0
        receiver = load_config()
        service = NewsService(ROOT, config, receiver)
    except (ValueError, TypeError):
        print("News service configuration is invalid; check local Telegram settings.")
        return 1
    try:
        with ServiceLock(ROOT) as acquired:
            if not acquired:
                print("News service is already running.")
                return 0
            service.run()
    except KeyboardInterrupt:
        service.stop.set()
    except Exception:
        # HTTP exceptions can embed token-bearing URLs; never log tracebacks.
        print("News service stopped unexpectedly; restart the News launcher.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
