"""Offline full-review pricing, atomic job/global ceilings and API admission."""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from directorloop.api import app as A
from directorloop.config import Settings
from directorloop.providers import spend as S
from directorloop.providers.base import ProviderError
from directorloop.providers.openai_compat import OpenAICompatProvider
from directorloop.providers.registry import build_providers

FIXTURE_CARD = S.PriceCard("https://api.openai.com/v1", "offline-probe", "1", "1", 100, 10,
                         "max_completion_tokens", "https://example.invalid/offline-fixture", "2000-01-01", "2100-01-01",
                         service_tier="default")


def scoped_worker(args):
    path, run_id = args
    ledger = S.SpendLedger(path, "shared", "0.000525", max_attempts=20,
                           run_limit_usd="0.000315", max_run_attempts=3)
    try:
        return ledger.reserve(FIXTURE_CARD, 5, run_id=run_id)
    except S.SpendLimitExceeded:
        return None


@pytest.mark.parametrize("executor", [ThreadPoolExecutor, ProcessPoolExecutor])
def test_both_limits_are_reserved_atomically_across_workers(tmp_path, executor):
    path = tmp_path / "shared.db"
    ledger = S.SpendLedger(path, "shared", "0.000525", max_attempts=20,
                           run_limit_usd="0.000315", max_run_attempts=3)
    with executor(max_workers=6) as pool:
        results = list(pool.map(scoped_worker, [(path, f"job-{i % 2}") for i in range(30)]))
    assert sum(r is not None for r in results) == 5
    assert ledger.summary()["held_usd"] == 0.000525
    assert sum(ledger.run_summary(f"job-{i}")["physical_attempts"] for i in range(2)) == 5
    assert all(ledger.run_summary(f"job-{i}")["physical_attempts"] <= 3 for i in range(2))


def test_run_policy_is_immutable_and_missing_scope_cannot_escape_it(tmp_path):
    path = tmp_path / "shared.db"
    ledger = S.SpendLedger(path, "shared", 2, run_limit_usd=1, max_run_attempts=2)
    with pytest.raises(S.SpendGuardError, match="explicit run ID"):
        ledger.reserve(FIXTURE_CARD, 5)
    for kwargs in ({}, {"run_limit_usd": 2, "max_run_attempts": 2}, {"run_limit_usd": 1, "max_run_attempts": 3}):
        with pytest.raises(S.SpendGuardError, match="fixed run policy"):
            S.SpendLedger(path, "shared", 2, **kwargs)
    for _ in range(2):
        ledger.unknown(ledger.reserve(FIXTURE_CARD, 5, run_id="job-one"))
    reopened = S.SpendLedger(path, "shared", 2, run_limit_usd=1, max_run_attempts=2)
    with pytest.raises(S.SpendLimitExceeded):
        reopened.reserve(FIXTURE_CARD, 5, run_id="job-one")
    assert reopened.run_summary("job-one")["attempt_status_counts"] == {"unknown": 2}
    assert reopened.run_summary("job-one")["held_usd"] == 0.00021
    reopened.reserve(FIXTURE_CARD, 5, run_id="job-two")


def test_old_unscoped_spend_cannot_be_excluded_from_new_run_policy(tmp_path):
    path = tmp_path / "old.db"
    ledger = S.SpendLedger(path, "old", 1)
    ledger.reserve(FIXTURE_CARD, 5)
    with pytest.raises(S.SpendGuardError, match="unscoped"):
        S.SpendLedger(path, "old", 1, run_limit_usd=.5)
    assert ledger.summary()["physical_attempts"] == 1


def test_run_money_limit_binds_before_attempt_limit_and_unknown_never_refunds(tmp_path):
    ledger = S.SpendLedger(tmp_path / "scopes.db", "scope-cost", ".001", max_attempts=100,
                           run_limit_usd=".00021", max_run_attempts=100)
    for _ in range(2):
        ledger.unknown(ledger.reserve(FIXTURE_CARD, 5, run_id="same-job"))
    with pytest.raises(S.SpendLimitExceeded):
        ledger.reserve(FIXTURE_CARD, 5, run_id="same-job")
    assert ledger.summary()["available_usd"] == .00079
    assert ledger.run_summary("same-job")["available_usd"] == 0
    ledger.reserve(FIXTURE_CARD, 5, run_id="other-job")


def test_different_model_prices_share_the_exact_global_ceiling(tmp_path):
    ledger = S.SpendLedger(tmp_path / "mixed.db", "mixed", ".00031", run_limit_usd=".00031")
    ledger.reserve(FIXTURE_CARD, 5, run_id="first")  # .000105 remains held
    more_expensive = replace(FIXTURE_CARD, model="offline-planner", input_usd_per_million="2")
    ledger.reserve(more_expensive, 5, run_id="second")  # .000205, exactly the shared ceiling
    assert ledger.summary()["held_usd"] == .00031
    with pytest.raises(S.SpendLimitExceeded):
        ledger.reserve(FIXTURE_CARD, 5, run_id="third")


