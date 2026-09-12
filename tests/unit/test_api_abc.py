"""A/B-to-C through the API: start a run from two uploaded edits, execute it with the job worker (scripted model stages,
real renders), read it back with media links and without local paths."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from directorloop.api.app import create_app
from directorloop.config import Settings
from tests.unit.test_abc import ScriptedPlanner, _clip, bundle, install


@pytest.fixture
def client_and_clips(tmp_path: Path) -> tuple[TestClient, tuple[Path, Path]]:
    settings = Settings(_env_file=None, dl_data_dir=str(tmp_path / "data"), dl_weave_enabled=False)
    return TestClient(create_app(settings, start_worker=False)), (_clip(tmp_path / "a.mp4", "testsrc2", 330), _clip(tmp_path / "b.mp4", "rgbtestsrc", 520))


def test_abc_api_end_to_end(client_and_clips: tuple[TestClient, tuple[Path, Path]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client, clips = client_and_clips
    with client:
        assert client.get("/api/health").json()["features"]["abc"] is True
        ids = []
        for path in clips:
            ids.append(client.post("/api/uploads", files={"file": (path.name, path.read_bytes(), "video/mp4")}).json()["video_id"])
        registry = {u["video_id"]: u["path"] for u in json.loads((tmp_path / "data" / "uploads" / "registry.json").read_text())}
        uploaded = (Path(registry[ids[0]]), Path(registry[ids[1]]))
        install(monkeypatch, uploaded, {"AB:whole": "A", "CA:whole:0": "C", "CB:whole:0": "same", "CA:region": "C"})
        services = client.app.state.services
        services.providers = bundle(ScriptedPlanner(["remove_a_u1"]))
        body = {"a_video_id": ids[0], "b_video_id": ids[1], "objective": "understand how the bridge was measured", "iteration_budget": 1}
        assert client.post("/api/abc", json={**body, "b_video_id": ids[0]}).status_code == 422
        assert client.post("/api/abc", json={**body, "b_video_id": "missing-video"}).status_code == 404
        assert client.post("/api/abc", json={**body, "iteration_budget": 9}).status_code == 422
        started = client.post("/api/abc", json=body).json()
        assert started["abc_id"] == "abc_" + started["job_id"].removeprefix("job_")
        assert client.get(f"/api/jobs/{started['job_id']}").json()["abc_id"] == started["abc_id"]
        job = services.worker.run_once()
        assert job is not None and job.state == "COMPLETED", job.error if job else None
        stages = [e["stage"] for e in client.get(f"/api/jobs/{started['job_id']}/events.json").json()]
        for st in ("STARTED", "PREPARING", "AUDITING", "AUDITS_DONE", "ALIGNING", "COMPARED_AB", "DIRECTING", "RENDERING_C", "VERIFYING_C", "REVIEWING_C", "JUDGING_C", "DECIDED_C"):
            assert st in stages, (st, stages)
        assert stages[-1] == "DONE"
        listed = client.get("/api/abc").json()
        assert listed[0]["id"] == started["abc_id"] and listed[0]["final_version"] == "C" and listed[0]["decisions"] == ["accept"]
        detail = client.get(f"/api/abc/{started['abc_id']}").json()
        text = json.dumps(detail)
        assert str(tmp_path) not in text, "no local paths reach the client"
        assert detail["versions"]["A"]["media_url"].startswith("/media/renders/") and detail["attempts"][0]["render_media_url"].startswith("/media/renders/")
        assert client.get(detail["attempts"][0]["render_media_url"]).status_code == 200
        assert detail["rubric"]["sha256"] and detail["mocked_stages"], "scripted providers are disclosed"
        assert client.get("/api/abc/abc_../../x").status_code == 404
