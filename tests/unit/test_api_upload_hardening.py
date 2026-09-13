from __future__ import annotations

import asyncio
import hashlib
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from directorloop.api.app import create_app
from directorloop.config import Settings
from directorloop.domain.assets import StreamInfo
from directorloop.media.probe import FFMPEG


def app_for(tmp_path: Path, **options):
    return create_app(Settings(_env_file=None, dl_data_dir=str(tmp_path), dl_weave_enabled=False, **options), start_worker=False)


@pytest.mark.asyncio
async def test_concurrent_uploads_keep_separate_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = app_for(tmp_path)
    endpoint = next(r.endpoint for r in app.routes if getattr(r, "path", None) == "/api/uploads")
    barrier = asyncio.Barrier(2)

    class Upload:
        filename = "clip.mp4"

        def __init__(self, payload: bytes):
            self.payload = payload
            self.reads = 0

        async def read(self, count: int) -> bytes:
            self.reads += 1
            if self.reads == 1:
                await barrier.wait()
                return self.payload
            await asyncio.sleep(0)
            return b""

        async def close(self) -> None:
            pass

    monkeypatch.setattr("directorloop.media.probe.inspect_media", lambda path, **kw: StreamInfo(width=160, height=90, duration_ms=1000))
    payloads = [b"independent first video", b"independent second video"]
    results = await asyncio.gather(*(endpoint(Upload(payload)) for payload in payloads))
    for payload, result in zip(payloads, results, strict=True):
        digest = hashlib.sha256(payload).hexdigest()
        assert result["sha256"] == digest
        assert (tmp_path / "uploads" / f"{digest}.mp4").read_bytes() == payload
    assert len(app.state.services.uploads()) == 2
    assert not list((tmp_path / "uploads").glob(".incoming_*"))


def test_upload_honors_configured_size_limit(tmp_path: Path) -> None:
    with TestClient(app_for(tmp_path, dl_max_upload_mb=1)) as client:
        assert client.get("/api/health").json()["limits"]["upload_max_bytes"] == 1024 * 1024
        response = client.post("/api/uploads", files={"file": ("oversized.mp4", b"x" * (1024 * 1024 + 1), "video/mp4")})
        assert response.status_code == 413
        assert "1 MB" in response.json()["detail"]
    assert not list((tmp_path / "uploads").glob(".incoming_*"))


def test_rejected_probe_does_not_expose_error_details(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def unsafe_error(path, **kwargs):
        raise ValueError("internal /private/file key=DO_NOT_EXPOSE_TEST_TOKEN")

    monkeypatch.setattr("directorloop.media.probe.inspect_media", unsafe_error)
    with TestClient(app_for(tmp_path)) as client:
        response = client.post("/api/uploads", files={"file": ("broken.mp4", b"not video", "video/mp4")})
        assert response.status_code == 422
        assert "DO_NOT_EXPOSE" not in response.text
        assert "/private/" not in response.text
    assert not list((tmp_path / "uploads").glob(".incoming_*"))


def test_disguised_playlist_cannot_read_another_local_video(tmp_path: Path) -> None:
    segment = tmp_path / "unrelated.ts"
    subprocess.run([FFMPEG, "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=s=160x90:r=10:d=1",
                    "-c:v", "libx264", "-f", "mpegts", str(segment)], check=True, timeout=30)
    playlist = f"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:1\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:1.0,\n{segment.as_uri()}\n#EXT-X-ENDLIST\n"
    with TestClient(app_for(tmp_path / "data")) as client:
        response = client.post("/api/uploads", files={"file": ("disguised.mp4", playlist.encode(), "video/mp4")})
        assert response.status_code == 422
        assert client.app.state.services.uploads() == []
