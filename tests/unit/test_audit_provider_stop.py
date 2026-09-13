"""Offline terminal-provider stops preserve actual replies and never charge skipped windows."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from directorloop.audit import review as R
from directorloop.audit.attention import AttentionConfig
from directorloop.creative.signals import VideoSignals
from directorloop.providers.base import CompletionResult, ProviderCapability, ProviderError
from directorloop.runtime.budget import BudgetedProvider, CallBudget
from tests.unit.test_audit_workflow import ScriptedAuditProvider, install_media


class Rejection(Exception):
    def __init__(self, status: int, code: str):
        self.status_code = status
        self.body = {"error": {"code": code}}
        super().__init__("private-provider-body")


class Provider:
    capability = ProviderCapability(name="offline", role="probe", model="scripted")

    def __init__(self, failure="quota", *, drain=False, precision=False):
        self.failure, self.drain, self.precision = failure, drain, precision
        self.calls = []
        self.lock = threading.Lock()
        self.started_six = threading.Barrier(6) if drain else None
        self.release_siblings = threading.Event()

    def judge_json(self, media, instruction, schema):
        with self.lock:
            self.calls.append((media.duration_ms, "window" if schema is R.WINDOW_SCHEMA else "diagnosis"))
        if schema is R.DIAG_SCHEMA:
            return CompletionResult(data={"findings": [], "strengths": [], "audience": {}, "overall_summary": "offline diagnosis"}, latency_ms=1, model="scripted")
        assert schema is R.WINDOW_SCHEMA
        if self.drain:
            self.started_six.wait(timeout=3)
        failed_window = media.duration_ms % 2000 != 0 if self.precision else media.duration_ms == 2000
        if failed_window:
            status, code = {"quota": (429, "insufficient_quota"), "auth": (401, "invalid_api_key"),
                            "permission": (403, "permission_denied"), "rate_limit": (429, "rate_limit_exceeded"),
                            "transient": (503, "unavailable")}[self.failure]
            raise ProviderError("scripted provider rejected the request private-provider-body") from Rejection(status, code)
        if self.drain:
            assert self.release_siblings.wait(3)
        return CompletionResult(data={"reaction": "neutral", "attention_risk": "high" if self.precision and media.duration_ms >= 4000 else "low",
                                      "understanding": "retained successful review", "cause": "offline script"},
                                latency_ms=1, input_tokens=2, output_tokens=1, model="scripted")


def setup(monkeypatch, tmp_path, duration=32000):
    video = install_media(monkeypatch, tmp_path)
    monkeypatch.setattr(R, "inspect_media", lambda _: SimpleNamespace(duration_ms=duration, has_audio=True, width=360, height=640))
    monkeypatch.setattr(R, "extract_signals", lambda *_: VideoSignals(duration, 360, 640, [2000], [], np.ones(duration // 10, dtype=np.float32)))
    return video


def run(tmp_path, video, provider, callback=None, precision=False):
    budget = CallBudget(100)
    return budget, lambda: R.run_audit(video_id="source", video_path=video, version_id="v0", data_dir=tmp_path / "data",
                                      provider=BudgetedProvider(provider, budget), on_stage=callback,
                                      attention=AttentionConfig(precision_enabled=precision))


def checkpoint(tmp_path: Path):
    paths = list((tmp_path / "data" / "audit").glob("*/chronological_reactions.json"))
    assert len(paths) == 1
    assert not (paths[0].parent / "audit.json").exists(), "a terminal provider stop is not a complete audit"
    return json.loads(paths[0].read_text())


@pytest.mark.parametrize("failure", ["quota", "auth", "permission"])
def test_hard_failure_stops_new_windows_and_diagnosis(monkeypatch, tmp_path, failure):
    video = setup(monkeypatch, tmp_path)
    provider = Provider(failure)
    events = []
    budget, execute = run(tmp_path, video, provider, lambda *event: events.append(event))
    with pytest.raises(ProviderError):
        execute()
    saved = checkpoint(tmp_path)
    receipt = saved["provider_stop"]
    assert 1 <= budget.calls <= 6
    assert budget.calls == len(provider.calls) == receipt["logical_calls_started"]
    assert all(kind == "window" for _, kind in provider.calls)
    skipped = [r for r in saved["coarse_reactions"] if (r["error"] or "").startswith(R.UNATTEMPTED_WINDOW)]
    assert len(skipped) == 16 - budget.calls == receipt["windows_unattempted"]
    assert all(r["input_tokens"] is None and r["output_tokens"] is None and r["latency_ms"] == 0 for r in skipped)
    assert receipt["diagnosis_attempted"] is False and receipt["in_flight_remaining"] == 0
    assert any(event[0] == "WORKFLOW_STAGE" and event[2]["stage"]["key"] == "diagnose" and event[2]["stage"]["status"] == "skipped" for event in events)
    assert "private-provider-body" not in json.dumps(saved) + json.dumps(events)


def test_already_in_flight_successes_are_drained_and_preserved(monkeypatch, tmp_path):
    video = setup(monkeypatch, tmp_path)
    provider = Provider(drain=True)

    def on_stage(stage, message, data):
        if stage == "WORKFLOW_STAGE" and data["stage"]["key"] == "review_window" and data["stage"]["status"] == "incomplete":
            provider.release_siblings.set()

    budget, execute = run(tmp_path, video, provider, on_stage)
    with pytest.raises(ProviderError):
        execute()
    saved = checkpoint(tmp_path)
    receipt = saved["provider_stop"]
    assert budget.calls == receipt["logical_calls_started"] == 6
    assert receipt["logical_calls_succeeded"] == receipt["in_flight_when_stop_detected"] == 5
    assert receipt["logical_calls_failed"] == 1 and receipt["windows_unattempted"] == 10
    successes = [r for r in saved["coarse_reactions"] if r["error"] is None]
    assert len(successes) == 5 and all(r["understanding"] == "retained successful review" for r in successes)
    assert budget.input_tokens == 10 and budget.output_tokens == 5
    assert receipt["failed_request_billed_usage"] == "unknown"


@pytest.mark.parametrize("failure", ["rate_limit", "transient"])
def test_recoverable_window_errors_allow_remaining_reviews_and_diagnosis(monkeypatch, tmp_path, failure):
    video = setup(monkeypatch, tmp_path)
    provider = Provider(failure)
    budget, execute = run(tmp_path, video, provider)
    report = execute()
    assert report.status == "incomplete"
    assert budget.calls == len(provider.calls) == report.model_calls == 17
    assert provider.calls[-1][1] == "diagnosis"
    assert len([r for r in report.window_reactions if r.error]) == 1
    assert len([r for r in report.window_reactions if r.error is None]) == 15
    assert "private-provider-body" not in report.model_dump_json()
    for path in (tmp_path / "data" / "audit").rglob("*.json"):
        assert "private-provider-body" not in path.read_text()


def test_precision_hard_failure_preserves_coarse_reviews_and_skips_diagnosis(monkeypatch, tmp_path):
    video = setup(monkeypatch, tmp_path, duration=8000)
    provider = Provider(precision=True)
    _, execute = run(tmp_path, video, provider, precision=True)
    with pytest.raises(ProviderError):
        execute()
    saved = checkpoint(tmp_path)
    assert len(saved["coarse_reactions"]) == 4 and all(r["error"] is None for r in saved["coarse_reactions"])
    assert saved["precision_reactions"] and any(r["error"] for r in saved["precision_reactions"])
    assert saved["provider_stop"]["diagnosis_attempted"] is False
    assert all(kind == "window" for _, kind in provider.calls)


def test_spending_guard_denial_stops_downstream_audit(monkeypatch, tmp_path):
    from directorloop.providers.spend import SpendLimitExceeded

    video = setup(monkeypatch, tmp_path)

    class BudgetDenied(Provider):
        def judge_json(self, *args, **kwargs):
            raise SpendLimitExceeded("spending reservation limit reached; no request sent")

    _, execute = run(tmp_path, video, BudgetDenied())
    with pytest.raises(ProviderError, match="spending reservation limit"):
        execute()
    saved = checkpoint(tmp_path)
    assert saved["provider_stop"]["diagnosis_attempted"] is False
    assert saved["provider_stop"]["windows_unattempted"] >= 10


def test_closeup_quota_does_not_count_suppressed_checks_or_claim_complete_audit(monkeypatch, tmp_path):
    video = setup(monkeypatch, tmp_path, duration=6000)

    class CloseupQuota(ScriptedAuditProvider):
        closeup_calls = 0

        def judge_json(self, media, instruction, schema):
            if schema is R.CLOSEUP_SCHEMA:
                with self._lock:
                    self.closeup_calls += 1
                raise ProviderError("private-provider-body") from Rejection(429, "insufficient_quota")
            result = super().judge_json(media, instruction, schema)
            if schema is R.DIAG_SCHEMA:
                result.data["findings"] *= 10
            return result

    provider = CloseupQuota()
    budget, execute = run(tmp_path, video, provider)
    report = execute()
    assert report.status == "incomplete"
    assert 1 <= provider.closeup_calls <= 4
    assert report.model_calls == budget.calls == 4 + provider.closeup_calls
    assert report.call_usage["closeup"].calls == report.call_usage["closeup"].failed == provider.closeup_calls
    stage = next(s for s in report.workflow_stages if s.key == "closeup_checks")
    assert stage.summary["skipped"] == 10 - provider.closeup_calls
    assert stage.summary["provider_stop"]["diagnosis_attempted"] is True
    assert stage.status == "incomplete" and "private-provider-body" not in report.model_dump_json()
    skipped = [f for f in report.findings if "not attempted" in f.closeup_note]
    assert len(skipped) == 10 - provider.closeup_calls
