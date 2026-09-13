"""A SHA filename does not identify its storage location: uploaded sources and renders stay distinct."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from directorloop.api.app import create_app
from directorloop.audit.models import AuditReport, CoverageRecord
from directorloop.compare.models import ABCRun, DeclaredContext, VersionRef
from directorloop.config import Settings
from directorloop.runtime.abc import save_abc
from directorloop.runtime.director import DirectorRun, RunConfig, save_run
from tests.unit.test_api_runs import _mp4


def test_uploaded_audit_run_and_abc_return_playable_source_urls(tmp_path: Path) -> None:
    data = tmp_path / "data"
    settings = Settings(_env_file=None, dl_data_dir=str(data), dl_weave_enabled=False)
    with TestClient(create_app(settings, start_worker=False)) as client:
        clip = _mp4(tmp_path / "source.mp4").read_bytes()
        up = client.post("/api/uploads", files={"file": ("source.mp4", clip, "video/mp4")}).json()
        source = client.app.state.services.video(up["video_id"])["path"]
        aid = "audit_aabb_ccdd"
        report = AuditReport(id=aid, video_id=up["video_id"], version_id="v0", artifact_hash=up["sha256"], artifact_path=source,
                             duration_ms=up["duration_ms"], created_at="t", coverage=CoverageRecord(mode="sampled_frames_plus_asr", duration_ms=up["duration_ms"]))
        audit_dir = data / "audit" / aid
        audit_dir.mkdir(parents=True)
        (audit_dir / "audit.json").write_text(report.model_dump_json())
        audit = client.get(f"/api/audits/{aid}").json()
        assert audit["media_url"] == up["media_url"] and "/renders/" not in audit["media_url"]
        played = client.get(audit["media_url"])
        assert played.status_code == 200 and played.content == clip
        partial = client.get(audit["media_url"], headers={"Range": "bytes=0-31"})
        assert partial.status_code == 206 and partial.content == clip[:32]
        assert partial.headers["content-range"] == f"bytes 0-31/{len(clip)}"

        run = DirectorRun(id="run_aabb_ccdd", config=RunConfig(video_id=up["video_id"], video_path=source, objective="Understand the clip"),
                          original_path=source, original_artifact_hash=up["sha256"], final_path=source, final_artifact_hash=up["sha256"], created_at="t")
        save_run(run, data)
        view = client.get(f"/api/runs/{run.id}").json()
        assert view["original_media_url"] == view["final_media_url"] == up["media_url"]

        abc = ABCRun(id="abc_aabb_ccdd", context=DeclaredContext(objective="Understand the clip"), created_at="t",
                     versions={"A": VersionRef(label="A", video_id=up["video_id"], evaluated_path=source, evaluated_hash=up["sha256"], duration_ms=up["duration_ms"])})
        save_abc(abc, data)
        assert client.get(f"/api/abc/{abc.id}").json()["versions"]["A"]["media_url"] == up["media_url"]

        # Changed source bytes must not be represented as the exact analyzed artifact.
        Path(source).write_bytes(b"replaced after audit")
        assert client.get(f"/api/audits/{aid}").json()["media_url"] is None


def test_unregistered_same_basename_does_not_alias_a_render(tmp_path: Path) -> None:
    data = tmp_path / "data"
    sha = "a" * 64
    (data / "renders").mkdir(parents=True)
    (data / "renders" / f"{sha}.mp4").write_bytes(b"a different artifact")
    outside = tmp_path / "unregistered" / f"{sha}.mp4"
    outside.parent.mkdir()
    outside.write_bytes(b"private source with a SHA-shaped filename")
    aid = "audit_abcd_1234"
    audit = AuditReport(id=aid, video_id="not-registered", version_id="v0", artifact_hash=sha, artifact_path=str(outside), duration_ms=1000, created_at="t",
                        coverage=CoverageRecord(mode="sampled_frames_plus_asr", duration_ms=1000))
    folder = data / "audit" / aid
    folder.mkdir(parents=True)
    (folder / "audit.json").write_text(audit.model_dump_json())
    settings = Settings(_env_file=None, dl_data_dir=str(data), dl_weave_enabled=False)
    with TestClient(create_app(settings, start_worker=False)) as client:
        body = client.get(f"/api/audits/{aid}").json()
        assert body["media_url"] is None
        assert str(outside) not in json.dumps(body)
