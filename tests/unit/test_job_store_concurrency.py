"""Concurrent status/event polls must not corrupt a shared SQLite statement."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from directorloop.jobs import JobStore


def test_polling_remains_consistent_while_worker_records_events(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.db")
    params = {"video_id": "concurrent-video", "evidence": [1, 2, 3]}
    job, _ = store.create("screening", params, idempotency_key="polling-test")
    assert store.claim_next(owner_id="worker").id == job.id
    start = Barrier(8)

    def poll() -> None:
        start.wait(timeout=5)
        for _ in range(160):
            current = store.get(job.id)
            assert current is not None and current.id == job.id
            assert current.params == params and current.state in {"RUNNING", "COMPLETED"}
            listed = store.list()
            assert len(listed) == 1 and listed[0].params == params
            events = store.events(job.id)
            assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
            assert all(event["data"] == {"index": event["seq"]} for event in events)
            assert not store.cancel_requested(job.id)
            assert store.pending_recovery() == []
            assert store.idempotent_result("screening", params, "polling-test").id == job.id

    def record() -> None:
        start.wait(timeout=5)
        for index in range(1, 81):
            assert store.add_event(job.id, "REVIEW", "Saved evidence", {"index": index}, owner_id="worker") == index
            assert store.heartbeat(job.id, "worker")
        store.finish(job.id, "COMPLETED", result={"screen_id": "screen_concurrent"})

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(poll) for _ in range(7)] + [pool.submit(record)]
        for future in futures:
            future.result(timeout=15)
    final = store.get(job.id)
    assert final.state == "COMPLETED" and final.result == {"screen_id": "screen_concurrent"}
    assert len(store.events(job.id)) == 80
