"""API path for the runtime loop: upload, start a run, execute it through the job worker, read the run, audit and repair
records back without local paths. Stage functions are scripted (see test_director); media probing and ffmpeg are real."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from directorloop.api.app import create_app
from directorloop.audit.models import AuditFinding, AuditReport, CoverageRecord, EvidenceFrame
from directorloop.config import Settings
from directorloop.media.probe import FFMPEG
from directorloop.runtime import director as D
from tests.unit.test_director import Script, bundle, finding, install


def _mp4(path: Path, video: bool = True) -> Path:
    inputs = (["-f", "lavfi", "-i", "testsrc2=s=180x320:d=3:r=30"] if video else []) + ["-f", "lavfi", "-i", "sine=frequency=300:duration=3"]
    codecs = (["-c:v", "libx264", "-pix_fmt", "yuv420p"] if video else []) + ["-c:a", "aac", "-shortest"]
    subprocess.run([FFMPEG, "-nostdin", "-loglevel", "error", "-y", *inputs, *codecs, str(path)], check=True, timeout=60)
    return path


@pytest.fixture
def app_client(tmp_path: Path) -> tuple[TestClient, Path]:
    data = tmp_path / "data"
    settings = Settings(_env_file=None, dl_data_dir=str(data), dl_weave_enabled=False)
    return TestClient(create_app(settings, start_worker=False)), data


def test_upload_validation_and_run_creation(app_client: tuple[TestClient, Path], tmp_path: Path) -> None:
    client, data = app_client
    with client:
        good = _mp4(tmp_path / "My clip!.mp4")
        r = client.post("/api/uploads", files={"file": ("My clip!.mp4", good.read_bytes(), "video/mp4")})
        assert r.status_code == 200, r.text
        up = r.json()
        assert up["video_id"].startswith("upl-") and up["title"] == "My clip" and up["duration_ms"] > 2500 and up["has_audio"] is True
        assert client.get(up["media_url"]).status_code == 200
        assert client.post("/api/uploads", files={"file": ("notes.txt", b"hello", "text/plain")}).status_code == 415
        assert client.post("/api/uploads", files={"file": ("broken.mp4", b"\x00" * 2048, "video/mp4")}).status_code == 422
        audio_only = _mp4(tmp_path / "voice.mp4", video=False)
        bad = client.post("/api/uploads", files={"file": ("voice.mp4", audio_only.read_bytes(), "video/mp4")})
        assert bad.status_code == 422 and "no video stream" in bad.json()["detail"]
        assert not list((data / "uploads").glob(".incoming_*")), "rejected uploads leave no temp files"

        body = {"video_id": up["video_id"], "objective": "keep viewers watching", "constraints": ["keep the ending"], "iteration_budget": 2, "idempotency_key": "k1"}
        started = client.post("/api/runs", json=body).json()
        assert started["created"] is True and started["run_id"] == "run_" + started["job_id"].removeprefix("job_")
        again = client.post("/api/runs", json=body).json()
        assert again["job_id"] == started["job_id"] and again["created"] is False
        assert client.post("/api/runs", json={**body, "iteration_budget": 3}).status_code == 409
        assert client.post("/api/runs", json={**body, "idempotency_key": "k2", "focus": "virality"}).status_code == 422
        assert client.post("/api/runs", json={**body, "idempotency_key": "k3", "allowed_edits": ["GENERATE_SHOT"]}).status_code == 422
        assert client.post("/api/runs", json={**body, "idempotency_key": "k4", "iteration_budget": 99}).status_code == 422
        assert client.post("/api/runs", json={**body, "idempotency_key": "k5", "video_id": "unknown-video"}).status_code == 404
        job = client.get(f"/api/jobs/{started['job_id']}").json()
        assert job["kind"] == "run" and job["run_id"] == started["run_id"] and job["state"] == "QUEUED"


def test_run_job_executes_through_the_worker_and_reads_back(app_client: tuple[TestClient, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, data = app_client
    s = Script(findings_by_version={"v0": [finding("f1")], "later_a": []}, choices=["remove_a", "later_a"], outcomes={"remove_a": "regression", "later_a": "improvement"})
    install(monkeypatch, tmp_path / "stages", s) if (tmp_path / "stages").mkdir() is None else None
    with client:
        up = client.post("/api/uploads", files={"file": ("clip.mp4", _mp4(tmp_path / "clip.mp4").read_bytes(), "video/mp4")}).json()
        services = client.app.state.services
        services.providers = bundle()
        started = client.post("/api/runs", json={"video_id": up["video_id"], "objective": "keep viewers watching", "iteration_budget": 3}).json()
        job = services.worker.run_once()
        assert job is not None and job.state == "COMPLETED", job.error if job else None
        events = client.get(f"/api/jobs/{started['job_id']}/events.json").json()
        stages = [e["stage"] for e in events]
        assert stages[0] == "STARTED" and "DECIDED" in stages and stages[-1] == "DONE"
        runs = client.get("/api/runs").json()
        assert runs[0]["id"] == started["run_id"] and runs[0]["launched_via"] == "api"
        assert runs[0]["decisions"] == ["reject_keep_current", "accept", "stop"]
        detail = client.get(f"/api/runs/{started['run_id']}").json()
        assert detail["status"] == "completed" and detail["mocked_stages"], "scripted providers are disclosed on the record"
        assert detail["iterations"][0]["next_action"] == "try_alternative"
        assert client.get("/api/runs/run_../../etc").status_code == 404
        rid = detail["iterations"][0]["repair_run_id"]
        rep = client.get(f"/api/repairs/{rid}").json()
        assert rep["status"] == "rejected" and "candidate_path" not in rep and "original_path" not in rep


def test_run_job_cancel_marks_the_run_record(app_client: tuple[TestClient, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, data = app_client
    s = Script(findings_by_version={"v0": [finding("f1")]}, choices=["remove_a"], outcomes={"remove_a": "regression"})
    (tmp_path / "stages").mkdir()
    install(monkeypatch, tmp_path / "stages", s)
    with client:
        up = client.post("/api/uploads", files={"file": ("clip.mp4", _mp4(tmp_path / "clip.mp4").read_bytes(), "video/mp4")}).json()
        services = client.app.state.services
        services.providers = bundle()
        started = client.post("/api/runs", json={"video_id": up["video_id"], "objective": "keep viewers watching"}).json()
        original = D.run_audit

        def audit_then_cancel(**kw):  # noqa: ANN003, ANN202
            services.jobs.request_cancel(started["job_id"])
            return original(**kw)

        monkeypatch.setattr(D, "run_audit", audit_then_cancel)
        job = services.worker.run_once()
        assert job is not None and job.state == "CANCELED"
        run = client.get(f"/api/runs/{started['run_id']}").json()
        assert run["status"] == "failed" and run["stop_reason"] == "the run was canceled"


def test_audit_view_serves_frames_without_local_paths(app_client: tuple[TestClient, Path]) -> None:
    client, data = app_client
    aid = "audit_1a0000000aa_0badf00d"
    frames = data / "audit" / aid / "frames"
    frames.mkdir(parents=True)
    (frames / "1500.jpg").write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
    f = AuditFinding(id=f"{aid}_f1", version_id="v0", start_ms=1000, end_ms=2000, weakness="w", evidence_frames=[EvidenceFrame(t_ms=1500, path=str(frames / "1500.jpg"))])
    rep = AuditReport(id=aid, video_id="x1", version_id="v0", artifact_hash="a" * 64, artifact_path="/private/somewhere/" + "a" * 64 + ".mp4", duration_ms=3000, created_at="t",
                      coverage=CoverageRecord(mode="sampled_frames_plus_asr", duration_ms=3000), findings=[f])
    (data / "audit" / aid / "audit.json").write_text(rep.model_dump_json())
    with client:
        body = client.get(f"/api/audits/{aid}").json()
        assert body["findings"][0]["evidence_frames"][0]["url"] == f"/media/audit/{aid}/1500.jpg"
        assert "/private/" not in json.dumps(body) and str(data) not in json.dumps(body)
        assert body["media_url"] is None, "an unregistered private path is not a render merely because its filename is a SHA"
        assert client.get(f"/media/audit/{aid}/1500.jpg").status_code == 200
        assert client.get(f"/media/audit/{aid}/..%2F..%2Faudit.json.jpg").status_code == 404
        assert client.get("/media/audit/not-an-audit/1500.jpg").status_code == 404
        assert client.get("/api/audits/audit_zz").status_code == 404
