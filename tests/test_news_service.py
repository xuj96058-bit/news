from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from src.news_store import NewsStore
from src.notifications import NotificationConfig, configure_route, has_route
from src.news_service import (
    NewsService, ServiceLock, can_activate_investat, collection_cycle,
    configure_investat_schedule, read_status, request_stop, service_running,
    telegram_cycle, load_investat_token, make_investat_due,
)
from src.scheduler import configure_schedule, list_schedules, run_due as scheduler_run_due
from src.telegram_source import TelegramConfig, TelegramStore


def baseline(status="ok", *, selected=2, skipped=0, saved=2):
    return {"sources": [{"id": "investat", "status": status, "selected": selected,
                         "fetched": selected, "added": saved, "skipped": skipped,
                         "limited": 1, "gap": True}]}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = NotificationConfig("123456:" + "a" * 30, -1001234567890)
        self.input = TelegramConfig("654321:" + "b" * 30, -1009876543210)

    def test_os_lock_blocks_second_worker_and_releases_without_pid_files(self):
        first, second = ServiceLock(self.root), ServiceLock(self.root)
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            self.assertTrue(service_running(self.root))
        finally:
            first.release()
            second.release()
        self.assertFalse(service_running(self.root))
        self.assertTrue(second.acquire())
        second.release()

    def test_investat_baseline_accepts_capped_window_or_partial_body_but_not_missing_records(self):
        self.assertTrue(can_activate_investat(baseline()))
        self.assertTrue(can_activate_investat(baseline("partial")))
        self.assertTrue(can_activate_investat(baseline("empty", selected=0, saved=0)))
        self.assertFalse(can_activate_investat(baseline("error")))
        self.assertFalse(can_activate_investat(baseline("partial", skipped=1, saved=1)))
        self.assertFalse(can_activate_investat(baseline("partial", saved=1)))
        self.assertFalse(can_activate_investat({}))

    def test_investat_schedule_is_five_minutes_unfiltered_and_preserves_rss(self):
        with NewsStore(self.root / "news.sqlite3") as store:
            configure_schedule(store, "rss_example", 120, enabled=False, options={"limit_per_source": 10})
            configure_schedule(store, "investat", 60, options={"ticker": "OLD"})
            future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            with store.connection:
                store.connection.execute("UPDATE collection_schedules SET next_run_at=? WHERE source_id='investat'", (future,))
            configure_investat_schedule(store, self.config)
            schedules = {item["source_id"]: item for item in list_schedules(store)}
            self.assertEqual(schedules["investat"]["interval_minutes"], 5)
            self.assertEqual(schedules["investat"]["options"]["limit"], 50)
            self.assertIs(schedules["investat"]["options"]["incremental"], True)
            self.assertNotIn("ticker", schedules["investat"]["options"])
            self.assertLess(schedules["investat"]["next_run_at"], future)
            self.assertEqual(schedules["rss_example"]["interval_minutes"], 120)
            self.assertFalse(schedules["rss_example"]["enabled"])

    def test_restart_and_new_incremental_option_preserve_existing_rate_limit_delay(self):
        with NewsStore(self.root / "news.sqlite3") as store:
            configure_schedule(store, "investat", 5, options={"limit": 50, "max_body_pages": 5, "timeout": 15})
            future = (datetime.now(timezone.utc) + timedelta(minutes=32)).isoformat()
            with store.connection:
                store.connection.execute("UPDATE collection_schedules SET next_run_at=?,last_status='error',consecutive_failures=5 WHERE source_id='investat'", (future,))
            configure_investat_schedule(store, self.config)
            schedule = list_schedules(store)[0]
            self.assertEqual(schedule["next_run_at"], future)
            self.assertEqual(schedule["consecutive_failures"], 5)
            self.assertIs(schedule["options"]["incremental"], True)

    def test_telegram_failure_or_backlog_does_not_enable_old_news_then_success_enables(self):
        for status in ("error", "busy", "partial"):
            with patch("src.news_service.sync_once", return_value={"status": status}):
                self.assertFalse(telegram_cycle(self.root, self.input, self.config)["route_active"])
        with patch("src.news_service.sync_once", return_value={"status": "ok"}) as sync:
            self.assertTrue(telegram_cycle(self.root, self.input, self.config)["route_active"])
            self.assertEqual(sync.call_args.kwargs["max_batches"], 100)
        with patch("src.news_service.sync_once", return_value={"status": "ok"}) as sync:
            self.assertTrue(telegram_cycle(self.root, self.input, self.config)["route_active"])
            self.assertEqual(sync.call_args.kwargs["max_batches"], 10)

    def test_failed_investat_baseline_does_not_activate_route(self):
        with NewsStore(self.root / "news.sqlite3") as store:
            for report in (baseline("error"), baseline("partial", skipped=1, saved=1)):
                outcomes = [{"source_id": "investat", "status": "partial", "report": report}]
                with patch("src.news_service.run_due", return_value=outcomes):
                    self.assertFalse(collection_cycle(store, self.config, investat_token="test-token")["route_active"])
            outcomes = [{"source_id": "investat", "status": "partial", "report": baseline("partial")}]
            with patch("src.news_service.run_due", return_value=outcomes):
                self.assertTrue(collection_cycle(store, self.config, investat_token="test-token")["route_active"])
            self.assertTrue(has_route(store.connection, "investat", self.config))

    def test_lease_lost_collection_cannot_activate_route(self):
        with NewsStore(self.root / "news.sqlite3") as store:
            outcomes = [{"source_id": "investat", "status": "lease_lost", "report": baseline()}]
            with patch("src.news_service.run_due", return_value=outcomes):
                self.assertFalse(collection_cycle(store, self.config, investat_token="test-token")["route_active"])

    def test_investat_local_token_is_reloaded_and_removal_does_not_use_stale_file_value(self):
        with patch("src.news_service._PROCESS_INVESTAT_TOKEN", ""):
            self.assertEqual(load_investat_token(self.root), "")
            path = self.root / ".env"
            path.write_text("INVESTAT_MCP_TOKEN=test-one\n", encoding="utf-8")
            self.assertEqual(load_investat_token(self.root), "test-one")
            path.write_text("INVESTAT_MCP_TOKEN=test-two\n", encoding="utf-8")
            self.assertEqual(load_investat_token(self.root), "test-two")
            path.write_text("INVESTAT_MCP_TOKEN=\n", encoding="utf-8")
            self.assertEqual(load_investat_token(self.root), "")

    def test_missing_investat_token_reports_configuration_problem_without_activating(self):
        with NewsStore(self.root / "news.sqlite3") as store:
            configure_investat_schedule(store, self.config)
            outcomes = [{"source_id": "investat", "status": "error", "report": baseline("error")}]
            with patch("src.news_service.run_due", return_value=outcomes) as run:
                result = collection_cycle(store, self.config, investat_token="")
                self.assertEqual(result["status"], "missing_token")
                self.assertFalse(result["route_active"])
                self.assertEqual(run.call_args.kwargs["investat_token"], "")
            future = list_schedules(store)[0]["next_run_at"]
            make_investat_due(store)
            self.assertLess(list_schedules(store)[0]["next_run_at"], future)

    def test_waiting_keeps_last_collection_failure_visible(self):
        with NewsStore(self.root / "news.sqlite3") as store:
            configure_investat_schedule(store, self.config)
            with store.connection:
                store.connection.execute("UPDATE collection_schedules SET last_status='error' WHERE source_id='investat'")
            with patch("src.news_service.run_due", return_value=[]):
                result = collection_cycle(store, self.config, investat_token="test-token")
                self.assertEqual(result["status"], "error")
                self.assertTrue(result["waiting"])

    def test_incremental_detail_counts_remain_visible_while_waiting(self):
        with NewsStore(self.root / "news.sqlite3") as store:
            report = baseline()
            report["sources"][0].update(body_pages=0, details_skipped=50)
            outcomes = [{"source_id": "investat", "status": "ok", "report": report}]
            with patch("src.news_service.run_due", return_value=outcomes):
                result = collection_cycle(store, self.config, investat_token="test-token")
            self.assertEqual(result["body_pages"], 0)
            self.assertEqual(result["details_skipped"], 50)
            service = NewsService(self.root, self.config, self.input)
            service.status("investat", result)
            service.status("investat", {"status": "ok", "waiting": True})
            visible = service.snapshot()["workers"]["investat"]
            self.assertEqual(visible["body_pages"], 0)
            self.assertEqual(visible["details_skipped"], 50)

    def test_replacing_invalid_nonempty_investat_token_resets_backoff(self):
        stop = Mock()
        stop.is_set.side_effect = [False, False, False, True]
        stop.wait.return_value = False
        service = NewsService(self.root, self.config, self.input, stop_event=stop)
        with patch("src.news_service.load_investat_token", side_effect=["bad-token", "bad-token", "corrected-token"]), \
                patch("src.news_service.make_investat_due") as due, \
                patch("src.news_service.collection_cycle", return_value={"status": "error"}):
            service.collection_loop()
            self.assertEqual(due.call_count, 1)
        self.assertNotIn("bad-token", str(service.snapshot()))
        self.assertNotIn("corrected-token", str(service.snapshot()))

    def test_initial_token_does_not_reset_backoff_but_token_added_later_does(self):
        for tokens, expected in ((["existing-token"], 0), (["", "new-token"], 1)):
            with self.subTest(token_count=len(tokens)):
                stop = Mock()
                stop.is_set.side_effect = [False] * len(tokens) + [True]
                stop.wait.return_value = False
                service = NewsService(self.root, self.config, self.input, stop_event=stop)
                with patch("src.news_service.load_investat_token", side_effect=tokens), \
                        patch("src.news_service.make_investat_due") as due, \
                        patch("src.news_service.collection_cycle", return_value={"status": "error"}):
                    service.collection_loop()
                    self.assertEqual(due.call_count, expected)

    def test_repeated_complete_investat_windows_with_partial_bodies_keep_five_minutes(self):
        with NewsStore(self.root / "news.sqlite3") as store:
            configure_investat_schedule(store, self.config)
            configure_schedule(store, "rss_example", 5)
            now = datetime.now(timezone.utc) + timedelta(seconds=1)
            def runner(_store, source_id, _options):
                report = baseline("partial")
                report["sources"][0].update(id=source_id, error="Some text could not be collected")
                return report
            for index in range(4):
                with patch("src.news_service.run_due", side_effect=lambda _store, **_kw: scheduler_run_due(
                        _store, sources=[], now=now, runner=runner)):
                    result = collection_cycle(store, self.config, investat_token="test-token")
                schedules = {item["source_id"]: item for item in list_schedules(store)}
                current = schedules["investat"]
                self.assertEqual(result["status"], "partial")
                self.assertEqual(current["last_status"], "partial")
                self.assertEqual(current["consecutive_failures"], 0)
                self.assertEqual(datetime.fromisoformat(current["next_run_at"]) - now, timedelta(minutes=5))
                self.assertEqual(schedules["rss_example"]["consecutive_failures"], index + 1)
                now += timedelta(minutes=30)
            self.assertGreater(datetime.fromisoformat(schedules["rss_example"]["next_run_at"])
                               - datetime.fromisoformat(schedules["rss_example"]["last_run_at"]), timedelta(minutes=5))

    def test_investat_list_or_save_failures_retain_backoff(self):
        for label, report in (("list", baseline("error", selected=0, saved=0)),
                              ("save", baseline("partial", selected=2, saved=1, skipped=1))):
            with self.subTest(failure=label), NewsStore(self.root / (label + ".sqlite3")) as store:
                configure_investat_schedule(store, self.config)
                report["sources"][0]["error"] = "Investat collection failed"
                now = datetime.now(timezone.utc) + timedelta(seconds=1)
                for index in range(3):
                    with patch("src.news_service.run_due", side_effect=lambda _store, **_kw: scheduler_run_due(
                            _store, sources=[], now=now, runner=lambda *_a: report)):
                        result = collection_cycle(store, self.config, investat_token="test-token")
                    current = list_schedules(store)[0]
                    self.assertEqual(current["consecutive_failures"], index + 1)
                    self.assertFalse(result["route_active"])
                    now += timedelta(minutes=30)
                self.assertEqual(datetime.fromisoformat(current["next_run_at"])
                                 - datetime.fromisoformat(current["last_run_at"]), timedelta(minutes=8))

    def test_same_source_and_destination_rejected_and_status_contains_no_secret(self):
        with self.assertRaises(ValueError):
            NewsService(self.root, self.config, TelegramConfig(self.input.token, self.config.destination_chat_id))
        service = NewsService(self.root, self.config, self.input)
        self.assertNotIn(self.config.token, str(service.snapshot()))
        self.assertNotIn(self.input.token, str(service.snapshot()))

    def test_independent_loops_start_and_stop_without_network(self):
        self.root.joinpath("data").mkdir()
        service = NewsService(self.root, self.config, self.input)
        started = [threading.Event() for _ in range(3)]
        def loop(index):
            started[index].set()
            service.stop.wait(10)
        service.telegram_loop = lambda: loop(0)
        service.collection_loop = lambda: loop(1)
        service.sender_loop = lambda: loop(2)
        thread = threading.Thread(target=service.run)
        thread.start()
        try:
            self.assertTrue(all(event.wait(2) for event in started))
            request_stop(self.root)
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(read_status(self.root)["state"], "stopping")
        finally:
            service.stop.set()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
