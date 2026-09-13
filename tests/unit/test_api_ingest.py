"""Link ingestion, the audit-only Judge job, and the edit-rights gate.

An in-process HTTP transport serves real media; the platform path uses a fake
yt-dlp runner. No network, no model calls.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from directorloop.api.app import create_app
from directorloop.audit.models import AuditReport, CoverageRecord
from directorloop.config import Settings
from directorloop.ingest import url as U
from directorloop.media.probe import FFMPEG


def _mp4(path: Path) -> Path:
    subprocess.run([FFMPEG, "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=180x320:d=3:r=30", "-f", "lavfi", "-i", "sine=frequency=300:duration=3",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)], check=True, timeout=60)
    return path


@pytest.fixture(scope="module")
def web(tmp_path_factory: pytest.TempPathFactory) -> Any:
    root = tmp_path_factory.mktemp("web")
    _mp4(root / "clip.mp4")
    (root / "notes.mp4").write_text("this is not a video")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "127.0.0.1" and request.url.port == 18080
        if request.url.path in {"/clip.mp4", "/notes.mp4"}:
            return httpx.Response(200, content=(root / request.url.path[1:]).read_bytes(),
                                  headers={"Content-Type": "video/mp4"})
        return httpx.Response(404, content=b"File not found")

    def client(*args, **kwargs):
        return httpx.Client(*args, **kwargs, transport=httpx.MockTransport(handler))

    # Replace only the ingestion module's client, leaving TestClient and the
    # global offline socket guard intact.
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(U, "httpx", SimpleNamespace(Client=client, Timeout=httpx.Timeout))
        yield "http://127.0.0.1:18080"


def test_classify_and_host_checks() -> None:
    assert U.classify_url("https://www.instagram.com/reel/abc/")["platform"] == "instagram"
    assert U.classify_url("https://youtu.be/xyz")["platform"] == "youtube"
    assert U.classify_url("https://cdn.example.com/a/b/clip.MOV")["kind"] == "direct"
    assert U.classify_url("https://example.com/page")["kind"] == "unknown"
    for bad in ("ftp://example.com/clip.mp4", "https://user:pass@example.com/clip.mp4", "file:///etc/passwd", "not a url"):
        with pytest.raises(U.IngestError):
            U.classify_url(bad)
    for host in ("127.0.0.1", "localhost", "10.0.0.5", "169.254.169.254", "::1"):
        with pytest.raises(U.IngestError):
            U.check_host(host, None)
    U.check_host("127.0.0.1", 8000, allow_private=True)


def _client(tmp_path: Path, allow_private: bool) -> TestClient:
    settings = Settings(_env_file=None, dl_data_dir=str(tmp_path / "data"), dl_weave_enabled=False, dl_ingest_allow_private_hosts=allow_private)
    return TestClient(create_app(settings, start_worker=False))


def test_direct_link_ingest_and_edit_gate(tmp_path: Path, web: str) -> None:
    client = _client(tmp_path, allow_private=True)
    with client:
        assert client.get("/api/health").json()["features"]["url_ingest"] is True
        started = client.post("/api/ingest/url", json={"url": f"{web}/clip.mp4"}).json()
        assert started["kind"] == "direct" and started["edit_permission"] == "requires_owner_confirmation"
        job = client.app.state.services.worker.run_once()
        assert job is not None and job.state == "COMPLETED", job.error if job else None
        vid = job.result["video_id"]
        stages = [e["stage"] for e in client.get(f"/api/jobs/{job.id}/events.json").json()]
        assert stages[:4] == ["RESOLVING", "DOWNLOADING", "PROBING", "REGISTERED"]
        video = next(v for v in client.get("/api/videos").json() if v["video_id"] == vid)
        assert video["source"] == "url" and video["role"] == "link" and video["edit_permission"] == "requires_owner_confirmation"
        assert client.get(video["media_url"]).status_code == 200
        body = {"video_id": vid, "objective": "keep viewers watching"}
        blocked = client.post("/api/runs", json=body)
        assert blocked.status_code == 403 and "owner_confirms_rights" in blocked.json()["detail"]
        assert client.post("/api/runs", json={**body, "owner_confirms_rights": True}).status_code == 200
        judged = client.post("/api/audits", json={"video_id": vid})
        assert judged.status_code == 200, "judging a linked video never needs edit rights"
        assert not list((tmp_path / "data" / "uploads" / "incoming").glob("incoming_*")), "no partial downloads are left behind"


def test_link_failures_are_explicit(tmp_path: Path, web: str) -> None:
    locked = _client(tmp_path / "locked", allow_private=False)
    with locked:
        r = locked.post("/api/ingest/url", json={"url": f"{web}/clip.mp4"})
        assert r.status_code == 422 and "standard web ports" in r.json()["detail"], "odd ports are refused"
        r = locked.post("/api/ingest/url", json={"url": "http://127.0.0.1/clip.mp4"})
        assert r.status_code == 422 and "private or local" in r.json()["detail"], "the server refuses to fetch from its own network"
        r = locked.post("/api/ingest/url", json={"url": "http://169.254.169.254/latest/clip.mp4"})
        assert r.status_code == 422 and "private or local" in r.json()["detail"]
        assert locked.post("/api/ingest/url", json={"url": "https://example.com/about"}).status_code == 422
    client = _client(tmp_path / "open", allow_private=True)
    with client:
        client.post("/api/ingest/url", json={"url": f"{web}/notes.mp4"})
        job = client.app.state.services.worker.run_once()
        assert job is not None and job.state == "FAILED" and "video" in (job.error or "")
        client.post("/api/ingest/url", json={"url": f"{web}/missing.mp4"})
        job = client.app.state.services.worker.run_once()
        assert job is not None and job.state == "FAILED" and "404" in (job.error or "")


def test_platform_link_uses_ytdlp_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _mp4(tmp_path / "source.mp4")
    calls: list[list[str]] = []

    def fake_runner(argv: list[str], **kw: Any) -> subprocess.CompletedProcess:
        calls.append(argv)
        out_tmpl = Path(argv[argv.index("-o") + 1])
        target = out_tmpl.parent / "reel123.mp4"
        target.write_bytes(source.read_bytes())
        meta = {"id": "reel123", "title": "A reel about Smoot!", "uploader": "someone", "webpage_url": argv[-1], "requested_downloads": [{"filepath": str(target)}]}
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(meta), stderr="")

    monkeypatch.setattr(U.shutil, "which", lambda name: str(source))  # any existing file stands in for the executable
    got = U.fetch_platform("https://www.instagram.com/reel/reel123/", "instagram", tmp_path / "incoming", runner=fake_runner)
    assert got.kind == "platform" and got.platform == "instagram" and got.title == "A reel about Smoot" and got.uploader == "someone"
    assert "--no-playlist" in calls[0] and "--max-filesize" in calls[0] and got.path.exists()

    def failing(argv: list[str], **kw: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="ERROR: [Instagram] login required")

    with pytest.raises(U.IngestError, match="login required"):
        U.fetch_platform("https://www.instagram.com/reel/x/", "instagram", tmp_path / "incoming", runner=failing)


def test_audit_job_runs_through_the_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import directorloop.api.app as A

    client = _client(tmp_path, allow_private=False)
    with client:
        clip = _mp4(tmp_path / "judge.mp4")
        vid = client.post("/api/uploads", files={"file": ("judge.mp4", clip.read_bytes(), "video/mp4")}).json()["video_id"]

        def fake_audit(*, video_id: str, video_path: Path, version_id: str, provider: Any, data_dir: Path, on_stage: Any = None, closeups: bool = True) -> AuditReport:
            rep = AuditReport(id="audit_1a0000000bb_00000001", video_id=video_id, version_id=version_id, artifact_hash="h", artifact_path=str(video_path), duration_ms=3000,
                              created_at="t", coverage=CoverageRecord(mode="sampled_frames_plus_asr", duration_ms=3000))
            (data_dir / "audit" / rep.id).mkdir(parents=True, exist_ok=True)
            (data_dir / "audit" / rep.id / "audit.json").write_text(rep.model_dump_json())
            if on_stage:
                on_stage("AUDIT_DONE", "done", {"audit_id": rep.id})
            return rep

        monkeypatch.setattr(A, "run_audit", fake_audit)
        started = client.post("/api/audits", json={"video_id": vid}).json()
        job = client.app.state.services.worker.run_once()
        assert job is not None and job.state == "COMPLETED" and job.result["audit_id"] == "audit_1a0000000bb_00000001"
        assert client.get(f"/api/audits?video_id={vid}").json()[0]["id"] == "audit_1a0000000bb_00000001"
        assert started["job_id"] == job.id
