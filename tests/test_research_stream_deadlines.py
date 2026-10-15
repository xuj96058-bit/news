"""Wall-clock deadlines and safe handling of HTTP-200 provider stream errors."""
import time
import unittest
from threading import Event
from types import SimpleNamespace

from src.analysis_errors import provider_failure_reason, provider_status_code
from src.research import ResearchError, _stream_content


class StreamDeadlineTests(unittest.TestCase):
    def test_hidden_heartbeats_cannot_extend_deadline(self):
        class Heartbeats:
            def __init__(self):
                self.closed = Event()
            def __iter__(self):
                # An SDK may keep receiving network bytes without yielding a
                # completion chunk, so an outer `for chunk` never checks time.
                self.closed.wait(5)
                yield SimpleNamespace(choices=[])
            def close(self):
                self.closed.set()
        stream = Heartbeats()
        start = time.monotonic()
        with self.assertRaises(ResearchError) as raised:
            _stream_content(stream, deadline=start + 0.05)
        self.assertEqual(raised.exception.reason, "generation_timeout")
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertTrue(stream.closed.is_set())

    def test_cancel_is_observed_without_a_content_chunk(self):
        callbacks = []
        class Cancelled:
            def __iter__(self):
                callbacks[0]()
                return iter(())
            def close(self):
                pass
        with self.assertRaises(ResearchError):
            _stream_content(Cancelled(), deadline=time.monotonic() + 5,
                register_cancel=callbacks.append)

    def test_sse_error_code_is_used_without_persisting_message(self):
        for code in (401, 402, 403, 404, 429, 503):
            error = RuntimeError("do-not-store-this-secret")
            error.body = {"code": code, "message": "do-not-store-this-secret"}
            with self.subTest(code=code):
                self.assertEqual(provider_status_code(error), code)
                self.assertEqual(provider_failure_reason(error), f"provider_http_{code}")

    def test_malformed_sse_code_does_not_become_diagnostic_text(self):
        error = RuntimeError("secret")
        error.body = {"code": "secret", "message": "secret"}
        self.assertIsNone(provider_status_code(error))
        self.assertEqual(provider_failure_reason(error), "provider_or_processing_error")


if __name__ == "__main__":
    unittest.main()
