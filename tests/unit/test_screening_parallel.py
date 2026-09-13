"""Offline concurrency, durable dispatch, and information-boundary regressions."""

from __future__ import annotations

import threading
import time
from contextvars import ContextVar
from types import SimpleNamespace

import pytest

from directorloop.jobs.worker import JobInterrupted
from directorloop.observability.workflow import _parent
from directorloop.providers.base import ProviderError
from directorloop.screening import runner
from directorloop.screening.models import ScreenJudgment
from tests.unit.test_screening import Frames, Provider, reply


@pytest.fixture
def full_media(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"immutable synthetic media")
    monkeypatch.setattr(runner, "inspect_media", lambda _: SimpleNamespace(
        duration_ms=31637, fps=30, width=448, height=796, has_audio=False))
    monkeypatch.setattr(runner, "FrameCache", Frames)
    return source, tmp_path / "data"


def checked_reply(media):
    return reply(media.frames[-1].timestamp_ms, review_checks=[
        {"aspect": aspect, "status": "unknown", "reason": "Evidence is insufficient.", "observation_indices": []}
        for aspect in sorted(runner.FULL_REVIEW_ASPECTS)
    ])


class ParallelProvider(Provider):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback
        self.lock = threading.Lock()
        self.first_three = threading.Barrier(3, timeout=5)
        self.active = self.peak = self.started = 0
        self.finished = []

    def judge_json(self, media, instruction, schema):
        with self.lock:
            self.started += 1
            order = self.started
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.calls.append((media, instruction, schema))
        try:
            if order <= 3:
                self.first_three.wait()
            self.callback(order, media, instruction, schema)
            from directorloop.providers.base import CompletionResult
            return CompletionResult(data=checked_reply(media), latency_ms=12, model="Qwen/test", input_tokens=100, output_tokens=50)
        finally:
            with self.lock:
                self.active -= 1
                self.finished.append(media.duration_ms)


def test_full_fanout_is_bounded_ordered_prefix_safe_and_has_one_report_writer(full_media, monkeypatch):
    source, data_dir = full_media
    marker = ContextVar("screening_test_marker", default=None)
    token = marker.set("root-context")
    writer_threads = []
    original_persist = runner._persist

    def persist(report, directory):
        writer_threads.append(threading.get_ident())
        original_persist(report, directory)

    monkeypatch.setattr(runner, "_persist", persist)

    def validate_dispatch(order, media, instruction, schema):
        assert marker.get() == "root-context"
        assert _parent.get().key == "screening_prefix"
        assert all(frame.timestamp_ms < media.duration_ms for frame in media.frames)
        assert schema is not runner.FULL_SCREENING_SCHEMA
        assert schema["properties"].keys() == runner.FULL_SCREENING_SCHEMA["properties"].keys()
        assert "Per-interval risk is not cumulative audience survival" in instruction
        # Dispatch cannot occur until this worker's exact inputs are durable.
        saved = runner.load_screening(data_dir, "screen_parallel")
        window = next(w for w in saved.windows if w.end_ms == media.duration_ms)
        assert window.instruction_sha256 and window.payload_sha256
        assert window.response_schema_sha256 == runner.sha256_json(schema)
        assert saved.model_calls >= order
        if order <= 3:
            time.sleep((4 - order) * 0.02)

    provider = ParallelProvider(validate_dispatch)
    try:
        report = runner.run_screening("video", source, provider, data_dir, coverage="full", screening_id="screen_parallel")
    finally:
        marker.reset(token)
    assert report.status == "complete", report.error
    assert provider.peak == 3 and provider.started == report.model_calls == 8
    assert report.protocol["max_concurrent_prefix_calls"] == 3
    assert report.protocol["version"].endswith("full-timeline-v7")
    ends = [window.end_ms for window in report.windows]
    assert ends == sorted(ends) and provider.finished != ends
    assert all(window.status == "complete" for window in report.windows)
    assert report.input_tokens == 800 and report.output_tokens == 400
    assert set(writer_threads) == {threading.get_ident()}
    stages = [stage for stage in report.workflow_stages if stage.key == "screening_prefix"]
    assert len(stages) == 8 and all(stage.status == "completed" for stage in stages)
    assert sorted(stage.inputs["end_ms"] for stage in stages) == ends
    assert runner.load_screening(data_dir, report.id) == report


