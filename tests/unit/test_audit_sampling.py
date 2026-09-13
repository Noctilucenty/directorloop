"""Frame budgets cover the whole interval they are meant to check, and the coverage record states the real sampling.
Found while analyzing real ads: close-ups kept only the first 16 frames (3.2 s) of a longer flagged span, so events at its
end were reported as "not visible", and the coverage text claimed 2 frames per second for a diagnosis capped at 40 frames."""

from __future__ import annotations

from typing import Any

import numpy as np

from directorloop.audit import review as R
from directorloop.creative.signals import VideoSignals
from directorloop.media.frames import SampledFrame
from directorloop.providers.base import CompletionResult, ProviderCapability


class FramesOnly:
    def __init__(self) -> None:
        self.times: list[int] = []

    def many(self, times: list[int], width: int) -> list[SampledFrame]:
        self.times = list(times)
        return [SampledFrame(t, b"jpeg", width, width * 2) for t in times]


class Recorder:
    capability = ProviderCapability(name="offline", role="probe", model="scripted")

    def __init__(self) -> None:
        self.media: Any = None

    def judge_json(self, media: Any, instruction: str, schema: dict[str, Any]) -> CompletionResult:
        self.media = media
        return CompletionResult(data={"happens": True, "start_s": 31.0, "end_s": 35.0, "confirmed_observations": [], "corrected_observations": [], "note": ""},
                                latency_ms=7, model="scripted", input_tokens=120, output_tokens=12)


def test_spread_keeps_both_ends_and_the_budget() -> None:
    times = list(range(0, 2200, 100))
    kept = R.spread(times, 16)
    assert len(kept) == 16 and kept[0] == 0 and kept[-1] == 2100
    assert R.spread(times[:5], 16) == times[:5]


def test_a_long_flagged_span_is_checked_to_its_end() -> None:
    frames = FramesOnly()
    finding = {"start_s": 31.2, "end_s": 34.8, "weakness": "the ending repeats the call to action", "observed": []}
    data, window, usage = R.closeup_verify(Recorder(), frames, [], finding, 35737)  # type: ignore[arg-type]
    assert len(frames.times) == R.CLOSEUP_MAX_FRAMES
    assert frames.times[0] < 31000 and frames.times[-1] > 34900, "the last 3 s of the span were previously never shown"
    assert window.frame_timestamps_ms == frames.times and (usage.input_tokens, usage.output_tokens, usage.latency_ms) == (120, 12, 7)


def test_diagnosis_frames_cover_the_whole_video_and_the_record_says_how_densely() -> None:
    times = R.diagnosis_frame_times(73957)
    assert len(times) == R.DIAG_MAX_FRAMES and times[0] < 1000 and times[-1] > 73000
    assert R.diagnosis_frame_times(14834) == R._times(0, 14834, R.DIAG_FPS), "a short video keeps 2 frames per second"


class ClampingFrames(FramesOnly):
    def __init__(self, duration_ms: int) -> None:
        super().__init__()
        self.duration_ms = duration_ms

    def many(self, times: list[int], width: int) -> list[SampledFrame]:
        return super().many([min(t, self.duration_ms - 60) for t in times], width)


def test_closeup_records_sent_timestamps_and_transcript_count_after_tail_clamping() -> None:
    frames, provider = ClampingFrames(2050), Recorder()
    words = [R.Word("before", 0, 100), R.Word("one", 1600, 1700), R.Word("two", 2000, 2050), R.Word("after", 2100, 2200)]
    _, window, _ = R.closeup_verify(provider, frames, words, {"start_s": 1.9, "end_s": 2.05, "observed": []}, 2050)  # type: ignore[arg-type]
    assert window.frame_timestamps_ms == [f.timestamp_ms for f in provider.media.frames] == [1600, 1800, 1990]
    assert window.transcript_words == 2


def test_diagnosis_records_sent_timestamps_after_tail_clamping() -> None:
    frames, provider = ClampingFrames(2290), Recorder()
    signals = VideoSignals(2290, 360, 640, [], [], np.array([], dtype=np.float32))
    _, window, _ = R.diagnostic_pass(provider, frames, signals, [], [], 2290)  # type: ignore[arg-type]
    assert window.frame_timestamps_ms == [f.timestamp_ms for f in provider.media.frames]
    assert window.frame_timestamps_ms[-1] == 2230


def test_prefix_records_context_timestamps_and_only_words_actually_in_the_prompt() -> None:
    frames = FramesOnly()
    signals = VideoSignals(4000, 360, 640, [], [], np.array([], dtype=np.float32))
    words = [R.Word(f"word{i}", i * 10, (i + 1) * 10) for i in range(70)]
    payload = R.build_viewer_payload(frames, signals, words, 2000, 4000, fps=30.0)  # type: ignore[arg-type]
    assert payload.window.context_frame_timestamps_ms == [500, 1500]
    assert payload.window.context_frames == len(payload.window.context_frame_timestamps_ms)
    assert payload.window.transcript_words == 60
    assert "word0\n" not in payload.instruction and "word10\n" in payload.instruction
