"""Offline physical-attempt reservations, including parallel processes and ambiguity."""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from directorloop.providers import spend as S
from directorloop.providers.base import ProviderError
from directorloop.providers.openai_compat import OpenAICompatProvider

CARD = S.PriceCard("https://api.inference.wandb.ai/v1", "offline-priced-model", "1", "1", 100, 10,
                   "max_tokens", "https://example.invalid/offline-fixture", "2000-01-01", "2100-01-01")


def reserve_worker(args):
    path, limit = args
    ledger = S.SpendLedger(path, "parallel", limit)
    try:
        return ledger.reserve(CARD, 5)
    except S.SpendLimitExceeded:
        return None


@pytest.mark.parametrize("executor", [ThreadPoolExecutor, ProcessPoolExecutor])
def test_parallel_workers_cannot_overreserve(tmp_path, executor):
    path = tmp_path / "spend.db"
    S.SpendLedger(path, "parallel", "0.001")
    with executor(max_workers=6) as pool:
        attempted = list(pool.map(reserve_worker, [(path, "0.001")] * 30))
    # Exactly 9 reservations of $0.000105 fit; no rounding or race overshoot.
    assert sum(result is not None for result in attempted) == 9
    ledger = S.SpendLedger(path, "parallel", "0.001")
    summary = ledger.summary()
    assert summary["physical_attempts"] == 9
    assert summary["held_usd"] == 0.000945
    assert summary["usage_estimate_usd"] == 0
    assert summary["attempt_status_counts"] == {"reserved": 9}


def test_failed_missing_usage_and_restart_never_refund(tmp_path):
    path = tmp_path / "spend.db"
    ledger = S.SpendLedger(path, "persist", "0.00021")
    first, second = ledger.reserve(CARD, 5), ledger.reserve(CARD, 5)
    ledger.unknown(first)
    ledger.settle(second, 10, None)
    ledger = S.SpendLedger(path, "persist", "0.00021")
    with pytest.raises(S.SpendLimitExceeded):
        ledger.reserve(CARD, 5)
    assert ledger.summary()["attempt_status_counts"] == {"unknown": 2}
    assert ledger.summary()["held_usd"] == 0.00021
    ledger.settle(first, 0, 0)  # no accidental late overwrite of ambiguous billing
    assert ledger.summary()["held_usd"] == 0.00021


def test_valid_usage_releases_only_unused_reservation(tmp_path):
    ledger = S.SpendLedger(tmp_path / "spend.db", "settle", "0.00011")
    attempt = ledger.reserve(CARD, 5)
    ledger.settle(attempt, 2, 1)
    ledger.settle(attempt, 0, 0)  # settlement idempotence
    assert ledger.summary()["usage_estimate_usd"] == 0.000003
    ledger.reserve(CARD, 5)
    assert ledger.summary()["held_usd"] == 0.000108


@pytest.mark.parametrize("bad_usage", [(-1, 0), (None, 2), (True, 0), (1.5, 0), ("2", 0)])
def test_invalid_usage_is_unknown(tmp_path, bad_usage):
    ledger = S.SpendLedger(tmp_path / "spend.db", "unknown", 1)
    attempt = ledger.reserve(CARD, 5)
    ledger.settle(attempt, *bad_usage)
    assert ledger.summary()["attempt_status_counts"] == {"unknown": 1}
    assert ledger.summary()["unsettled_reserved_usd"] == 0.000105


def test_provider_bound_violation_is_recorded_and_halts_future_work(tmp_path):
    ledger = S.SpendLedger(tmp_path / "spend.db", "violation", 1)
    attempt = ledger.reserve(CARD, 5)
    ledger.settle(attempt, 101, 5)
    assert ledger.summary()["halted"]
    assert ledger.summary()["held_usd"] == 0.000106
    with pytest.raises(S.SpendLimitExceeded):
        ledger.reserve(CARD, 5)


def test_existing_budget_and_attempt_ceiling_are_immutable(tmp_path):
    path = tmp_path / "spend.db"
    ledger = S.SpendLedger(path, "fixed", 1, max_attempts=1)
    attempt = ledger.reserve(CARD, 5)
    ledger.settle(attempt, 0, 0)
    with pytest.raises(S.SpendLimitExceeded):
        ledger.reserve(CARD, 5)
    with pytest.raises(S.SpendGuardError, match="different fixed limit"):
        S.SpendLedger(path, "fixed", 2, max_attempts=1)
    with pytest.raises(S.SpendGuardError, match="different fixed limit"):
        S.SpendLedger(path, "fixed", 1)


def test_unsupported_or_stale_prices_fail_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "PRICE_CARDS", (CARD,))
    guard = S.SpendGuard(S.SpendLedger(tmp_path / "spend.db", "pricing", 1), 5)
    for endpoint, model in [(CARD.endpoint, "other"), ("https://other.invalid/v1", CARD.model)]:
        with pytest.raises(S.SpendGuardError, match="no verified price"):
            guard.prepare(endpoint, model, {})
    with pytest.raises(S.SpendGuardError, match="stale"):
        replace(CARD, expires_on="2000-01-01").check(date(2026, 9, 13))
    assert guard.ledger.summary()["physical_attempts"] == 0


