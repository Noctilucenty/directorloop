"""The causal HTTP contract executes the real runner with disclosed scripted model/media stages.

Request validation, durable jobs, cancellation, failure status and hash-bound media serving are real.
No network or model calls. Live routes never select these scripted stages.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from directorloop.api.app import create_app
from directorloop.config import Settings
from directorloop.domain.ids import sha256_file
from directorloop.runtime import causal as C
from tests.unit.test_api_runs import _mp4
from tests.unit.test_causal_planner import World, bundle, install


@pytest.fixture
def causal_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    world = World(outcomes={"TRIM_PAUSE": "improvement", "PUNCH_IN": "regression"})
    videos = install(monkeypatch, tmp_path / "stages", world)
    settings = Settings(_env_file=None, dl_data_dir=str(tmp_path / "data"), dl_weave_enabled=False)
    with TestClient(create_app(settings, start_worker=False)) as client:
        services = client.app.state.services
        services.providers = bundle()
        services.register_upload({"video_id": "video-a", "path": str(videos["a"]), "title": "Source", "source": "upload"})
        yield client, services, videos, world


def test_causal_upload_request_and_idempotency(causal_client, tmp_path: Path) -> None:  # noqa: ANN001
    client, services, _, _ = causal_client
    up = client.post("/api/uploads", files={"file": ("clip.mp4", _mp4(tmp_path / "clip.mp4").read_bytes(), "video/mp4")}).json()
    body = {"video_id": up["video_id"], "objective": "Keep the explanation understandable", "idempotency_key": "causal-1"}
    response = client.post("/api/causal", json=body)
    assert response.status_code == 200, response.text
    created = response.json()
    assert created["causal_id"] == "causal_" + created["job_id"].removeprefix("job_")
    assert client.get("/api/health").json()["features"]["causal"] is True
    assert client.get(f"/api/jobs/{created['job_id']}").json()["causal_id"] == created["causal_id"]
    assert services.jobs.get(created["job_id"]).params["audit_id"] is None, "default execution performs a fresh audit"
    assert client.post("/api/causal", json=body).json() == {**created, "created": False}
    assert client.post("/api/causal", json={**body, "arms": 1}).status_code == 409
    for patch in ({"arms": 4}, {"max_model_calls": 1}, {"deadline_s": 1}, {"audit_id": "../../etc"}, {"video_path": "/tmp/another.mp4"}):
        assert client.post("/api/causal", json={**body, **patch}).status_code == 422
    assert client.post("/api/causal", json={**body, "video_id": "not-registered"}).status_code == 404
    assert client.post("/api/causal", json={**body, "audit_id": "audit_aabb_ccdd"}).status_code == 404


def test_causal_edit_rights_and_plan_only(causal_client) -> None:  # noqa: ANN001
    client, services, videos, _ = causal_client
    services.register_upload({"video_id": "linked-video", "path": str(videos["a"]), "title": "Link", "source": "url", "edit_permission": "requires_owner_confirmation"})
    body = {"video_id": "linked-video"}
    assert client.post("/api/causal", json=body).status_code == 403
    assert client.post("/api/causal", json={**body, "owner_confirms_rights": True}).status_code == 200
    assert client.post("/api/causal", json={**body, "plan_only": True}).status_code == 200


def test_causal_job_executes_runner_and_exposes_truthful_records(causal_client) -> None:  # noqa: ANN001
    client, services, videos, world = causal_client
    started = client.post("/api/causal", json={"video_id": "video-a", "constraints": ["Preserve the product name"]}).json()
    job = services.worker.run_once()
    assert job is not None and job.state == "COMPLETED", job.error if job else None
    detail = client.get(f"/api/causal/{started['causal_id']}").json()
    assert detail["status"] == "completed" and detail["launched_via"] == "api"
    assert detail["mocked_stages"] and detail["audit_source"] == "fresh"
    assert [a["verdict"] for a in detail["arms"]] == ["win", "loss"]
    assert world.renders == world.compares == ["TRIM_PAUSE", "PUNCH_IN"]
    assert len(detail["policy_records_added"]) == 2 and detail["plan_inputs"]["policy_sha256"]
    assert detail["plan"]["policy_effect"]["selection_changed"] is False
    assert "video_path" not in detail["config"] and all("render_path" not in a for a in detail["arms"])
    assert str(services.data.parent) not in json.dumps(detail)
    stages = detail["workflow_stages"]
    assert stages and all(s["status"] != "running" and s["ended_at"] for s in stages)
    events = client.get(f"/api/jobs/{started['job_id']}/events.json").json()
    assert any(e["stage"] == "WORKFLOW_STAGE" for e in events) and events[-1]["stage"] == "DONE"
    assert client.get(detail["original_media_url"]).content == videos["a"].read_bytes()
    listed = client.get("/api/causal?video_id=video-a").json()
    assert listed[0]["id"] == started["causal_id"] and listed[0]["verdicts"] == ["win", "loss"]
    assert client.get("/api/causal?video_id=video-b").json() == []


def test_causal_changed_source_is_not_served_as_original(causal_client) -> None:  # noqa: ANN001
    client, services, videos, _ = causal_client
    started = client.post("/api/causal", json={"video_id": "video-a", "plan_only": True}).json()
    services.worker.run_once()
    detail = client.get(f"/api/causal/{started['causal_id']}").json()
    assert detail["arms"] == [] and detail["config"]["plan_only"] is True
    videos["a"].write_bytes(b"different footage")
    assert client.get(detail["original_media_url"]).status_code == 409
    assert client.get("/api/causal/causal_../../etc").status_code == 404
    assert client.get("/media/causal/not-a-run/original.mp4").status_code == 404


def test_causal_runtime_failure_marks_both_job_and_run(causal_client, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    client, services, _, _ = causal_client
    secret = "private-runtime-exception-value"

    def failed_audit(**kw):  # noqa: ANN003, ANN202
        raise RuntimeError(f"provider unavailable Authorization: Bearer {secret}")

    monkeypatch.setattr(C, "run_audit", failed_audit)
    started = client.post("/api/causal", json={"video_id": "video-a"}).json()
    job = services.worker.run_once()
    assert job is not None and job.state == "FAILED"
    run = client.get(f"/api/causal/{started['causal_id']}").json()
    assert run["status"] == "failed" and "RuntimeError" in run["error"]
    persisted = (services.data / "causal" / f"{started['causal_id']}.json").read_text()
    events = client.get(f"/api/jobs/{started['job_id']}/events").text
    assert secret not in json.dumps(run) + persisted + events + (job.error or "")


@pytest.mark.parametrize("kind", ["run", "abc", "causal"])
def test_api_early_exception_closes_artifact_without_leaking_details(causal_client, monkeypatch: pytest.MonkeyPatch, kind: str):  # noqa: ANN001
    from directorloop.api import app as A
    from directorloop.compare.models import ABCRun
    from directorloop.domain.ids import utc_now_iso
    from directorloop.runtime import abc as B
    from directorloop.runtime import director as D

    client, services, videos, _ = causal_client
    secret = "private-early-failure-credential"
    services.register_upload({"video_id": "video-b", "path": str(videos["b"]), "title": "Other source", "source": "upload"})

    def interrupted(config, providers, data_dir, *, run_id=None, abc_id=None, **kwargs):  # noqa: ANN001, ANN003
        run_id = run_id or abc_id
        if kind == "run":
            D.save_run(D.DirectorRun(id=run_id, config=config, created_at=utc_now_iso(), original_path=config.video_path), data_dir)
        elif kind == "abc":
            B.save_abc(ABCRun(id=run_id, context=config.context, created_at=utc_now_iso()), data_dir)
        else:
            C.save_causal(C.CausalRun(id=run_id, config=config, created_at=utc_now_iso()), data_dir)
        raise RuntimeError(f"https://provider.invalid/file?signature={secret}")

    monkeypatch.setattr(A, {"run": "run_director", "abc": "run_abc", "causal": "run_causal"}[kind], interrupted)
    endpoint = "runs" if kind == "run" else kind
    body = {"a_video_id": "video-a", "b_video_id": "video-b", "objective": "keep the meaning"} if kind == "abc" else {"video_id": "video-a", "objective": "keep the meaning"}
    started = client.post(f"/api/{endpoint}", json=body).json()
    job = services.worker.run_once()
    rid = started[f"{kind}_id"]
    detail = client.get(f"/api/{endpoint}/{rid}")
    assert detail.status_code == 200 and detail.json()["status"] == "failed"
    assert job.state == "FAILED"
    persisted = (services.data / endpoint / f"{rid}.json").read_text()
    events = client.get(f"/api/jobs/{job.id}/events").text
    assert secret not in detail.text + persisted + events + (job.error or "")
    assert "RuntimeError" in detail.json()["error"]


def test_causal_render_exception_does_not_enter_arm_or_workflow(causal_client, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN001
    client, services, _, _ = causal_client
    secret = "private-render-error-value"

    def broken_render(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError(f"render token={secret}")

    monkeypatch.setattr(C, "render_candidate", broken_render)
    started = client.post("/api/causal", json={"video_id": "video-a", "arms": 1}).json()
    services.worker.run_once()
    detail = client.get(f"/api/causal/{started['causal_id']}")
    persisted = (services.data / "causal" / f"{started['causal_id']}.json").read_text()
    events = client.get(f"/api/jobs/{started['job_id']}/events").text
    assert detail.json()["arms"][0]["verdict"] == "incomplete"
    assert secret not in detail.text + persisted + events


def test_causal_cancellation_marks_both_job_and_run(causal_client, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    client, services, _, _ = causal_client
    original = C.run_audit
    started = client.post("/api/causal", json={"video_id": "video-a"}).json()

    def cancel_during_audit(**kw):  # noqa: ANN003, ANN202
        services.jobs.request_cancel(started["job_id"])
        return original(**kw)

    monkeypatch.setattr(C, "run_audit", cancel_during_audit)
    job = services.worker.run_once()
    assert job is not None and job.state == "CANCELED"
    run = client.get(f"/api/causal/{started['causal_id']}").json()
    assert run["status"] == "cancelled" and run["ended_at"]
    assert all(s["status"] != "running" for s in run["workflow_stages"])


def test_causal_render_media_url_uses_hash_store(causal_client) -> None:  # noqa: ANN001
    client, services, _, _ = causal_client
    started = client.post("/api/causal", json={"video_id": "video-a"}).json()
    services.worker.run_once()
    run = C.load_causal(services.data, started["causal_id"])
    assert run is not None
    arm = run.arms[0]
    render = Path(arm.render_path)
    canonical = services.data / "renders" / f"{sha256_file(render)}.mp4"
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(render.read_bytes())
    arm.render_path = str(canonical)
    C.save_causal(run, services.data)
    detail = client.get(f"/api/causal/{run.id}").json()
    url = detail["arms"][0]["render_media_url"]
    assert url == f"/media/renders/{arm.render_hash}.mp4"
    assert client.get(url).content == render.read_bytes()
