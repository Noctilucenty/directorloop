"""Job store and worker: idempotency, ordered events, cancellation, failure recording, crash recovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from directorloop.jobs import IdempotencyConflict, JobStore, JobWorker


def test_idempotency_same_key_same_body_returns_same_job(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.db")
    a, created_a = store.create("experiment", {"video_id": "v1"}, idempotency_key="k1")
    b, created_b = store.create("experiment", {"video_id": "v1"}, idempotency_key="k1")
    assert created_a and not created_b and a.id == b.id
    with pytest.raises(IdempotencyConflict):
        store.create("experiment", {"video_id": "v2"}, idempotency_key="k1")
    c, created_c = store.create("experiment", {"video_id": "v1"})
    assert created_c and c.id != a.id


def test_worker_runs_job_and_streams_ordered_events(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.db")

    def runner(job, on_stage):  # noqa: ANN001
        on_stage("ANALYZING", "genome", None)
        on_stage("RENDERING", "arms", {"n": 4})
        return {"experiment_id": "cexp_1"}

    worker = JobWorker(store, {"experiment": runner})
    job, _ = store.create("experiment", {"video_id": "v"})
    done = worker.run_once()
    assert done is not None and done.state == "COMPLETED" and done.view()["experiment_id"] == "cexp_1"
    ev = store.events(job.id)
    assert [e["seq"] for e in ev] == [1, 2] and ev[1]["data"] == {"n": 4}
    assert store.events(job.id, after=1)[0]["stage"] == "RENDERING"
    assert worker.run_once() is None, "nothing left to claim"


def test_failures_and_cancellation_are_recorded(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.db")

    def failing(job, on_stage):  # noqa: ANN001
        on_stage("RENDERING", "about to fail", None)
        raise RuntimeError("ffmpeg exploded")

    worker = JobWorker(store, {"experiment": failing})
    job, _ = store.create("experiment", {})
    done = worker.run_once()
    assert done.state == "FAILED" and "RuntimeError" in (done.error or "")
    assert "ffmpeg exploded" not in (done.error or ""), "arbitrary exception strings are not a public error contract"
    assert store.events(job.id)[-1]["stage"] == "FAILED"

    def slow(job, on_stage):  # noqa: ANN001
        store.request_cancel(job.id)
        on_stage("RENDERING", "checks cancel here", None)
        return {"experiment_id": "never"}

    worker2 = JobWorker(store, {"experiment": slow})
    job2, _ = store.create("experiment", {"x": 1})
    done2 = worker2.run_once()
    assert done2.state == "CANCELED"
    queued, _ = store.create("experiment", {"x": 2})
    assert store.request_cancel(queued.id).state == "CANCELED"


def test_stale_running_jobs_fail_on_restart(tmp_path: Path) -> None:
    db = tmp_path / "jobs.db"
    store = JobStore(db)
    job, _ = store.create("experiment", {})
    store.claim_next()
    store._conn.execute("UPDATE jobs SET heartbeat_at='2000-01-01T00:00:00.000+00:00' WHERE id=?", (job.id,))
    restarted = JobStore(db)
    assert restarted.recover_stale(lease_seconds=60) == [job.id]
    assert restarted.get(job.id).state == "FAILED"
