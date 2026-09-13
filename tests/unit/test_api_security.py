from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from directorloop.api.app import create_app
from directorloop.api.security import COOKIE_NAME, SESSION_SECONDS, SessionAuth
from directorloop.config import Settings

TOKEN = "unit-test-only-operator-token-123456789"


def settings_for(tmp_path: Path, **kwargs) -> Settings:
    return Settings(_env_file=None, dl_data_dir=str(tmp_path), dl_weave_enabled=False,
                    dl_local_auth_token=TOKEN, **kwargs)


def test_configured_token_protects_media_and_docs_and_supports_cookie_range(tmp_path: Path) -> None:
    renders = tmp_path / "renders"
    renders.mkdir()
    payload = b"private recorded artifact"
    (renders / ("a" * 64 + ".mp4")).write_bytes(payload)
    url = "/media/renders/" + "a" * 64 + ".mp4"
    with TestClient(create_app(settings_for(tmp_path), start_worker=False)) as client:
        for path in ("/api/health", "/api/docs", "/api/openapi.json", url, "/media/source/aptip.mp4"):
            assert client.get(path).status_code == 401
        assert client.post("/api/session", headers={"Authorization": "Bearer wrong"}).status_code == 401
        response = client.post("/api/session", headers={"Authorization": f"Bearer {TOKEN}"})
        assert response.status_code == 200
        assert response.json()["expires_in_seconds"] == SESSION_SECONDS
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "SameSite=strict" in response.headers["set-cookie"]
        assert TOKEN not in response.text and TOKEN not in response.headers["set-cookie"]
        assert client.get("/api/health").status_code == 200
        partial = client.get(url, headers={"Range": "bytes=0-6"})
        assert partial.status_code == 206 and partial.content == payload[:7]
        assert "private" in partial.headers["cache-control"]
        assert client.delete("/api/session", headers={"Origin": "http://testserver"}).status_code == 200
        assert client.get(url).status_code == 401


@pytest.mark.parametrize("origin", [None, "https://attacker.invalid", "http://testserver.attacker.invalid"])
def test_cookie_unsafe_requests_require_exact_origin(tmp_path: Path, origin: str | None) -> None:
    with TestClient(create_app(settings_for(tmp_path), start_worker=False)) as client:
        client.post("/api/session", headers={"Authorization": f"Bearer {TOKEN}"})
        headers = {"Origin": origin} if origin else {}
        response = client.post("/api/causal", json={"video_id": "does-not-exist"}, headers=headers)
        assert response.status_code == 403
        assert client.app.state.services.jobs.list() == []
        assert client.post("/api/causal", json={"video_id": "does-not-exist"}, headers={"Origin": "http://testserver"}).status_code == 404


def test_tampered_and_future_sessions_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = settings_for(tmp_path)
    auth = SessionAuth(settings)
    assert auth.signer is not None
    cookie = auth.signer.dumps({"scope": "directorloop"})
    with TestClient(create_app(settings, start_worker=False)) as client:
        client.cookies.set(COOKIE_NAME, cookie + "tampered")
        assert client.get("/api/health").status_code == 401
        client.cookies.set(COOKIE_NAME, cookie)
        monkeypatch.setattr("itsdangerous.timed.TimestampSigner.get_timestamp", lambda self: 0)
        # A token from the future also fails; signed timestamps are validated, not only signatures.
        assert client.get("/api/health").status_code == 401


def test_session_older_than_twelve_hours_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = settings_for(tmp_path)
    auth = SessionAuth(settings)
    assert auth.signer is not None
    with monkeypatch.context() as past:
        past.setattr("itsdangerous.timed.TimestampSigner.get_timestamp", lambda self: 1000)
        expired = auth.signer.dumps({"scope": "directorloop"})
    with TestClient(create_app(settings, start_worker=False)) as client:
        client.cookies.set(COOKIE_NAME, expired)
        assert client.get("/api/health").status_code == 401