def test_verified_standard_prices_include_long_context_and_cache_writes():
    terra = S.price_card("https://api.openai.com/v1", "gpt-5.6-terra")
    sol = S.price_card("https://api.openai.com/v1", "gpt-5.6-sol")
    assert terra.cost_units(1_050_000, 2048) == 5_286_864_000
    assert sol.cost_units(1_050_000, 2048) == 10_561_440_000
    assert terra.cost_units(272_000, 1000) == 692_000_000
    assert terra.cost_units(272_001, 1000) == 1_378_005_000
    assert sol.cost_units(1000, 1000) == 25_000_000
    assert terra.service_tier == sol.service_tier == "default"


def fake_client(monkeypatch, replies):
    import openai
    calls, configs = [], []

    def create(**kwargs):
        calls.append(kwargs)
        result = replies.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def client(**kwargs):
        configs.append(kwargs)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    monkeypatch.setattr(openai, "OpenAI", client)
    return calls, configs


def reply(*, tier="default"):
    return SimpleNamespace(service_tier=tier, usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1),
                           choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok":true}'), finish_reason="stop")])


def settings(tmp_path, **extra):
    values = dict(_env_file=None, dl_data_dir=str(tmp_path / "data"), dl_weave_enabled=False,
                  dl_screening_enabled=False, openai_api_key="offline-secret", gemini_api_key="", wandb_api_key="",
                  dl_probe_provider="openai", dl_probe_model=FIXTURE_CARD.model,
                  dl_planner_provider="openai", dl_planner_model="offline-planner",
                  dl_spend_limit_usd=.01, dl_spend_budget_id="shared", dl_spend_per_run_limit_usd=.001,
                  dl_spend_max_run_attempts=2, dl_spend_max_physical_attempts=4, dl_max_output_tokens=5)
    return Settings(**(values | extra))


def fixture_prices(monkeypatch):
    monkeypatch.setattr(S, "PRICE_CARDS", (FIXTURE_CARD, replace(FIXTURE_CARD, model="offline-planner", input_usd_per_million="2")))


def test_probe_and_planner_share_scopes_without_mutating_other_jobs(monkeypatch, tmp_path):
    fixture_prices(monkeypatch)
    calls, configs = fake_client(monkeypatch, [reply(), reply(), reply()])
    monkeypatch.setenv("OPENAI_BASE_URL", "https://unpriced.invalid/v1")
    bundle = build_providers(settings(tmp_path))
    one, two = bundle.for_run("job-one"), bundle.for_run("job-two")
    assert one.probe.spend_guard is one.planner.spend_guard
    assert one.probe.spend_guard.ledger is two.probe.spend_guard.ledger
    assert bundle.probe.spend_guard.run_id is None
    one.probe._chat([], {}, 0)
    one.planner._chat([], {}, 0)
    with pytest.raises(S.SpendLimitExceeded):
        one.probe._chat([], {}, 0)
    two.probe._chat([], {}, 0)
    assert len(calls) == 3
    assert all(c["max_retries"] == 0 and c["base_url"] == FIXTURE_CARD.endpoint for c in configs)
    assert all(c["service_tier"] == "default" and c["n"] == 1 and c["max_completion_tokens"] == 5 for c in calls)
    assert one.probe._request_policy()["spend_run_id"] == "job-one"
    assert one.probe._request_policy()["compatibility_fallback"] is False
    assert bundle.spend_guard.ledger.run_summary("job-one")["held_usd"] == .000008
    assert bundle.spend_guard.ledger.run_summary("job-two")["held_usd"] == .000003


def test_failed_physical_request_retains_job_and_shared_reservation_without_retry(monkeypatch, tmp_path):
    fixture_prices(monkeypatch)
    rejection = type("Rejection", (Exception,), {"status_code": 400})("temperature unsupported")
    calls, _ = fake_client(monkeypatch, [rejection, reply()])
    bundle = build_providers(settings(tmp_path)).for_run("job-one")
    with pytest.raises(ProviderError):
        bundle.probe._chat([], {}, 0)
    assert len(calls) == 1
    assert bundle.spend_guard.ledger.summary()["attempt_status_counts"] == {"unknown": 1}
    assert bundle.spend_guard.ledger.run_summary("job-one")["held_usd"] == .000105


@pytest.mark.parametrize("bad", [{"n": 2}, {"service_tier": "fast"}, {"extra_body": {"service_tier": "priority"}},
                                  {"modalities": ["audio"]}, {"messages": [{"role": "user", "content": [{"type": "input_audio"}]}]}])
