"""Full coverage admission, worker forwarding and idempotency without inference."""
from directorloop.api import app as A
from directorloop.domain.ids import sha256_file, utc_now_iso
from directorloop.providers.spend import price_card
from directorloop.screening import runner as R
from directorloop.screening.models import ScreenReport, ScreenWindow
from tests.unit.test_api_screening import screening_client as screening_client


def _duration(services, source, milliseconds):
    services.register_upload({"video_id": "source-one", "path": str(source), "title": "Offline fixture", "source": "upload", "duration_ms": milliseconds})


def _consume_offline(ledger, count):
    card = price_card(A.WANDB_INFERENCE_BASE_URL, A.SCREENING_MODEL)
    for _ in range(count):
        attempt = ledger.reserve(card, 2048)
        ledger.settle(attempt, 1, 1)


def test_full_mode_reaches_worker_and_cannot_reuse_quick_idempotency(screening_client, monkeypatch):
    client, services, source, _ = screening_client
    _duration(services, source, 31637)
    admissions = []
    original = services.require_screening

    def require(count=3):
        admissions.append(count)
        original(count)

    calls = []

    def offline_full(video_id, path, provider, data_dir, *, screening_id, on_stage, is_cancelled, coverage, repair_incomplete):
        calls.append(coverage)
        assert repair_incomplete is True
        assert not is_cancelled()
        plan = R.screening_windows(31637, coverage)
        assert len(plan) == 8 and plan[0][0] == 0 and plan[-1][1] == 31637
        assert all(left[1] == right[0] for left, right in zip(plan, plan[1:], strict=False))
        report = ScreenReport(id=screening_id, video_id=video_id, artifact_path=str(path), artifact_hash=sha256_file(path),
                              duration_ms=31637, created_at=utc_now_iso(), ended_at=utc_now_iso(), status="complete",
                              windows=[ScreenWindow(start_ms=start, end_ms=end, status="complete") for start, end in plan])
        R.save_screening(data_dir, report)
        return report

    monkeypatch.setattr(services, "require_screening", require)
    monkeypatch.setattr(R, "run_screening", offline_full)
    body = {"video_id": "source-one", "coverage": "full", "idempotency_key": "full-once"}
    response = client.post("/api/screenings", json=body)
    assert response.status_code == 200
    first = response.json()
    assert services.jobs.get(first["job_id"]).params == {"video_id": "source-one", "coverage": "full"}
    assert client.post("/api/screenings", json=body).json() == {**first, "created": False}
    assert client.post("/api/screenings", json={**body, "coverage": "quick"}).status_code == 409
    assert services.worker.run_once().state == "COMPLETED"
    assert admissions == [16, 16] and calls == ["full"]
    detail = client.get("/api/screenings/" + first["screen_id"]).json()
    assert len(detail["windows"]) == 8 and detail["automatic_edit_allowed"] is False
    assert services.screening_ledger.summary()["physical_attempts"] == 0


def test_full_admission_reserves_no_calls_and_worker_rechecks_initial_plus_repair_headroom(screening_client):
    client, services, source, _ = screening_client
    _duration(services, source, 31637)
    ledger = services.screening_ledger
    _consume_offline(ledger, 14)
    assert client.get("/api/health").json()["full_screening"]["available"] is True
    response = client.post("/api/screenings", json={"video_id": "source-one", "coverage": "full", "idempotency_key": "sixteen-left"})
    assert response.status_code == 200 and ledger.summary()["physical_attempts"] == 14
    _consume_offline(ledger, 1)
    before = ledger.summary()
    health = client.get("/api/health").json()
    assert health["full_screening"]["available"] is False
    assert "16 requests" in health["full_screening"]["reason"]
    assert health["screening"]["available"] is True
    job = services.worker.run_once()
    assert job.state == "FAILED" and ledger.summary() == before
    assert not list((services.data / "screenings").glob("*.json"))
    new = client.post("/api/screenings", json={"video_id": "source-one", "coverage": "full"})
    assert new.status_code == 503 and "16 requests" in new.json()["detail"]
    assert client.post("/api/screenings", json={"video_id": "source-one", "coverage": "quick"}).status_code == 200
    assert ledger.summary() == before


def test_full_short_video_uses_actual_plan_count_and_rejects_unknown_mode(screening_client):
    client, services, source, _ = screening_client
    _duration(services, source, 1500)
    _consume_offline(services.screening_ledger, 28)
    before = services.screening_ledger.summary()
    assert client.post("/api/screenings", json={"video_id": "source-one", "coverage": "every-frame"}).status_code == 422
    assert services.jobs.list() == []
    assert client.post("/api/screenings", json={"video_id": "source-one", "coverage": "full"}).status_code == 200
    assert services.screening_ledger.summary() == before
    _consume_offline(services.screening_ledger, 1)
    exhausted = services.screening_ledger.summary()
    rejected = client.post("/api/screenings", json={"video_id": "source-one", "coverage": "full"})
    assert rejected.status_code == 503 and "2 requests" in rejected.json()["detail"]
    assert services.screening_ledger.summary() == exhausted


def test_full_admission_keeps_initial_pass_dollar_bound_and_individual_reservations(screening_client):
    client, services, source, _ = screening_client
    _duration(services, source, 31637)
    ledger = services.screening_ledger
    card = price_card(A.WANDB_INFERENCE_BASE_URL, A.SCREENING_MODEL)

    def spend_offline(count):
        for _ in range(count):
            attempt = ledger.reserve(card, A.SCREENING_OUTPUT_CAP)
            ledger.settle(attempt, 240000, 1333)

    # Settled fixture usage leaves room for eight conservative reservations,
    # but not sixteen at once. Repairs are admitted individually after settling.
    spend_offline(10)
    before = ledger.summary()
    one_bound = card.cost_units(card.max_input_tokens, A.SCREENING_OUTPUT_CAP) / 1_000_000_000
    assert 8 * one_bound < before["available_usd"] < 16 * one_bound
    assert before["physical_attempts"] + 16 <= before["max_physical_attempts"]
    assert client.get("/api/health").json()["full_screening"]["available"] is True
    response = client.post("/api/screenings", json={"video_id": "source-one", "coverage": "full"})
    assert response.status_code == 200 and ledger.summary() == before
    spend_offline(2)
    after = ledger.summary()
    assert after["physical_attempts"] + 16 <= after["max_physical_attempts"]
    assert after["available_usd"] < 8 * one_bound
    assert client.get("/api/health").json()["full_screening"]["available"] is False
    assert services.worker.run_once().state == "FAILED"
    assert ledger.summary() == after
