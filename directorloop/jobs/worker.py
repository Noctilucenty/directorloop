"""Background worker: runs queued experiment jobs one at a time and streams their stages into the job store."""

from __future__ import annotations

import logging
import re
import threading
import traceback
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .store import Job, JobStore

log = logging.getLogger(__name__)

Runner = Callable[[Job, Callable[[str, str, dict | None], None]], dict[str, Any]]


class JobCanceled(RuntimeError):
    pass


class JobInterrupted(RuntimeError):
    pass


SAFE_FAILURE_REASONS = frozenset({
    "provider credits exhausted; no automatic retry",
    "provider authentication or permission failed",
    "provider rate limit reached",
    "operation timed out",
    "worker execution interrupted; not retried",
    "cancellation requested by the operator",
    "run budget or deadline reached",
    "spending reservation limit reached; no request sent",
    "spending guard rejected request; no request sent",
    "model output token limit reached; response incomplete",
})


def _preserved_failure_reason(message: str) -> str | None:
    """Keep an already sanitized category across durable run/job exception wrappers.

    Only complete fixed categories or numeric limit templates are accepted; suffixes
    containing provider diagnostics, URLs or credentials cannot be echoed back.
    """
    reason = re.sub(r"^(?:[A-Za-z_][A-Za-z0-9_]*: )+", "", message)
    if reason in SAFE_FAILURE_REASONS:
        return reason
    if re.fullmatch(r"model-call limit of [0-9]+ reached|deadline of [0-9]+ s reached after [0-9]+ s", reason):
        return reason
    return None


def safe_limit_reason(exc: BaseException) -> str:
    """Only the fixed numeric budget templates are safe to echo as a stop reason."""
    message = str(exc)
    if re.fullmatch(r"model-call limit of [0-9]+ reached|deadline of [0-9]+ s reached after [0-9]+ s", message):
        return message
    return "run budget or deadline reached"


def safe_failure(exc: BaseException, job_kind: str | None = None) -> str:
    """Public failure classification without provider bodies, signed URLs or arbitrary exception text."""
    current: BaseException | None = exc
    seen: set[int] = set()
    category = "video import failed" if job_kind == "ingest" else "operation failed; inspect saved job stages"
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        preserved = _preserved_failure_reason(str(current))
        if preserved is not None:
            category = preserved
            break
        body = getattr(current, "body", None)
        detail = body.get("error", body) if isinstance(body, dict) else {}
        code = detail.get("code") if isinstance(detail, dict) else None
        status = getattr(current, "status_code", None)
        # String inspection supplies a safe category only; the inspected text is never persisted.
        message = str(current).lower()
        link_status = re.fullmatch(r"the link returned http ([45][0-9]{2})", message) if job_kind == "ingest" else None
        if link_status:
            category = f"video import failed: HTTP {link_status.group(1)}"
        if code in ("credit_balance_exhausted", "insufficient_quota") or "credit_balance_exhausted" in message or "insufficient_quota" in message:
            category = "provider credits exhausted; no automatic retry"
            break
        if status in (401, 403):
            category = "provider authentication or permission failed"
        elif status == 429:
            category = "provider rate limit reached"
        elif "timeout" in type(current).__name__.lower():
            category = "operation timed out"
        elif isinstance(current, JobInterrupted):
            category = "worker execution interrupted; not retried"
        elif isinstance(current, JobCanceled):
            category = "cancellation requested by the operator"
        elif type(current).__name__ == "BudgetExceeded":
            category = safe_limit_reason(current)
        elif type(current).__name__ == "SpendLimitExceeded":
            category = "spending reservation limit reached; no request sent"
            break
        elif type(current).__name__ == "SpendGuardError":
            category = "spending guard rejected request; no request sent"
            break
        current = current.__cause__ or current.__context__
    return f"{type(exc).__name__}: {category}"


class JobWorker:
    def __init__(self, store: JobStore, runners: dict[str, Runner], poll_seconds: float = 0.5, *,
                 lease_seconds: float = 180, heartbeat_seconds: float = 5,
                 on_recovery: Callable[[Job], None] | None = None) -> None:
        if not 0 < heartbeat_seconds < lease_seconds:
            raise ValueError("heartbeat interval must be positive and shorter than the lease")
        self.store = store
        self.runners = runners
        self.poll_seconds = poll_seconds
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.on_recovery = on_recovery
        self.owner_id = str(uuid.uuid4())
        self._active_job_id: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._maintenance_thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._maintenance_thread = threading.Thread(target=self._maintain_loop, name="directorloop-job-maintenance", daemon=True)
        self._maintenance_thread.start()
        self._thread = threading.Thread(target=self._loop, name="directorloop-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        if self._maintenance_thread:
            self._maintenance_thread.join(timeout=timeout)

    def maintain_once(self) -> None:
        """Keep long steps live and reconcile expired owners without replaying their work."""
        active = self._active_job_id
        if active:
            self.store.heartbeat(active, self.owner_id)
        self.store.recover_stale(lease_seconds=self.lease_seconds)
        for job in self.store.pending_recovery():
            try:
                if self.on_recovery:
                    self.on_recovery(job)
                self.store.complete_recovery(job.id)
            except Exception:
                # The durable pending flag keeps artifact reconciliation retryable after a write failure.
                log.error("job %s recovery reconciliation failed", job.id)

    def _maintain_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.maintain_once()
            except Exception:
                log.error("job maintenance failed", exc_info=False)
            self._stop.wait(self.heartbeat_seconds)

    def run_once(self) -> Job | None:
        job = self.store.claim_next(owner_id=self.owner_id)
        if job is None:
            return None
        self._active_job_id = job.id
        runner = self.runners.get(job.kind)
        if runner is None:
            self.store.finish(job.id, "FAILED", error=f"no runner for job kind {job.kind}")
            self._active_job_id = None
            return self.store.get(job.id)

        def on_stage(stage: str, message: str, data: dict | None) -> None:
            if self._stop.is_set():
                raise JobInterrupted("worker stopped during execution; not retried")
            current = self.store.get(job.id)
            if current is None or current.state != "RUNNING" or current.owner_id != self.owner_id:
                raise JobInterrupted("worker no longer owns the execution lease; not retried")
            if self.store.cancel_requested(job.id):
                raise JobCanceled(f"canceled during {stage}")
            self.store.add_event(job.id, stage, message, data, owner_id=self.owner_id)

        try:
            result = runner(job, on_stage)
            self.store.finish(job.id, "COMPLETED", result=result)
        except JobCanceled:
            self.store.add_event(job.id, "CANCELED", "cancellation requested by the operator")
            self.store.finish(job.id, "CANCELED", error="cancellation requested by the operator")
        except Exception as exc:  # noqa: BLE001 - every failure is recorded on the job, never swallowed
            error = safe_failure(exc, job.kind)
            locations = [f"{Path(f.filename).name}:{f.lineno}" for f in traceback.extract_tb(exc.__traceback__)]
            log.error("job %s failed: %s; locations=%s", job.id, error, locations)
            self.store.add_event(job.id, "FAILED", error)
            self.store.finish(job.id, "FAILED", error=error)
        finally:
            self._active_job_id = None
        return self.store.get(job.id)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job = self.run_once()
            except Exception as exc:  # noqa: BLE001
                log.error("worker loop error: %s", safe_failure(exc))
                job = None
            if job is None:
                self._stop.wait(self.poll_seconds)