def test_unpriced_request_shapes_fail_before_reservation(monkeypatch, tmp_path, bad):
    fixture_prices(monkeypatch)
    ledger = S.SpendLedger(tmp_path / "guard.db", "guard", 1)
    with pytest.raises(S.SpendGuardError):
        S.SpendGuard(ledger, 5).prepare(FIXTURE_CARD.endpoint, FIXTURE_CARD.model, bad)
    assert ledger.summary()["physical_attempts"] == 0


def test_unexpected_returned_service_tier_is_unknown_and_halts(monkeypatch, tmp_path):
    fixture_prices(monkeypatch)
    calls, _ = fake_client(monkeypatch, [reply(tier="priority"), reply()])
    ledger = S.SpendLedger(tmp_path / "guard.db", "guard", 1)
    provider = OpenAICompatProvider("openai", "offline", FIXTURE_CARD.model, spend_guard=S.SpendGuard(ledger, 5))
    with pytest.raises(S.SpendGuardError, match="unpriced service tier"):
        provider._chat([], {}, 0)
    assert ledger.summary()["attempt_status_counts"] == {"unknown": 1}
    assert ledger.summary()["halted"] is True
    with pytest.raises(S.SpendLimitExceeded):
        provider._chat([], {}, 0)
    assert len(calls) == 1


def test_two_dollar_real_model_budget_blocks_admission_without_model_calls(monkeypatch, tmp_path):
    calls, _ = fake_client(monkeypatch, [])
    monkeypatch.setattr(A, "init_weave", lambda settings: None)
    config = settings(tmp_path, dl_probe_model="gpt-5.6-terra", dl_planner_model="gpt-5.6-sol",
                      dl_spend_limit_usd=2, dl_spend_per_run_limit_usd=.5, dl_max_output_tokens=2048)
    app = A.create_app(config, start_worker=False)
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"offline registered fixture")
    app.state.services.register_upload({"video_id": "clip", "path": str(source), "title": "Clip", "source": "upload"})
    with TestClient(app) as client:
        for _ in range(2):
            status = client.get("/api/health").json()["full_review_spending"]
            assert status["enabled"] and not status["available"]
            assert {Decimal(str(p["request_reservation_usd"])) for p in status["prices"]} == {Decimal("5.286864"), Decimal("10.56144")}
            assert client.post("/api/audits", json={"video_id": "clip"}).status_code == 503
        assert app.state.services.jobs.list() == []
    assert calls == []
    assert app.state.services.providers.spend_guard.ledger.summary()["physical_attempts"] == 0


def test_api_binds_job_scope_and_idempotency_survives_exhaustion(monkeypatch, tmp_path):
    fixture_prices(monkeypatch)
    calls, _ = fake_client(monkeypatch, [reply()])
    monkeypatch.setattr(A, "init_weave", lambda settings: None)
    app = A.create_app(settings(tmp_path, dl_spend_max_physical_attempts=1), start_worker=False)
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"offline registered fixture")
    app.state.services.register_upload({"video_id": "clip", "path": str(source), "title": "Clip", "source": "upload"})

    def audit(**kwargs):
        kwargs["provider"]._chat([], {}, 0)
        return SimpleNamespace(id="audit-offline", status="complete", findings=[], strengths=[], weave_url=None)

    monkeypatch.setattr(A, "run_audit", audit)
    with TestClient(app) as client:
        body = {"video_id": "clip", "idempotency_key": "once"}
        started = client.post("/api/audits", json=body).json()
        job = app.state.services.worker.run_once()
        assert job.state == "COMPLETED"
        assert app.state.services.providers.spend_guard.ledger.run_summary(job.id)["physical_attempts"] == 1
        again = client.post("/api/audits", json=body).json()
        assert again["job_id"] == started["job_id"] and again["created"] is False
        assert client.post("/api/audits", json={"video_id": "clip"}).status_code == 503
    assert len(calls) == 1


def test_missing_ledger_is_not_recreated_by_status_or_dispatch(monkeypatch, tmp_path):
    fixture_prices(monkeypatch)
    calls, _ = fake_client(monkeypatch, [reply()])
    bundle = build_providers(settings(tmp_path)).for_run("job-one")
    path = bundle.spend_guard.ledger.path
    path.unlink()
    assert bundle.spend_status() == {"enabled": True, "available": False,
                                     "reason": "Full-review spending ledger is unavailable; new requests blocked.", "budget": None}
    with pytest.raises(S.SpendGuardError, match="ledger unavailable"):
        bundle.probe._chat([], {}, 0)
    assert not path.exists() and calls == []
