"""Execution hierarchy is honest under parallelism, errors, and offline tracing."""

from __future__ import annotations

from contextvars import copy_context
from threading import Barrier
from types import SimpleNamespace

import pytest
import weave
from weave.trace.context import call_context

from directorloop.jobs.worker import JobCanceled
from directorloop.observability import workflow as wf


def test_offline_hierarchy_persist_and_terminal_order(monkeypatch, tmp_path):
    monkeypatch.setattr(weave, "get_client", lambda: None)
    events, persisted = [], []

    @wf.workflow_session(persist=lambda result, data_dir: persisted.append(result.workflow_stages))
    def run(data_dir, on_stage):
        result = SimpleNamespace(workflow_stages=[])
        wf.attach_workflow(result)
        with wf.workflow_stage("iteration", "Attempt 1", kind="agent", iteration=1) as attempt:
            with wf.workflow_stage("render", "Render candidate", inputs={"api_key": "hidden"}) as render:
                render.update(artifact_hash="abc", token="hidden")
            attempt.update(decision="reject_keep_current")
            on_stage("DONE", "Retain original", {})
        return result

    result = run(tmp_path, lambda *event: events.append(event))
    parent, child = result.workflow_stages
    assert child.parent_id == parent.id
    assert child.iteration == 1
    assert child.weave_call_id is None and child.weave_url is None
    assert child.status == parent.status == "completed"
    assert child.duration_ms is not None and child.ended_at is not None
    assert child.inputs["api_key"] == child.summary["token"] == "[redacted]"
    assert parent.summary["decision"] == "reject_keep_current"
    assert events[-1][0] == "DONE"
    assert persisted[0][-1].status == "completed"
    assert wf.snapshot_workflow() == []


def test_parallel_nested_sessions_keep_their_own_records(monkeypatch):
    monkeypatch.setattr(weave, "get_client", lambda: None)
    barrier = Barrier(2)

    @wf.workflow_session
    def audit(label):
        with wf.workflow_stage("cold_judge", f"Audit {label}"):
            barrier.wait(timeout=5)
            with wf.workflow_stage("window", f"{label} 0–2s"):
                pass
        return SimpleNamespace(workflow_stages=[])

    @wf.workflow_session
    def session():
        with wf.workflow_stage("original", "Independent A/B audits"):
            with weave.ThreadPoolExecutor(max_workers=2) as pool:
                a, b = list(pool.map(audit, ["A", "B"]))
        return SimpleNamespace(workflow_stages=[], a=a, b=b)

    result = session()
    assert len(result.workflow_stages) == 5
    assert [s.title for s in result.a.workflow_stages] == ["Audit A", "A 0–2s"]
    assert [s.title for s in result.b.workflow_stages] == ["Audit B", "B 0–2s"]
    ids = {s.id for s in result.workflow_stages}
    assert all(s.parent_id in ids for s in result.workflow_stages if s.parent_id)
    assert wf.snapshot_workflow() == []


def test_exception_rethrows_and_closes_stage_without_secret_message(monkeypatch):
    monkeypatch.setattr(weave, "get_client", lambda: None)
    captured = []

    @wf.workflow_session
    def run(on_stage):
        with wf.workflow_stage("render", "Render"):
            raise ValueError("private token should stay out of stage metadata")

    with pytest.raises(ValueError, match="private token"):
        run(lambda *event: captured.append(event))
    stage = captured[-1][2]["stage"]
    assert stage["status"] == "failed"
    assert stage["ended_at"] is not None
    assert stage["summary"] == {"error_type": "ValueError"}
    assert wf.snapshot_workflow() == []


def test_weave_parentage_and_failed_finish_cannot_poison_next_stage(monkeypatch):
    calls = []
    root = SimpleNamespace(id="root")

    class Client:
        def create_call(self, op, inputs, display_name, attributes, use_stack):
            call = SimpleNamespace(id=str(len(calls)), ui_url=f"https://wandb.ai/test/call/{len(calls)}",
                                   parent_id=call_context.get_current_call().id, title=display_name)
            calls.append(call)
            call_context.push_call(call)
            return call

        def finish_call(self, call, output, exception):
            raise RuntimeError("offline transport")

    monkeypatch.setattr(weave, "get_client", lambda: Client())
    monkeypatch.setattr(wf, "_stage_op", lambda key, kind: key)
    with call_context.set_call_stack([root]):
        with wf.workflow_stage("iteration", "Attempt"):
            with wf.workflow_stage("render", "Render"):
                pass
            with wf.workflow_stage("verify", "Verify"):
                pass
        assert call_context.get_current_call() is root
    assert [(c.title, c.parent_id) for c in calls] == [("Attempt", "root"), ("Render", "0"), ("Verify", "0")]


def test_context_reset_does_not_leak_between_sessions(monkeypatch):
    monkeypatch.setattr(weave, "get_client", lambda: None)

    @wf.workflow_session
    def run():
        with wf.workflow_stage("ingest", "Read video"):
            pass
        return SimpleNamespace(workflow_stages=[])

    first, second = copy_context().run(run), copy_context().run(run)
    assert len(first.workflow_stages) == len(second.workflow_stages) == 1
    assert first.workflow_stages[0].id != second.workflow_stages[0].id
    assert second.workflow_stages[0].parent_id is None


def test_exception_persists_closed_stages_before_api_reloads_record(monkeypatch, tmp_path):
    monkeypatch.setattr(weave, "get_client", lambda: None)
    persisted = []

    @wf.workflow_session(persist=lambda result, data_dir: persisted.append(result.workflow_stages))
    def run(data_dir):
        record = SimpleNamespace(workflow_stages=[])
        wf.attach_workflow(record)
        with wf.workflow_stage("original", "Audit original", kind="agent"):
            raise RuntimeError("provider unavailable")

    with pytest.raises(RuntimeError, match="provider unavailable"):
        run(tmp_path)
    assert len(persisted) == 1
    assert persisted[0][0].status == "failed"
    assert persisted[0][0].ended_at is not None


def test_cancellation_at_stage_start_stops_work_and_persists_cancelled(monkeypatch, tmp_path):
    monkeypatch.setattr(weave, "get_client", lambda: None)
    persisted, events = [], []

    def on_stage(*event):
        events.append(event)
        raise JobCanceled("cancel requested")

    @wf.workflow_session(persist=lambda result, data_dir: persisted.append(result.workflow_stages))
    def run(data_dir, on_stage):
        record = SimpleNamespace(workflow_stages=[])
        wf.attach_workflow(record)
        with wf.workflow_stage("render", "Render candidate"):
            pytest.fail("Work must not begin after cancellation")

    with pytest.raises(JobCanceled):
        run(tmp_path, on_stage)
    assert persisted[0][0].status == "cancelled"
    assert events[-1][2]["stage"]["status"] == "cancelled"
    assert wf.snapshot_workflow() == []
