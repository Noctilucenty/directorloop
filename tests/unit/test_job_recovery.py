"""Interrupted jobs never resume paid work; live long steps retain their execution lease."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from directorloop.jobs import JobStore, JobWorker
from directorloop.jobs.recovery import reconcile_interrupted_job
from directorloop.jobs.worker import safe_failure


def test_quick_restart_recovers_when_recent_lease_expires_without_replaying(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.db")
    job, _ = store.create("causal", {})
    store.claim_next(owner_id="stopped-worker")
    recovered = threading.Event()
    invoked = []
    worker = JobWorker(store, {"causal": lambda *_: invoked.append(True)}, lease_seconds=0.08,
                       heartbeat_seconds=0.01, on_recovery=lambda _: recovered.set())
    worker.maintain_once()
    assert store.get(job.id).state == "RUNNING"
    worker.start()
    try:
        assert recovered.wait(2)
        assert store.get(job.id).state == "FAILED"
        assert invoked == []
        assert store.events(job.id)[-1]["data"]["automatic_retry"] is False
    finally:
        worker.stop()


def test_heartbeat_keeps_long_step_live_for_other_store_connection(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.db")
    other = JobStore(tmp_path / "jobs.db")
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()

    def runner(job, emit):
        entered.set()
        assert release.wait(2)
        emit("DONE", "long step finished", None)
        completed.set()
        return {}

    worker = JobWorker(store, {"causal": runner}, lease_seconds=0.1, heartbeat_seconds=0.01)
    job, _ = store.create("causal", {})
    worker.start()
    try:
        assert entered.wait(1)
        assert not release.wait(0.3)  # Three leases pass without a workflow event.
        assert other.recover_stale(lease_seconds=0.1) == []
        assert other.get(job.id).state == "RUNNING"
        assert not other.heartbeat(job.id, "different-worker")
        release.set()
        assert completed.wait(1)
    finally:
        release.set()
        worker.stop()
    assert store.get(job.id).state == "COMPLETED"
    assert not store.heartbeat(job.id, worker.owner_id)


@pytest.mark.parametrize("kind,directory", [("run", "runs"), ("abc", "abc"), ("causal", "causal")])
def test_recovery_preserves_snapshot_and_closes_only_running_stages(tmp_path: Path, kind: str, directory: str):
    store = JobStore(tmp_path / "jobs.db")
    job, _ = store.create(kind, {})
    store.claim_next(owner_id="terminated")
    rid = f"{kind}_{job.id.removeprefix('job_')}"
    path = tmp_path / directory / f"{rid}.json"
    path.parent.mkdir()
    original = {"id": rid, "status": "running", "custom_evidence": ["keep"], "workflow_stages": [
        {"id": "done", "status": "completed", "duration_ms": 125},
        {"id": "interrupted", "status": "running", "duration_ms": None, "summary": {"model_calls": 3}},
    ]}
    path.write_text(json.dumps(original))
    before = path.read_bytes()
    store._conn.execute("UPDATE jobs SET heartbeat_at='2000-01-01T00:00:00+00:00' WHERE id=?", (job.id,))
    worker = JobWorker(store, {}, on_recovery=lambda j: reconcile_interrupted_job(tmp_path, j))
    worker.maintain_once()
    saved = json.loads(path.read_text())
    receipt_path = tmp_path / "job-recovery" / f"{job.id}.json"
    receipt = receipt_path.read_bytes()
    assert saved["status"] == "failed" and saved["custom_evidence"] == ["keep"]
    assert saved["workflow_stages"][0] == original["workflow_stages"][0]
    interrupted = saved["workflow_stages"][1]
    assert interrupted["status"] == "failed" and interrupted["duration_ms"] is None
    assert interrupted["summary"]["model_calls"] == 3
    assert (tmp_path / "job-recovery" / f"{job.id}.before.json").read_bytes() == before
    assert json.loads(receipt)["automatic_retry"] is False
    assert store.pending_recovery() == []
    reconcile_interrupted_job(tmp_path, store.get(job.id))
    assert receipt_path.read_bytes() == receipt


def test_recovery_hook_retries_persistence_only_and_preserves_terminal_artifact(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.db")
    job, _ = store.create("causal", {})
    store.claim_next()
    store._conn.execute("UPDATE jobs SET heartbeat_at='2000-01-01T00:00:00+00:00' WHERE id=?", (job.id,))
    rid = "causal_" + job.id.removeprefix("job_")
    path = tmp_path / "causal" / f"{rid}.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"id": rid, "status": "completed", "arms": [{"evidence": "preserve"}]}))
    before = path.read_bytes()
    calls = []

    def recover(j):
        calls.append(j.id)
        if len(calls) == 1:
            raise OSError("simulated unavailable storage")
        reconcile_interrupted_job(tmp_path, j)

    worker = JobWorker(store, {}, on_recovery=recover)
    worker.maintain_once()
    assert len(store.pending_recovery()) == 1
    worker.maintain_once()
    assert store.pending_recovery() == [] and len(calls) == 2
    assert path.read_bytes() == before


def test_worker_errors_keep_safe_category_without_signed_urls_or_provider_body(tmp_path: Path, caplog):
    secret = "signed-private-credential"

    def runner(*_):
        try:
            raise RuntimeError(f"insufficient_quota https://example.invalid?token={secret}")
        except RuntimeError as cause:
            raise ValueError(f"provider body includes {secret}") from cause

    store = JobStore(tmp_path / "jobs.db")
    job, _ = store.create("audit", {})
    result = JobWorker(store, {"audit": runner}).run_once()
    assert result.state == "FAILED" and "provider credits exhausted" in result.error
    assert "ValueError" in result.error
    assert secret not in result.error + json.dumps(store.events(job.id)) + caplog.text
    assert "https://example.invalid" not in caplog.text
    assert safe_failure(TimeoutError("private body")) == "TimeoutError: operation timed out"


@pytest.mark.parametrize("reason", [
    "provider credits exhausted; no automatic retry",
    "provider authentication or permission failed",
    "provider rate limit reached",
    "operation timed out",
    "worker execution interrupted; not retried",
    "cancellation requested by the operator",
    "model-call limit of 120 reached",
    "deadline of 90 s reached after 91 s",
    "spending reservation limit reached; no request sent",
    "spending guard rejected request; no request sent",
    "model output token limit reached; response incomplete",
])
def test_safe_failure_survives_persisted_run_and_job_wrappers(reason):
    sanitized = f"ProviderError: {reason}"
    for _ in range(3):
        sanitized = safe_failure(RuntimeError(sanitized))
        assert sanitized == f"RuntimeError: {reason}"


def test_wrapped_known_reason_cannot_echo_a_secret_suffix():
    result = safe_failure(RuntimeError("ProviderError: provider credits exhausted; no automatic retry token=private-secret"))
    assert "private-secret" not in result
    assert result == "RuntimeError: operation failed; inspect saved job stages"


def test_worker_records_preserved_quota_category_in_job_and_events(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job, _ = store.create("causal", {})

    def runtime_returned_failure(*_):
        # Matches Services.run_causal_job wrapping an already sanitized saved run.error.
        raise RuntimeError("ProviderError: provider credits exhausted; no automatic retry")

    result = JobWorker(store, {"causal": runtime_returned_failure}).run_once()
    assert result.error == "RuntimeError: provider credits exhausted; no automatic retry"
    assert store.events(job.id)[-1]["message"] == result.error


def test_spending_guard_failure_is_actionable_without_echoing_details():
    from directorloop.providers.spend import SpendGuardError, SpendLimitExceeded

    assert safe_failure(SpendLimitExceeded("private diagnostic")) == "SpendLimitExceeded: spending reservation limit reached; no request sent"
    assert safe_failure(SpendGuardError("private diagnostic")) == "SpendGuardError: spending guard rejected request; no request sent"
