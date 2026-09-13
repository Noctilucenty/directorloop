"""Coordinator persistence and bounded-dispatch tests without model inference."""

from __future__ import annotations

import copy
import threading
from collections import Counter
from contextvars import ContextVar
from dataclasses import replace
from types import SimpleNamespace

import pytest

from directorloop.domain.ids import sha256_bytes
from directorloop.providers.base import ProviderError
from directorloop.screening import repair, repair_loop, runner
from directorloop.screening.models import ScreenReport
from tests.unit.test_screening_repair import inputs


def report_inputs(tmp_path, count):
    windows, payloads = [], []
    for index in range(count):
        window, payload = inputs()
        delta = index * 4000
        window.start_ms += delta
        window.end_ms += delta
        window.frame_timestamps_ms = [t + delta for t in window.frame_timestamps_ms]
        for observation in window.judgment.observations:
            observation.frame_timestamps_ms = [t + delta for t in observation.frame_timestamps_ms]
        payload.media.frames = [replace(frame, timestamp_ms=frame.timestamp_ms + delta) for frame in payload.media.frames]
        for evidence, frame in zip(window.evidence_frames, payload.media.frames, strict=True):
            path = tmp_path / f"{index}-{frame.timestamp_ms}.jpg"
            path.write_bytes(frame.jpeg)
            evidence.t_ms, evidence.path = frame.timestamp_ms, str(path)
        payload.window.start_ms += delta
        payload.window.end_ms += delta
        payload.window.latest_word_end_ms += delta
        payload.media.duration_ms += delta
        windows.append(window)
        payloads.append(payload)
    return ScreenReport(id="screen_offline_repairs", video_id="fixture", artifact_path="/fixture/video.mp4",
                        created_at="2026-09-13T00:00:00Z", duration_ms=windows[-1].end_ms,
                        windows=windows, model_calls=count, status="needs_review"), payloads


class OfflineProvider:
    def __init__(self, fail_at=None):
        self.calls = []
        self.fail_at = fail_at
        self.lock = threading.Lock()

    def perform(self, start_ms):
        with self.lock:
            self.calls.append(start_ms)
        if start_ms == self.fail_at:
            raise ProviderError("provider exhausted")


def stub_repair(monkeypatch, recorded):
    monkeypatch.setattr(repair, "repair_needed", lambda window: bool(window.validation_issues))

    def perform(provider, payload, window, *, checkpoint):
        candidate = window.model_copy(deep=True)
        candidate.status, candidate.raw_output = "pending", {}
        candidate.input_tokens = candidate.output_tokens = None
        candidate.instruction_sha256 = "new-instruction-hash"
        candidate.payload_sha256 = "same-evidence-new-request-hash"
        checkpoint(candidate)
        # Provider dispatch can occur only after durable original+repair snapshots.
        matching = [s for s in recorded if any(w["start_ms"] == window.start_ms and w["review_attempts"] for w in s["windows"])]
        assert matching
        stored = next(w for w in matching[-1]["windows"] if w["start_ms"] == window.start_ms)
        assert stored["review_attempts"][0]["phase"] == "initial"
        assert stored["review_attempts"][-1]["window"]["instruction_sha256"] == "new-instruction-hash"
        provider.perform(window.start_ms)
        candidate.status, candidate.validation_issues = "complete", []
        candidate.raw_output = {"corrected": True}
        candidate.input_tokens, candidate.output_tokens = 17, 3
        return candidate

    monkeypatch.setattr(repair, "repair_screen_window", perform)
    monkeypatch.setattr(repair, "focused_repair_screen_window", perform)


def test_parallel_corrections_persist_before_dispatch_and_keep_each_original(tmp_path, monkeypatch):
    report, payloads = report_inputs(tmp_path, 3)
    originals = [w.model_dump(mode="json", exclude={"review_attempts"}) for w in report.windows]
    receipts = []
    stub_repair(monkeypatch, receipts)
    provider = OfflineProvider()
    repair_loop.repair_selected_windows(report, payloads, provider, lambda: receipts.append(report.model_dump(mode="json")), lambda: None)
    assert len(provider.calls) == 3 and report.model_calls == 6
    assert report.review_repair["completed_sections"] == 3
    for window, original in zip(report.windows, originals, strict=True):
        assert window.status == "complete"
        assert window.review_attempts[0]["window"] == original
        assert window.review_attempts[1]["accepted"] is True
        assert window.review_attempts[0]["window"]["input_tokens"] == 111
        assert window.review_attempts[1]["window"]["input_tokens"] == 17
        for evidence in window.evidence_frames:
            from pathlib import Path

            assert sha256_bytes(Path(evidence.path).read_bytes()) == evidence.sha256
    # History is the spend boundary: a second invocation cannot dispatch again.
    repair_loop.repair_selected_windows(report, payloads, provider, lambda: receipts.append(report.model_dump(mode="json")), lambda: None)
    assert len(provider.calls) == 3 and report.model_calls == 6


