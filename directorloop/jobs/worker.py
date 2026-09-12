"""Background worker: runs queued experiment jobs one at a time and streams their stages into the job store."""

from __future__ import annotations

import logging
import threading
import time
import traceback
from collections.abc import Callable
from typing import Any

from .store import Job, JobStore

log = logging.getLogger(__name__)

Runner = Callable[[Job, Callable[[str, str, dict | None], None]], dict[str, Any]]


class JobCanceled(RuntimeError):
    pass


class JobWorker:
    def __init__(self, store: JobStore, runners: dict[str, Runner], poll_seconds: float = 0.5) -> None:
        self.store = store
        self.runners = runners
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="directorloop-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def run_once(self) -> Job | None:
        job = self.store.claim_next()
        if job is None:
            return None
        runner = self.runners.get(job.kind)
        if runner is None:
            self.store.finish(job.id, "FAILED", error=f"no runner for job kind {job.kind}")
            return self.store.get(job.id)

        def on_stage(stage: str, message: str, data: dict | None) -> None:
            self.store.add_event(job.id, stage, message, data)
            if self.store.cancel_requested(job.id):
                raise JobCanceled(f"canceled during {stage}")

        try:
            result = runner(job, on_stage)
            self.store.finish(job.id, "COMPLETED", result=result)
        except JobCanceled as exc:
            self.store.add_event(job.id, "CANCELED", str(exc))
            self.store.finish(job.id, "CANCELED", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - every failure is recorded on the job, never swallowed
            log.error("job %s failed: %s", job.id, traceback.format_exc())
            self.store.add_event(job.id, "FAILED", f"{type(exc).__name__}: {str(exc)[:500]}")
            self.store.finish(job.id, "FAILED", error=f"{type(exc).__name__}: {str(exc)[:500]}")
        return self.store.get(job.id)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job = self.run_once()
            except Exception:  # noqa: BLE001
                log.error("worker loop error: %s", traceback.format_exc())
                job = None
            if job is None:
                time.sleep(self.poll_seconds)