def test_currency_and_request_bounds(tmp_path):
    for invalid in ("NaN", "Infinity", "-1", "0", "0.0000000001"):
        with pytest.raises(S.SpendGuardError):
            S.SpendLedger(tmp_path / "invalid.db", "invalid", invalid)
    assert CARD.cost_units(1, 1) == 2000
    tiny = replace(CARD, input_usd_per_million="0.0000001")
    assert tiny.cost_units(1, 0) == 1  # always round a fraction upward
    ledger = S.SpendLedger(tmp_path / "spend.db", "limits", Decimal("1"))
    for cap in (0, True, -1, 11):
        with pytest.raises(S.SpendGuardError):
            ledger.reserve(CARD, cap)


class Rejection(Exception):
    status_code = 400


def adapter(monkeypatch, tmp_path, replies, *, fallback=True, guard_enabled=True,
            model=CARD.model, enable_thinking=None):
    import openai
    monkeypatch.setattr(S, "PRICE_CARDS", (replace(CARD, model=model),))
    calls, sdk_config = [], {}

    def create(**kwargs):
        calls.append(kwargs)
        result = replies.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def client(**kwargs):
        sdk_config.update(kwargs)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    monkeypatch.setattr(openai, "OpenAI", client)
    ledger = S.SpendLedger(tmp_path / "spend.db", "http", "0.001")
    provider = OpenAICompatProvider("offline", "private-secret", model, base_url=CARD.endpoint,
                                    spend_guard=S.SpendGuard(ledger, 5) if guard_enabled else None,
                                    allow_compatibility_fallback=fallback, enable_thinking=enable_thinking)
    return provider, ledger, calls, sdk_config


def reply(usage=True, text='{"ok":true}', finish_reason="stop"):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason=finish_reason)],
                           usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1) if usage else None)


def test_each_compatibility_attempt_is_reserved_and_sdk_retry_is_zero(monkeypatch, tmp_path):
    provider, ledger, calls, config = adapter(monkeypatch, tmp_path,
                                             [Rejection("response_format json_schema unsupported"), reply()])
    assert provider._chat([{"role": "user", "content": "test"}], {"type": "object"}, 0).data == {"ok": True}
    assert config["max_retries"] == 0
    assert len(calls) == ledger.summary()["physical_attempts"] == 2
    assert all(call["max_tokens"] == 5 for call in calls)
    assert ledger.summary()["attempt_status_counts"] == {"settled": 1, "unknown": 1}
    assert ledger.summary()["held_usd"] == 0.000108
    assert "private-secret" not in (tmp_path / "spend.db").read_bytes().decode(errors="ignore")


def test_pilot_no_fallback_and_timeout_never_retry(monkeypatch, tmp_path):
    provider, ledger, calls, _ = adapter(monkeypatch, tmp_path,
                                        [Rejection("response_format unsupported"), reply()], fallback=False)
    with pytest.raises(ProviderError):
        provider._chat([], {"type": "object"}, 0)
    assert len(calls) == 1 and ledger.summary()["attempt_status_counts"] == {"unknown": 1}


def test_ambiguous_failure_and_non_json_usage_are_accounted(monkeypatch, tmp_path):
    provider, ledger, calls, _ = adapter(monkeypatch, tmp_path, [TimeoutError("temperature response_format timeout"), reply(text="not JSON")])
    with pytest.raises(ProviderError):
        provider._chat([], {}, 0)
    assert len(calls) == 1
    with pytest.raises(ProviderError):
        provider._chat([], {}, 0)
    assert ledger.summary()["attempt_status_counts"] == {"settled": 1, "unknown": 1}


def test_guard_denial_sends_no_http(monkeypatch, tmp_path):
    provider, ledger, calls, _ = adapter(monkeypatch, tmp_path, [reply()])
    provider.model = "unpriced-model"
    with pytest.raises(S.SpendGuardError):
        provider._chat([], None, 0)
    assert calls == [] and ledger.summary()["physical_attempts"] == 0


def test_unconfigured_guard_preserves_legacy_uncapped_payload(monkeypatch, tmp_path):
    provider, ledger, calls, config = adapter(monkeypatch, tmp_path, [reply()], guard_enabled=False)
    provider._chat([], None, 0)
    assert "max_tokens" not in calls[0]
    assert config["max_retries"] == 0 and ledger.summary()["physical_attempts"] == 0


def test_compatibility_negotiation_has_three_physical_attempt_limit(monkeypatch, tmp_path):
    provider, ledger, calls, _ = adapter(monkeypatch, tmp_path, [Rejection("temperature unsupported"),
                                                                Rejection("response_format unsupported"),
                                                                Rejection("response_format unsupported"), reply()])
    with pytest.raises(ProviderError):
        provider._chat([], {"type": "object"}, 0)
    assert len(calls) == ledger.summary()["physical_attempts"] == 3
    assert ledger.summary()["attempt_status_counts"] == {"unknown": 3}