@pytest.mark.parametrize("options", [
    {"dl_mode": "production"}, {"dl_bind_host": "0.0.0.0"}, {"dl_bind_host": "::"},
    {"dl_bind_host": "example.invalid"},
])
def test_public_startup_requires_strong_configured_auth(tmp_path: Path, options: dict) -> None:
    with pytest.raises(ValueError, match="at least 32"):
        create_app(Settings(_env_file=None, dl_data_dir=str(tmp_path), dl_local_auth_token="",
                            dl_weave_enabled=False, **options), start_worker=False)


def test_production_cookie_is_secure_and_private_host_override_is_forbidden(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, dl_mode="production", dl_bind_host="0.0.0.0")
    with TestClient(create_app(settings, start_worker=False), base_url="https://testserver") as client:
        response = client.post("/api/session", headers={"Authorization": f"Bearer {TOKEN}"})
        assert "; Secure" in response.headers["set-cookie"]
        assert client.get("/api/health").status_code == 200
    with pytest.raises(ValueError, match="Private-host"):
        create_app(settings_for(tmp_path / "bad", dl_mode="production", dl_ingest_allow_private_hosts=True), start_worker=False)


@pytest.mark.asyncio
async def test_chunked_oversized_body_stops_before_full_consumption(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, dl_max_upload_mb=1)
    app = create_app(settings, start_worker=False)
    consumed = []

    async def body():
        yield b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="clip.mp4"\r\nContent-Type: video/mp4\r\n\r\n'
        for index in range(4):
            consumed.append(index)
            yield b"x" * (512 * 1024)
        yield b"\r\n--boundary--\r\n"

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post("/api/uploads", content=body(), headers={
            "Authorization": f"Bearer {TOKEN}", "Content-Type": "multipart/form-data; boundary=boundary"})
    assert response.status_code == 413
    assert consumed == [0, 1, 2]
    assert not (tmp_path / "uploads" / "registry.json").exists()


def test_unauthenticated_body_is_rejected_before_parsing(tmp_path: Path) -> None:
    with TestClient(create_app(settings_for(tmp_path), start_worker=False)) as client:
        response = client.post("/api/uploads", content=b"not multipart", headers={"Content-Type": "multipart/form-data"})
        assert response.status_code == 401
        assert TOKEN not in json.dumps(response.json())


def test_unexpected_value_error_does_not_echo_private_contents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(settings_for(tmp_path), start_worker=False)

    def fail():
        raise ValueError("private field token=DO_NOT_EXPOSE_TEST_TOKEN")

    monkeypatch.setattr(app.state.services, "corpus", fail)
    with TestClient(app) as client:
        response = client.get("/api/health", headers={"Authorization": f"Bearer {TOKEN}"})
        assert response.status_code == 422
        assert "DO_NOT_EXPOSE" not in response.text


def test_production_disables_remote_ingestion_before_dns_or_jobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("production ingestion must not resolve or download a remote URL")

    monkeypatch.setattr("directorloop.ingest.url.check_host", forbidden)
    monkeypatch.setattr("directorloop.ingest.url.acquire", forbidden)
    app = create_app(settings_for(tmp_path, dl_mode="production"), start_worker=False)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    with TestClient(app) as client:
        health = client.get("/api/health", headers=headers).json()
        assert health["features"]["url_ingest"] is False
        assert "restricted network egress" in health["capability_notes"]["url_ingest"]
        response = client.post("/api/ingest/url", json={"url": "https://example.invalid/video.mp4"}, headers=headers)
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "url_ingest_unavailable"
        assert app.state.services.jobs.list() == []
        job, _ = app.state.services.jobs.create("ingest", {"url": "https://example.invalid/video.mp4"})
        with pytest.raises(ValueError, match="unavailable in production"):
            app.state.services.run_ingest_job(job, lambda *a: None)
