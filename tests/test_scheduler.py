from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import time
import unittest

from src.news_store import NewsStore
from src.scheduler import configure_schedule, list_schedules, run_due, _claim


def report(status="ok"):
    return {"sources": [{"id": "test", "status": status}], "added": 0}


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "news.db"
        self.now = datetime.now(timezone.utc) + timedelta(seconds=1)

    def test_restart_preserves_due_time_and_checkpoint(self):
        with NewsStore(self.path) as store:
            configure_schedule(store, "test", 60)
            result = run_due(store, sources=[], now=self.now, runner=lambda *a: report())
            self.assertEqual(len(result), 1)
        with NewsStore(self.path) as store:
            calls = []
            self.assertEqual(run_due(store, sources=[], now=self.now + timedelta(minutes=59), runner=lambda *a: calls.append(1)), [])
            self.assertEqual(calls, [])
            self.assertEqual(list_schedules(store)[0]["checkpoint"]["last_success_at"], self.now.isoformat())
            self.assertEqual(len(run_due(store, sources=[], now=self.now + timedelta(minutes=60), runner=lambda *a: report())), 1)

    def test_concurrent_connection_cannot_claim_active_job(self):
        with NewsStore(self.path) as store:
            configure_schedule(store, "test", 1)
            def runner(*args):
                with NewsStore(self.path) as other:
                    result = run_due(other, sources=[], now=self.now, runner=lambda *a: self.fail("Duplicate collection"))
                    self.assertEqual(result, [])
                return report()
            self.assertEqual(run_due(store, sources=[], now=self.now, runner=runner)[0]["status"], "ok")

    def test_crashed_worker_lease_expires_and_is_reclaimed(self):
        with NewsStore(self.path) as store:
            configure_schedule(store, "test", 1)
            self.assertIsNotNone(_claim(store, self.now, "crashed-worker", 10, set()))
        with NewsStore(self.path) as store:
            self.assertEqual(run_due(store, sources=[], now=self.now + timedelta(seconds=9), runner=lambda *a: report()), [])
            self.assertEqual(len(run_due(store, sources=[], now=self.now + timedelta(seconds=10), runner=lambda *a: report())), 1)
            self.assertIsNone(list_schedules(store)[0]["lease_until"])

    def test_heartbeat_renews_lease_during_longer_running_collection(self):
        with NewsStore(self.path) as store:
            configure_schedule(store, "test", 1)
            def runner(*args):
                first_lease = list_schedules(store)[0]["lease_until"]
                time.sleep(2.3)  # Pass the original two-second lease.
                with NewsStore(self.path) as other:
                    self.assertGreater(list_schedules(other)[0]["lease_until"], first_lease)
                    self.assertEqual(run_due(other, sources=[], runner=lambda *a: self.fail("Renewed lease was ignored")), [])
                return report()
            self.assertEqual(run_due(store, sources=[], lease_seconds=2, runner=runner)[0]["status"], "ok")

    def test_user_timezone_is_normalized_for_due_time_and_checkpoint(self):
        with NewsStore(self.path) as store:
            configure_schedule(store, "test", 1)
            local = self.now.astimezone(timezone(timedelta(hours=8)))
            run_due(store, sources=[], now=local, runner=lambda *a: report())
            schedule = list_schedules(store)[0]
            self.assertEqual(schedule["last_run_at"], self.now.isoformat())
            self.assertEqual(schedule["next_run_at"], (self.now + timedelta(minutes=1)).isoformat())

    def test_failures_back_off_and_success_resets_failure_counter(self):
        with NewsStore(self.path) as store:
            configure_schedule(store, "test", 1)
            result = run_due(store, sources=[], now=self.now, runner=lambda *a: report("error"))[0]
            self.assertEqual(result["next_run_at"], (self.now + timedelta(minutes=2)).isoformat())
            result = run_due(store, sources=[], now=self.now + timedelta(minutes=2), runner=lambda *a: report("error"))[0]
            self.assertEqual(result["next_run_at"], (self.now + timedelta(minutes=6)).isoformat())
            run_due(store, sources=[], now=self.now + timedelta(minutes=6), runner=lambda *a: report())
            self.assertEqual(list_schedules(store)[0]["consecutive_failures"], 0)

    def test_one_broken_source_does_not_stop_other_due_sources(self):
        with NewsStore(self.path) as store:
            configure_schedule(store, "bad", 1)
            configure_schedule(store, "good", 1)
            def runner(store, source_id, options):
                if source_id == "bad":
                    raise RuntimeError("Bearer do-not-persist-this-secret")
                return report()
            result = run_due(store, sources=[], now=self.now, runner=runner)
            self.assertEqual([item["status"] for item in result], ["error", "ok"])
            self.assertNotIn("do-not-persist", str(result))
            self.assertEqual(result[0]["error"], "RuntimeError")

    def test_capped_successful_windows_keep_normal_collection_cadence(self):
        with NewsStore(self.path) as store:
            configure_schedule(store, "test", 1)
            result = run_due(store, sources=[], now=self.now, runner=lambda *a: report("partial"))[0]
            self.assertEqual(result["next_run_at"], (self.now + timedelta(minutes=1)).isoformat())
            self.assertEqual(list_schedules(store)[0]["consecutive_failures"], 0)

    def test_schedule_rejects_credentials_unknown_options_and_bad_limits(self):
        with NewsStore(self.path) as store:
            for options in ({"token": "secret"}, {"search": "ift__abcdefghijklmnop"}, {"limit": 51}, {"timeout": float("nan")}):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    configure_schedule(store, "investat", 60, options=options)
            configure_schedule(store, "investat", 60, enabled=False, options={"institution": "UBS"})
            self.assertEqual(run_due(store, sources=[], now=self.now, runner=lambda *a: self.fail()), [])
            self.assertEqual(list_schedules(store)[0]["options"], {"institution": "UBS"})


if __name__ == "__main__":
    unittest.main()