def test_provider_failure_keeps_original_and_stops_new_requests(tmp_path, monkeypatch):
    report, payloads = report_inputs(tmp_path, 3)
    original = report.windows[0].model_dump(mode="json", exclude={"review_attempts"})
    receipts = []
    stub_repair(monkeypatch, receipts)
    monkeypatch.setattr(runner, "MAX_FULL_SCREEN_WORKERS", 1)
    provider = OfflineProvider(fail_at=report.windows[0].start_ms)
    with pytest.raises(ProviderError):
        repair_loop.repair_selected_windows(report, payloads, provider, lambda: receipts.append(report.model_dump(mode="json")), lambda: None)
    assert len(provider.calls) == 1 and report.model_calls == 4
    assert report.windows[0].model_dump(mode="json", exclude={"review_attempts"}) == original
    assert report.windows[0].review_attempts[1]["window"]["status"] == "failed"
    assert report.windows[0].review_attempts[1]["window"]["input_tokens"] is None
    assert all(not w.review_attempts for w in report.windows[1:])
    assert report.review_repair["status"] == "partial"


def test_pre_dispatch_persistence_failure_sends_no_request_or_false_attempt(tmp_path, monkeypatch):
    report, payloads = report_inputs(tmp_path, 1)
    receipts = []
    stub_repair(monkeypatch, receipts)
    provider = OfflineProvider()

    def checkpoint():
        if report.windows[0].review_attempts:
            raise OSError("durable store unavailable")
        receipts.append(report.model_dump(mode="json"))

    with pytest.raises(OSError, match="durable store unavailable"):
        repair_loop.repair_selected_windows(report, payloads, provider, checkpoint, lambda: None)
    assert provider.calls == []
    assert report.model_calls == 1
    assert report.windows[0].review_attempts == []


def test_explicit_focused_upgrade_keeps_initial_and_failed_full_attempt_and_runs_only_once(tmp_path, monkeypatch):
    report, payloads = report_inputs(tmp_path, 1)
    original = report.windows[0].model_dump(mode="json", exclude={"review_attempts"})
    failed_full = copy.deepcopy(original)
    failed_full["raw_output"] = {"full_repair": "still insufficient"}
    failed_full["input_tokens"], failed_full["output_tokens"] = 70, 20
    history = [
        {"phase": "initial", "accepted": True, "window": original},
        {"phase": "repair", "accepted": False, "window": failed_full},
    ]
    report.windows[0].review_attempts = copy.deepcopy(history)
    report.model_calls = 2
    receipts = []
    stub_repair(monkeypatch, receipts)
    provider = OfflineProvider()
    repair_loop.repair_selected_windows(report, payloads, provider,
        lambda: receipts.append(report.model_dump(mode="json")), lambda: None, focused=True, upgrade=True)
    assert len(provider.calls) == 1 and report.model_calls == 3
    assert report.windows[0].review_attempts[:2] == history
    assert report.windows[0].review_attempts[2]["phase"] == "focused_repair"
    assert report.windows[0].review_attempts[2]["accepted"] is True
    # A subsequent validation issue cannot silently authorize another focused pass.
    report.windows[0].validation_issues = ["Still disputed"]
    report.windows[0].status = "needs_review"
    repair_loop.repair_selected_windows(report, payloads, provider,
        lambda: receipts.append(report.model_dump(mode="json")), lambda: None, focused=True, upgrade=True)
    assert len(provider.calls) == 1 and report.model_calls == 3
    assert len(report.windows[0].review_attempts) == 3


def test_missing_payload_inventory_fails_before_spending_or_history_changes(tmp_path):
    report, payloads = report_inputs(tmp_path, 2)
    before = report.model_dump_json()
    provider = OfflineProvider()
    with pytest.raises(ValueError, match="inventory"):
        repair_loop.repair_selected_windows(report, payloads[:1], provider, lambda: None, lambda: None)
    assert provider.calls == []
    assert report.model_dump_json() == before


def test_saved_payload_reconstructs_original_bytes_and_rejects_tampering(tmp_path):
    from pathlib import Path

    report, _ = report_inputs(tmp_path, 1)
    window = report.windows[0]
    before = copy.deepcopy(window.model_dump())
    payload = repair_loop.saved_payload(window)
    assert payload.media.transcript == window.prefix_asr_text
    assert payload.media.duration_ms == window.end_ms
    assert [f.timestamp_ms for f in payload.media.frames] == window.frame_timestamps_ms
    assert window.model_dump() == before
    Path(window.evidence_frames[0].path).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="evidence changed"):
        repair_loop.saved_payload(window)


