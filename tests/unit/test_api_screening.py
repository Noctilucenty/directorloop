"""Screening API contracts and cost-free browsing, with disclosed offline engine fixtures."""
from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from directorloop.api import app as A
from directorloop.config import Settings
from directorloop.creative.genome import GENOME_VERSION
from directorloop.domain.creative import BeatRole
from directorloop.domain.ids import sha256_file, utc_now_iso
from directorloop.jobs.recovery import reconcile_interrupted_job
from directorloop.providers.registry import ProviderBundle
from directorloop.providers.spend import SpendGuardError, price_card
from directorloop.screening import runner as R
from directorloop.screening.models import ScreenFrame, ScreenReport, ScreenWindow
from tests.unit.test_creative_mutations import make_genome


@pytest.fixture
def screening_client(tmp_path, monkeypatch):
    captured = []

    def provider(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(**kwargs, capability=SimpleNamespace(name=kwargs["name"], model=kwargs["model"]))

    monkeypatch.setattr(A, "OpenAICompatProvider", provider)
    monkeypatch.setattr(A, "build_providers", lambda settings: ProviderBundle(None, None))
    monkeypatch.setattr(A, "init_weave", lambda settings: None)
    monkeypatch.setattr(A, "weave_status", lambda: SimpleNamespace(connected=True, project="offline", traces_url=None, reason=None))
    settings = Settings(_env_file=None, dl_data_dir=str(tmp_path / "data"), dl_weave_enabled=False,
                        dl_screening_enabled=True, wandb_api_key="offline-private-key", openai_api_key="", gemini_api_key="")
    app = A.create_app(settings, start_worker=False)
    services = app.state.services
    source = tmp_path / "source.mp4"
    source.write_bytes(b"offline source fixture")
    services.register_upload({"video_id": "source-one", "path": str(source), "title": "Source", "source": "upload", "duration_ms": 7000})

    def run_screening(video_id, path, provider, data_dir, *, screening_id, on_stage, is_cancelled):
        frame_dir = data_dir / "screenings" / screening_id / "frames"
        frame_dir.mkdir(parents=True)
        frame_path = frame_dir / "000000_448.jpg"
        frame_path.write_bytes(b"offline frame fixture")
        report = ScreenReport(id=screening_id, video_id=video_id, artifact_path=str(path), artifact_hash=sha256_file(path),
                              duration_ms=7000, created_at=utc_now_iso(), status="complete", ended_at=utc_now_iso(),
                              windows=[ScreenWindow(start_ms=0, end_ms=2000, status="complete",
                                                    evidence_frames=[ScreenFrame(t_ms=0, path=str(frame_path), sha256=sha256_file(frame_path), width=448, height=800)])])
        R.save_screening(data_dir, report)
        on_stage("DONE", "Offline screening fixture saved", {"screen_id": screening_id})
        return report

    monkeypatch.setattr(R, "run_screening", run_screening)
    with TestClient(app) as client:
        yield client, services, source, captured


def test_screening_provider_is_separate_guarded_and_never_changes_final_bundle(screening_client):
    client, services, _, captured = screening_client
    assert services.providers.probe is None and services.providers.planner is None
    assert len(captured) == 1
    config = captured[0]
    assert config["model"] == "Qwen/Qwen3.8-27B" and config["enable_thinking"] is False
    assert config["allow_compatibility_fallback"] is False and config["max_output_tokens"] == 2048
    assert config["spend_guard"].ledger.summary()["limit_usd"] == 2
    assert config["spend_guard"].ledger.summary()["max_physical_attempts"] == 30
    health = client.get("/api/health").json()
    assert health["features"]["screening"] is True and health["screening"]["available"] is True
    assert health["screening"]["no_automatic_edit"] is True
    assert "offline-private-key" not in json.dumps(health)


def test_screening_idempotency_job_and_saved_evidence(screening_client):
    client, services, source, _ = screening_client
    body = {"video_id": "source-one", "idempotency_key": "screen-once"}
    started = client.post("/api/screenings", json=body)
    assert started.status_code == 200
    created = started.json()
    assert created["screen_id"] == "screen_" + created["job_id"].removeprefix("job_")
    assert client.get(f"/api/jobs/{created['job_id']}").json()["screen_id"] == created["screen_id"]
    assert services.jobs.get(created["job_id"]).kind == "screening"
    assert client.post("/api/screenings", json=body).json() == {**created, "created": False}
    assert client.post("/api/screenings", json={**body, "video_id": "other-source"}).status_code == 409
    job = services.worker.run_once()
    assert job.state == "COMPLETED"
    result = client.get(f"/api/screenings/{created['screen_id']}").json()
    assert result["status"] == "complete" and result["review_required"] is True
    assert result["semantic_grounding_verified"] is False and result["automatic_edit_allowed"] is False
    assert result["evidence_label"] == "screen_model_judgment"
    assert "artifact_path" not in result and str(source.parent) not in json.dumps(result)
    assert client.get(result["media_url"]).content == source.read_bytes()
    frame = result["windows"][0]["evidence_frames"][0]
    assert "path" not in frame
    response = client.get(frame["media_url"], headers={"Range": "bytes=0-3"})
    assert response.status_code == 206 and response.content == b"offl"
    assert client.get("/api/screenings?video_id=source-one").json()[0]["id"] == created["screen_id"]
    assert client.get("/api/screenings?video_id=other-source").json() == []
    assert client.get(f"/api/jobs/{job.id}/events.json").json()[-1]["stage"] == "DONE"
    assert "event:" in client.get(f"/api/jobs/{job.id}/events").text
    assert not (services.data / "audit" / created["screen_id"]).exists()
    assert client.post("/api/causal", json={"video_id": "source-one", "audit_id": created["screen_id"]}).status_code == 422


def test_screening_rejects_request_overrides_before_queueing(screening_client):
    client, services, _, _ = screening_client
    for addition in ({"model": "other"}, {"spend_cap": 100}, {"path": "/tmp/video.mp4"}, {"arms": 2}, {"enable_thinking": True}):
        response = client.post("/api/screenings", json={"video_id": "source-one", **addition})
        assert response.status_code == 422
    assert client.post("/api/screenings", json={"video_id": "not-registered"}).status_code == 404
    assert services.jobs.list() == []


def test_screening_budget_exhaustion_blocks_new_jobs_but_not_idempotent_reads(screening_client):
    client, services, _, _ = screening_client
    body = {"video_id": "source-one", "idempotency_key": "already-queued"}
    created = client.post("/api/screenings", json=body).json()
    ledger = services.screening_ledger
    card = price_card(A.WANDB_INFERENCE_BASE_URL, A.SCREENING_MODEL)
    for _ in range(30):
        reservation = ledger.reserve(card, 2048)
        ledger.settle(reservation, 1, 1)
    before = ledger.summary()
    for _ in range(3):
        status = client.get("/api/health").json()["screening"]
        assert status["available"] is False and "headroom" in status["reason"]
    assert ledger.summary() == before, "health does not reset or reserve budget"
    assert client.post("/api/screenings", json={"video_id": "source-one"}).status_code == 503
    assert len(services.jobs.list()) == 1
    assert client.post("/api/screenings", json=body).json() == {**created, "created": False}
    job = services.worker.run_once()
    assert job.state == "FAILED" and ledger.summary() == before, "worker rechecks readiness without a paid request"


def test_screening_stale_price_and_missing_tracing_fail_preflight(screening_client, monkeypatch):
    client, services, _, _ = screening_client
    monkeypatch.setattr(A, "weave_status", lambda: SimpleNamespace(connected=False, project="offline", traces_url=None, reason="offline"))
    assert client.post("/api/screenings", json={"video_id": "source-one"}).status_code == 503
    monkeypatch.setattr(A, "price_card", lambda *_: (_ for _ in ()).throw(SpendGuardError("stale pricing")))
    assert client.post("/api/screenings", json={"video_id": "source-one"}).status_code == 503
    assert services.jobs.list() == []


def test_disabled_screening_never_creates_a_ledger(tmp_path):
    settings = Settings(_env_file=None, dl_data_dir=str(tmp_path / "data"), dl_weave_enabled=False,
                        wandb_api_key="", openai_api_key="", gemini_api_key="")
    with TestClient(A.create_app(settings, start_worker=False)) as client:
        health = client.get("/api/health").json()["screening"]
        assert health["enabled"] is False and health["available"] is False and health["budget"] is None
    assert not (settings.data_dir / "screening-spend.sqlite3").exists()


def test_evidence_cannot_serve_changed_or_unrecorded_files(screening_client):
    client, services, source, _ = screening_client
    created = client.post("/api/screenings", json={"video_id": "source-one"}).json()
    services.worker.run_once()
    detail = client.get(f"/api/screenings/{created['screen_id']}").json()
    source.write_bytes(b"changed source")
    assert client.get(detail["media_url"]).status_code == 409
    source.unlink()
    assert client.get(detail["media_url"]).status_code == 404
    frame = services.data / "screenings" / created["screen_id"] / "frames" / "000000_448.jpg"
    frame.write_bytes(b"changed frame")
    assert client.get(detail["windows"][0]["evidence_frames"][0]["media_url"]).status_code == 404
    for filename in ("999999_448.jpg", "..%2F..%2Fsecret", "000000_448.png"):
        assert client.get(f"/media/screening/{created['screen_id']}/{filename}").status_code == 404


def test_reading_video_metadata_is_free_and_design_requires_cached_genome(screening_client, monkeypatch):
    from directorloop.creative import genome as G

    client, services, source, _ = screening_client
    monkeypatch.setattr(G, "extract_genome", lambda *a, **kw: pytest.fail("GET dispatched model analysis"))
    result = client.get("/api/videos/source-one")
    assert result.status_code == 200
    assert result.json()["genome"] is None and result.json()["investigation"] is None and result.json()["has_genome"] is False
    assert client.get("/api/videos/source-one/design").status_code == 409
    genome = make_genome(sha256_file(source), [BeatRole.HOOK, BeatRole.PROOF, BeatRole.PAYOFF])
    cache = services.creative / "genomes" / f"genome_{genome.artifact_hash[:24]}_{GENOME_VERSION}_{services.settings.dl_probe_model}.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(genome.model_dump_json())
    cached = client.get("/api/videos/source-one").json()
    assert cached["has_genome"] is True and cached["genome"]["artifact_hash"] == genome.artifact_hash
    genome.artifact_hash = "0" * 64
    cache.write_text(genome.model_dump_json())
    assert client.get("/api/videos/source-one").json()["genome"] is None
    assert services.screening_ledger.summary()["physical_attempts"] == 0


def test_screening_cancel_and_recovery_preserve_partial_evidence(screening_client, monkeypatch):
    client, services, source, _ = screening_client
    report_holder = []

    def interrupted(video_id, path, provider, data_dir, *, screening_id, on_stage, is_cancelled):
        report = ScreenReport(id=screening_id, video_id=video_id, artifact_path=str(path), artifact_hash=sha256_file(path),
                              created_at=utc_now_iso(), windows=[ScreenWindow(start_ms=0, end_ms=2000, status="complete"),
                                                               ScreenWindow(start_ms=2000, end_ms=4000)])
        R.save_screening(data_dir, report)
        report_holder.append(report)
        services.jobs.request_cancel("job_" + screening_id.removeprefix("screen_"))
        assert is_cancelled()
        on_stage("SCREENING", "Canceled before next prefix", {})

    monkeypatch.setattr(R, "run_screening", interrupted)
    created = client.post("/api/screenings", json={"video_id": "source-one"}).json()
    job = services.worker.run_once()
    assert job.state == "CANCELED"
    report = R.load_screening(services.data, created["screen_id"])
    assert report.status == "canceled" and report.windows[0].status == "complete"
    # Independent crash reconciliation has no right to replay paid requests or
    # to assert an in-flight request was never attempted.
    report.status = "running"
    R.save_screening(services.data, report)
    failed = replace(job, state="FAILED", ended_at=utc_now_iso())
    reconcile_interrupted_job(services.data, failed)
    restored = R.load_screening(services.data, report.id)
    assert restored.status == "failed" and restored.windows[0].status == "complete"
    assert restored.windows[1].status == "failed" and "unknown" in restored.windows[1].error
    assert services.screening_ledger.summary()["physical_attempts"] == 0


def test_screening_api_and_evidence_use_existing_auth(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "build_providers", lambda settings: ProviderBundle(None, None))
    settings = Settings(_env_file=None, dl_data_dir=str(tmp_path), dl_weave_enabled=False,
                        dl_local_auth_token="offline-test-session", wandb_api_key="", openai_api_key="", gemini_api_key="")
    with TestClient(A.create_app(settings, start_worker=False)) as client:
        assert client.get("/api/screenings").status_code == 401
        assert client.post("/api/screenings", json={"video_id": "source-one"}).status_code == 401
        assert client.get("/media/screening/screen_ab_cd/original.mp4").status_code == 401
        assert client.get("/api/screenings", headers={"Authorization": "Bearer offline-test-session"}).status_code == 200


def test_operator_can_remove_local_caps_without_erasing_history(screening_client, monkeypatch):
    client, services, _, captured = screening_client
    ledger = services.screening_ledger
    card = price_card(A.WANDB_INFERENCE_BASE_URL, A.SCREENING_MODEL)
    for _ in range(30):
        reservation = ledger.reserve(card, 2048)
        ledger.settle(reservation, 1, 1)
    before = ledger.summary()
    assert client.get('/api/health').json()['full_screening']['available'] is False
    services.settings.dl_screening_spend_guard_enabled = False
    services._configure_screening()
    assert captured[-1]['spend_guard'] is None
    assert captured[-1]['max_output_tokens'] == 2048
    assert captured[-1]['allow_compatibility_fallback'] is False
    state = client.get('/api/health').json()['full_screening']
    assert state['available'] is True
    assert state['spending_guard_enabled'] is False and state['budget'] is None
    assert 'W&B' in state['usage_tracking']
    assert ledger.summary() == before
    assert client.post('/api/screenings', json={'video_id': 'source-one', 'coverage': 'full'}).status_code == 200
    monkeypatch.setattr(A, 'weave_status', lambda: SimpleNamespace(connected=False, project='offline', traces_url=None, reason=None))
    assert client.get('/api/health').json()['full_screening']['available'] is False
    services.settings.wandb_api_key = ''
    services._configure_screening()
    assert services.screening_provider is None
    assert client.get('/api/health').json()['full_screening']['available'] is False
    assert ledger.summary() == before
