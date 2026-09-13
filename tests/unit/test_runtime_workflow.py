"""Execution evidence survives decisions and stops without inventing downstream work."""

from pathlib import Path

import pytest

from directorloop.runtime import director as D
from tests.unit.test_director import Script, bundle, config, finding, install


def test_retry_hierarchy_keeps_rejected_and_accepted_attempts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    script = Script(findings_by_version={"v0": [finding("f1")], "later_a": []}, choices=["remove_a", "later_a"],
                    outcomes={"remove_a": "regression", "later_a": "improvement"})
    video, data = install(monkeypatch, tmp_path, script)
    events: list[tuple[str, object]] = []
    run = D.run_director(config(video, iteration_budget=3), bundle(), data,
                         on_stage=lambda name, message, payload: events.append((name, payload)))

    stages = run.workflow_stages
    by_id = {stage.id: stage for stage in stages}
    assert all(stage.parent_id is None or stage.parent_id in by_id for stage in stages)
    assert all(stage.status != "running" and stage.ended_at is not None for stage in stages)
    iterations = [stage for stage in stages if stage.key == "iteration"]
    assert [stage.summary["decision"] for stage in iterations] == ["reject_keep_current", "accept", "stop"]
    for iteration in iterations[:2]:
        children = [stage for stage in stages if stage.parent_id == iteration.id]
        assert [stage.key for stage in children] == ["prepare_current", "plan_repair", "render", "verify", "rejudge", "compare", "decision", "lesson"]
        assert all(stage.iteration == iteration.iteration for stage in children)
    # A justified stop is a completed planning decision; it has no invented edit.
    stop_children = [stage.key for stage in stages if stage.parent_id == iterations[2].id]
    assert stop_children == ["prepare_current", "plan_repair"]
    assert script.audits == ["v0", "remove_a", "later_a"]
    assert events[-1][0] == "DONE"
    assert any(name == "WORKFLOW_STAGE" for name, _ in events)
    persisted = D.load_run(data, run.id)
    assert persisted is not None
    assert [stage.model_dump() for stage in persisted.workflow_stages] == [stage.model_dump() for stage in stages]
    assert all(stage.weave_url is None for stage in stages), "offline tests must never invent remote trace URLs"


def test_failed_and_unverified_renders_have_no_fresh_judge_or_compare(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    script = Script(findings_by_version={"v0": [finding("f1")]}, choices=["remove_a", "later_a"],
                    render_fail={"remove_a"}, unverified={"later_a"})
    video, data = install(monkeypatch, tmp_path, script)
    run = D.run_director(config(video, iteration_budget=2), bundle(), data)

    first = [stage for stage in run.workflow_stages if stage.iteration == 1]
    second = [stage for stage in run.workflow_stages if stage.iteration == 2]
    assert next(stage for stage in first if stage.key == "render").status == "failed"
    assert not {"verify", "rejudge", "compare"}.intersection(stage.key for stage in first)
    assert next(stage for stage in second if stage.key == "verify").status == "incomplete"
    assert not {"rejudge", "compare"}.intersection(stage.key for stage in second)
    assert script.audits == ["v0"]
    assert all(record.decision == "incomplete_keep_current" for record in run.iterations)


def test_incomplete_original_stops_before_any_repair(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    script = Script(findings_by_version={"v0": [finding("f1")]}, choices=["remove_a"], incomplete_audit_versions={"v0"})
    video, data = install(monkeypatch, tmp_path, script)
    run = D.run_director(config(video), bundle(), data)

    assert [stage.key for stage in run.workflow_stages] == ["ingest", "original", "final_selection"]
    assert run.workflow_stages[1].status == "incomplete"
    assert not run.iterations and not script.selector_calls
    assert run.workflow_stages[-1].summary["stop_reason"] == run.stop_reason
