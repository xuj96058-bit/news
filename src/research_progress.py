"""Poll generation on the UI thread, including time spent waiting for the API."""
from __future__ import annotations

from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
import time

from .research import ResearchError, STREAM_DEADLINE_SECONDS


def run_generation(generate, on_update, *, timeout=STREAM_DEADLINE_SECONDS, tick_seconds=0.25):
    """Return a draft within the total deadline; only the caller may save it.

    The worker never touches Streamlit or the report store. If the API keeps
    sending heartbeats without a draft, the caller can still show elapsed time
    and stop waiting. Cancellation closes registered transports, and a late
    worker result is discarded after the deadline.
    """
    progress = Queue(maxsize=1)
    outcome = Queue(maxsize=1)
    stopped = Event()
    cancel_lock = Lock()
    close_callbacks = []
    started = time.monotonic()

    def close_resource(close):
        try:
            close()
        except Exception:
            pass

    def register_cancel(close):
        with cancel_lock:
            cancelled = stopped.is_set()
            if not cancelled:
                close_callbacks.append(close)
        if cancelled:
            close_resource(close)
            raise ResearchError("generation_timeout")

    def report_progress(stage, received_chars):
        if stopped.is_set():
            return
        try:
            progress.put_nowait((stage, received_chars))
        except Full:
            try:
                progress.get_nowait()
            except Empty:
                pass
            progress.put_nowait((stage, received_chars))

    def work():
        try:
            result = (True, generate(on_progress=report_progress, register_cancel=register_cancel))
        except Exception as error:
            result = (False, error)
        if not stopped.is_set():
            outcome.put_nowait(result)

    Thread(target=work, name="research-generation", daemon=True).start()
    stage, received_chars = "preparing", 0
    try:
        while True:
            elapsed = time.monotonic() - started
            if elapsed >= timeout:
                raise ResearchError("generation_timeout")
            try:
                stage, received_chars = progress.get_nowait()
            except Empty:
                pass
            on_update(stage, received_chars, elapsed)
            try:
                ok, value = outcome.get(timeout=min(tick_seconds, timeout - elapsed))
            except Empty:
                continue
            if time.monotonic() - started >= timeout:
                raise ResearchError("generation_timeout")
            if not ok:
                raise value
            return value
    finally:
        with cancel_lock:
            stopped.set()
            resources = list(reversed(close_callbacks))
            close_callbacks.clear()
        for close in resources:
            close_resource(close)