def test_registry_uses_supplied_settings_and_shared_ledger(monkeypatch, tmp_path):
    from directorloop.config import Settings
    from directorloop.providers.registry import build_providers
    adapter(monkeypatch, tmp_path / "unused", [])  # offline OpenAI constructor
    settings = Settings(_env_file=None, dl_data_dir=str(tmp_path), dl_spend_limit_usd=1,
                        dl_spend_budget_id="registry", dl_max_output_tokens=5,
                        dl_probe_provider="wandb_inference", dl_planner_provider="wandb_inference",
                        dl_wandb_inference_probe_model=CARD.model, dl_wandb_inference_planner_model=CARD.model,
                        wandb_api_key="offline", openai_api_key="", gemini_api_key="", typesafe_api_key="")
    bundle = build_providers(settings)
    assert bundle.probe.spend_guard is bundle.planner.spend_guard
    assert bundle.probe.spend_guard.ledger.path == tmp_path / "provider-spend.sqlite3"
    assert bundle.probe.spend_guard.ledger.budget_id == "registry"
    assert bundle.probe.spend_guard.max_output_tokens == 5


def test_registry_never_falls_back_around_active_guard(tmp_path):
    from directorloop.config import Settings
    from directorloop.providers.registry import build_providers
    settings = Settings(_env_file=None, dl_data_dir=str(tmp_path), dl_spend_limit_usd=1,
                        dl_spend_budget_id="no-fallback", dl_probe_provider="wandb_inference",
                        dl_planner_provider="gemini", wandb_api_key="", gemini_api_key="offline",
                        openai_api_key="offline", typesafe_api_key="")
    bundle = build_providers(settings)
    assert bundle.probe is None and bundle.planner is None
    assert any("unsupported by the spending guard" in note for note in bundle.notes)


def test_verified_qwen_toggle_is_narrow_and_cap_remains_controlled(monkeypatch, tmp_path):
    provider, _, calls, _ = adapter(monkeypatch, tmp_path, [reply()], model="Qwen/Qwen3.8-27B", enable_thinking=False)
    provider._chat([], {}, 0)
    assert calls[0]["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert calls[0]["max_tokens"] == 5
    assert provider._request_policy() == {"endpoint": CARD.endpoint, "enable_thinking": False,
                                           "max_output_tokens": 5, "sdk_retries": 0, "compatibility_fallback": True}


def test_default_sends_no_thinking_override(monkeypatch, tmp_path):
    provider, _, calls, _ = adapter(monkeypatch, tmp_path, [reply()])
    provider._chat([], {}, 0)
    assert "extra_body" not in calls[0]
    assert provider._request_policy()["enable_thinking"] is None


@pytest.mark.parametrize("endpoint,model,toggle", [("https://api.openai.com/v1", "Qwen/Qwen3.8-27B", False),
                                                   (CARD.endpoint, "other-model", False),
                                                   (CARD.endpoint, "Qwen/Qwen3.8-27B", "false")])
def test_toggle_rejects_unverified_endpoint_model_or_type(endpoint, model, toggle):
    with pytest.raises(ProviderError, match="only verified"):
        OpenAICompatProvider("offline", "offline", model, base_url=endpoint, enable_thinking=toggle)


@pytest.mark.parametrize("body", [{"max_tokens": 1000}, {"max_completion_tokens": 1000},
                                  {"chat_template_kwargs": {"enable_thinking": False, "max_tokens": 1000}},
                                  {"chat_template_kwargs": {"enable_thinking": False}, "max_tokens": 1000}])
def test_extra_body_cannot_override_output_cap(monkeypatch, tmp_path, body):
    card = replace(CARD, model="Qwen/Qwen3.8-27B")
    monkeypatch.setattr(S, "PRICE_CARDS", (card,))
    guard = S.SpendGuard(S.SpendLedger(tmp_path / "spend.db", "body", 1), 5)
    with pytest.raises(S.SpendGuardError, match="request-body overrides"):
        guard.prepare(card.endpoint, card.model, {"extra_body": body})
    assert guard.ledger.summary()["physical_attempts"] == 0


@pytest.mark.parametrize("text", [None, '{"ok":true}'])
def test_length_cutoff_settles_returned_usage_before_safe_error_without_retry(monkeypatch, tmp_path, text):
    provider, ledger, calls, _ = adapter(monkeypatch, tmp_path, [reply(text=text, finish_reason="length"), reply()])
    with pytest.raises(ProviderError, match="^model output token limit reached; response incomplete$"):
        provider._chat([], {}, 0)
    assert len(calls) == 1
    assert ledger.summary()["attempt_status_counts"] == {"settled": 1}
    assert ledger.summary()["usage_estimate_usd"] == 0.000003