def test_saved_payload_can_feed_the_single_pass_reviewer(tmp_path):
    from tests.unit.test_screening_repair import Provider

    report, _ = report_inputs(tmp_path, 1)
    window = report.windows[0]
    fixed = repair.repair_screen_window(Provider(), repair_loop.saved_payload(window), window)
    assert fixed.status == "complete"


def test_full_runner_repairs_once_under_same_workflow_and_totals_both_attempts(tmp_path, monkeypatch):
    from directorloop.observability.workflow import _parent
    from directorloop.providers.base import CompletionResult
    from tests.unit.test_screening import Frames
    from tests.unit.test_screening_repair import judgment

    source, data_dir = tmp_path / "source.mp4", tmp_path / "data"
    source.write_bytes(b"immutable offline fixture")
    monkeypatch.setattr(runner, "inspect_media", lambda _: SimpleNamespace(
        duration_ms=6000, fps=30, width=448, height=796, has_audio=False))
    monkeypatch.setattr(runner, "FrameCache", Frames)
    marker = ContextVar("repair_root_test", default=None)
    token = marker.set("one-original-session")
    writer_threads = []
    original_persist = runner._persist

    def persist(report, directory):
        writer_threads.append(threading.get_ident())
        original_persist(report, directory)

    monkeypatch.setattr(runner, "_persist", persist)

    class FullProvider:
        capability = SimpleNamespace(name="offline", model="local-test-double")
        max_output_tokens = 2048
        enable_thinking = False

        def __init__(self):
            self.calls = []
            self.lock = threading.Lock()

        def judge_json(self, media, instruction, schema):
            focused = "This focused pass does not repeat" in instruction
            is_repair = "This is one independent correction pass" in instruction or focused
            with self.lock:
                self.calls.append((is_repair, media.duration_ms))
            assert marker.get() == "one-original-session"
            assert _parent.get().key == ("screening_repair" if is_repair else "screening_prefix")
            saved = runner.load_screening(data_dir, "screen_full_repair_test")
            stored = next(w for w in saved.windows if w.end_ms == media.duration_ms)
            if is_repair:
                attempt = stored.review_attempts[-1]["window"]
                assert attempt["response_schema_sha256"] == runner.sha256_json(schema)
                assert attempt["payload_sha256"] and attempt["instruction_sha256"]
            data = judgment()
            current_time = media.frames[-1].timestamp_ms
            data["observations"] = [
                {"kind": "visible_fact", "text": "The current screen shows a file picker.",
                 "frame_timestamps_ms": [current_time], "asr_quote": None},
                {"kind": "caption_claim", "text": "The caption reads Upload media.",
                 "frame_timestamps_ms": [current_time], "asr_quote": None},
            ]
            data["cause_observation_indices"] = [0]
            for check in data["review_checks"]:
                check["observation_indices"] = [0]
                if check["aspect"] == "caption_readability":
                    check["observation_indices"] = [1] if is_repair else [0]
                elif check["aspect"] in {"caption_alignment", "tone_from_words", "voice_delivery"}:
                    check.update(status="unknown", observation_indices=[], reason="Necessary audio evidence is unavailable.")
            for score in data["potential_scores"]:
                score["observation_indices"] = [0]
            if focused:
                data.pop("review_checks")
            return CompletionResult(data=data, latency_ms=12, model="local-test-double",
                                    input_tokens=40 if is_repair else 100,
                                    output_tokens=20 if is_repair else 50)

    provider = FullProvider()
    try:
        report = runner.run_screening("fixture", source, provider, data_dir,
            screening_id="screen_full_repair_test", coverage="full", repair_incomplete=True)
    finally:
        marker.reset(token)
    assert report.status == "complete", report.error
    assert report.model_calls == 6 and len(provider.calls) == 6
    assert Counter(provider.calls) == Counter((phase, end) for phase in [False, True] for end in [2000, 4000, 6000])
    assert report.input_tokens == 420 and report.output_tokens == 210
    assert report.review_repair["completed_sections"] == 3
    assert all(len(w.review_attempts) == 2 for w in report.windows)
    assert all(w.review_attempts[0]["window"]["status"] == "needs_review" for w in report.windows)
    assert all(w.status == "complete" and w.review_attempts[1]["accepted"] for w in report.windows)
    assert set(writer_threads) == {threading.get_ident()}
    stages = [s for s in report.workflow_stages if s.key in {"screening_prefix", "screening_repair"}]
    assert len(stages) == 6 and all(s.status == "completed" for s in stages)
    assert len({s.id for s in stages}) == 6
    assert runner.load_screening(data_dir, report.id) == report
    assert source.read_bytes() == b"immutable offline fixture"
    with pytest.raises(FileExistsError):
        runner.run_screening("fixture", source, provider, data_dir,
            screening_id=report.id, coverage="full", repair_incomplete=True)
    assert len(provider.calls) == 6
