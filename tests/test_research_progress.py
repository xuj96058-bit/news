"""Generation polling stays responsive while an API worker is waiting."""

from threading import Event, current_thread, get_ident
import unittest

from src.research import ResearchError
from src.research_progress import run_generation


class ResearchProgressTests(unittest.TestCase):
    def test_draft_is_acknowledged_on_caller_thread_before_review_starts(self):
        caller = get_ident()
        stages = []
        draft = {'title': 'Initial', 'body': 'Editable initial draft'}

        def generate(*, on_progress, register_cancel, on_draft):
            identifier = on_draft(draft)
            stages.append(('review', identifier))
            return {'body': 'Reviewed draft'}

        def save_draft(value):
            self.assertEqual(get_ident(), caller)
            self.assertIs(value, draft)
            stages.append(('saved', 'report-id'))
            return 'report-id'

        result = run_generation(generate, lambda *event: None, on_artifact=save_draft,
                                timeout=1, tick_seconds=0.01)
        self.assertEqual(stages, [('saved', 'report-id'), ('review', 'report-id')])
        self.assertEqual(result['body'], 'Reviewed draft')

    def test_failed_draft_save_stops_worker_before_review(self):
        review_started = Event()
        worker_finished = Event()
        expected = ValueError('Synthetic save failure')

        def generate(*, on_progress, register_cancel, on_draft):
            try:
                on_draft({'body': 'Initial'})
                review_started.set()
                return {'body': 'Reviewed'}
            finally:
                worker_finished.set()

        def save_draft(value):
            raise expected

        with self.assertRaises(ValueError) as caught:
            run_generation(generate, lambda *event: None, on_artifact=save_draft,
                           timeout=1, tick_seconds=0.01)
        self.assertIs(caught.exception, expected)
        self.assertTrue(worker_finished.wait(1))
        self.assertFalse(review_started.is_set())

    def test_elapsed_updates_while_blocked_and_updates_run_on_caller_thread(self):
        release = Event()
        self.addCleanup(release.set)
        caller = get_ident()
        worker_threads = []
        updates = []
        draft = {'title': 'Synthetic draft', 'body': 'Synthetic content'}

        def generate(*, on_progress, register_cancel):
            worker_threads.append(get_ident())
            on_progress('requesting', 0)
            release.wait(2)
            return draft

        def update(stage, characters, elapsed):
            updates.append((stage, characters, elapsed, get_ident()))
            if elapsed >= 0.035:
                release.set()

        self.assertIs(run_generation(generate, update, timeout=1, tick_seconds=0.01), draft)
        self.assertGreaterEqual(len(updates), 3)
        self.assertTrue(any(item[0] == 'requesting' for item in updates))
        self.assertEqual({item[3] for item in updates}, {caller})
        self.assertNotEqual(worker_threads[0], caller)
        times = [item[2] for item in updates]
        self.assertEqual(times, sorted(times))
        self.assertGreater(times[-1], times[0])

    def test_progress_burst_shows_latest_received_character_count(self):
        release = Event()
        self.addCleanup(release.set)
        updates = []

        def generate(*, on_progress, register_cancel):
            for characters in range(1, 1001):
                on_progress('receiving', characters)
            release.wait(2)
            return {'body': 'Synthetic content'}

        def update(stage, characters, elapsed):
            updates.append((stage, characters))
            if characters == 1000:
                release.set()

        run_generation(generate, update, timeout=1, tick_seconds=0.01)
        self.assertIn(('receiving', 1000), updates)

    def test_total_timeout_ends_wait_and_ignores_late_worker_events(self):
        release = Event()
        finished = Event()
        self.addCleanup(release.set)
        updates = []

        def generate(*, on_progress, register_cancel):
            on_progress('requesting', 0)
            release.wait(2)
            on_progress('ready', 100)
            finished.set()
            return {'body': 'Late synthetic content'}

        with self.assertRaises(ResearchError) as caught:
            run_generation(generate, lambda *event: updates.append(event), timeout=0.04, tick_seconds=0.01)
        self.assertEqual(caught.exception.reason, 'generation_timeout')
        self.assertFalse(finished.is_set())
        before_late_result = list(updates)
        release.set()
        self.assertTrue(finished.wait(1))
        self.assertEqual(updates, before_late_result)
        self.assertNotIn('ready', [event[0] for event in updates])

    def test_generation_error_is_propagated_to_caller(self):
        expected = ResearchError('invalid_citations')

        def generate(*, on_progress, register_cancel):
            on_progress('validating', 20)
            raise expected

        with self.assertRaises(ResearchError) as caught:
            run_generation(generate, lambda *event: None, timeout=1, tick_seconds=0.01)
        self.assertIs(caught.exception, expected)

    def test_timeout_closes_blocked_transport_and_worker_exits(self):
        closed = Event()
        finished = Event()
        self.addCleanup(closed.set)
        worker_threads = []
        close_calls = []

        def close_transport():
            close_calls.append(get_ident())
            closed.set()

        def generate(*, on_progress, register_cancel):
            worker_threads.append(current_thread())
            register_cancel(close_transport)
            on_progress('requesting', 0)
            if not closed.wait(2):
                raise AssertionError('The blocked transport was never closed.')
            finished.set()
            return {'body': 'Late synthetic content'}

        with self.assertRaises(ResearchError) as caught:
            run_generation(generate, lambda *event: None, timeout=0.04, tick_seconds=0.01)
        self.assertEqual(caught.exception.reason, 'generation_timeout')
        self.assertTrue(closed.is_set())
        self.assertTrue(finished.wait(1))
        worker_threads[0].join(1)
        self.assertFalse(worker_threads[0].is_alive())
        self.assertEqual(len(close_calls), 1)

    def test_resource_registered_after_timeout_is_closed_immediately(self):
        release = Event()
        closed = Event()
        finished = Event()
        self.addCleanup(release.set)
        registered_then_closed = []
        registration_errors = []

        def generate(*, on_progress, register_cancel):
            release.wait(2)
            try:
                register_cancel(closed.set)
            except ResearchError as error:
                registration_errors.append(error.reason)
            finally:
                registered_then_closed.append(closed.is_set())
                finished.set()
            return {'body': 'Late synthetic content'}

        with self.assertRaises(ResearchError) as caught:
            run_generation(generate, lambda *event: None, timeout=0.04, tick_seconds=0.01)
        self.assertEqual(caught.exception.reason, 'generation_timeout')
        self.assertFalse(closed.is_set())
        release.set()
        self.assertTrue(finished.wait(1))
        self.assertEqual(registered_then_closed, [True])
        self.assertEqual(registration_errors, ['generation_timeout'])

    def test_update_interruption_closes_transport_before_propagating(self):
        registered = Event()
        closed = Event()
        finished = Event()
        self.addCleanup(closed.set)

        class UiInterrupted(BaseException):
            pass

        def generate(*, on_progress, register_cancel):
            register_cancel(closed.set)
            registered.set()
            closed.wait(2)
            finished.set()
            return {'body': 'Late synthetic content'}

        def update(*event):
            if registered.is_set():
                raise UiInterrupted()

        with self.assertRaises(UiInterrupted):
            run_generation(generate, update, timeout=1, tick_seconds=0.01)
        self.assertTrue(closed.is_set())
        self.assertTrue(finished.wait(1))


if __name__ == '__main__':
    unittest.main()