def test_full_failure_stops_new_dispatch_and_preserves_inflight_successes(full_media):
    source, data_dir = full_media

    def fail_first(order, *_):
        if order == 1:
            raise ProviderError("synthetic provider unavailable")
        time.sleep(0.06)

    provider = ParallelProvider(fail_first)
    report = runner.run_screening("video", source, provider, data_dir, coverage="full")
    assert report.status == "failed"
    assert report.model_calls == provider.started == 3
    assert sum(window.status == "failed" for window in report.windows) == 1
    assert sum(window.status == "complete" for window in report.windows) == 2
    assert all(window.status == "not_attempted" for window in report.windows[3:])
    assert report.input_tokens is None  # The failed call's billed usage is unknown.
    assert provider.active == 0
    stages = [stage for stage in report.workflow_stages if stage.key == "screening_prefix"]
    assert len(stages) == 3 and sum(stage.status == "failed" for stage in stages) == 1
    assert all(stage.ended_at for stage in stages)


def test_full_cancellation_drains_existing_calls_without_dispatching_more(full_media):
    source, data_dir = full_media
    cancel = threading.Event()

    def cancel_after_start(order, *_):
        cancel.set()
        time.sleep(0.06)

    provider = ParallelProvider(cancel_after_start)
    report = runner.run_screening("video", source, provider, data_dir, coverage="full", is_cancelled=cancel.is_set)
    assert report.status == "canceled"
    assert provider.started == report.model_calls == 3
    assert all(window.status == "complete" for window in report.windows[:3])
    assert all(window.status == "not_attempted" for window in report.windows[3:])
    assert provider.active == 0
    assert report.input_tokens == 300


def test_full_lease_loss_before_dispatch_does_not_request_a_provider(full_media):
    source, data_dir = full_media
    provider = Provider()

    def lost_lease(stage, *_):
        if stage == "SCREENING_PREFIX":
            raise JobInterrupted("synthetic lease lost")

    report = runner.run_screening("video", source, provider, data_dir, coverage="full", on_stage=lost_lease)
    assert report.status == "failed"
    assert report.model_calls == 0 and provider.calls == []
    assert all(window.status == "not_attempted" for window in report.windows)


def test_full_durable_input_failure_never_dispatches_provider(full_media, monkeypatch):
    source, data_dir = full_media
    provider = Provider()
    original_persist = runner._persist
    failed = False

    def fail_input_once(report, directory):
        nonlocal failed
        if not failed and any(window.payload_sha256 for window in report.windows):
            failed = True
            raise OSError("synthetic input checkpoint unavailable")
        original_persist(report, directory)

    monkeypatch.setattr(runner, "_persist", fail_input_once)
    report = runner.run_screening("video", source, provider, data_dir, coverage="full")
    assert failed and report.status == "failed"
    assert provider.calls == [] and report.model_calls == 0
    assert all(window.status == "not_attempted" for window in report.windows)


def test_invalid_observation_cannot_support_a_clear_review_dimension():
    judgment = ScreenJudgment.model_validate(reply(999, review_checks=[
        {"aspect": "visual_clarity", "status": "clear", "reason": "The object is visible.", "observation_indices": [0]},
    ]))
    issues = runner.validate_evidence(judgment, [166], "")
    assert "Observation 1: frame citation was not supplied" in issues
    assert "Review check visual_clarity: supporting observation failed evidence validation" in issues


def test_full_review_distinguishes_intermediate_cutoff_from_source_ending(full_media):
    source, data_dir = full_media
    provider = Provider(lambda _index, media: checked_reply(media))
    report = runner.run_screening("video", source, provider, data_dir, coverage="full")
    assert report.status == "complete"
    instructions = {media.duration_ms: instruction for media, instruction, _schema in provider.calls}
    for window in report.windows:
        instruction = instructions[window.end_ms]
        if window.is_last_prefix:
            assert "FINAL CHECKPOINT: This cutoff is the end of the source video" in instruction
            assert "INTERMEDIATE CHECKPOINT" not in instruction
        else:
            assert "INTERMEDIATE CHECKPOINT: The source video continues beyond this evidence cutoff" in instruction
            assert "not the video's ending or an editing problem" in instruction
            assert "An unobserved later payoff remains unknown" in instruction
            assert "FINAL CHECKPOINT" not in instruction
        # The boundary metadata adds no withheld frames or continuation content.
        media = next(media for media, _, _ in provider.calls if media.duration_ms == window.end_ms)
        assert all(frame.timestamp_ms < window.end_ms for frame in media.frames)
