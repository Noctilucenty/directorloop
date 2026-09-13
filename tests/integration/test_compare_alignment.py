"""The comparison shows both versions at the same source moments. Found on a real run: a trimmed variant was sampled at
different moments than its original, and the judges read different caption states of identical footage as added text
and missing steps. Real ffmpeg renders and decoded frames; scripted judges; no model calls."""

from __future__ import annotations

import io
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from directorloop.audit import revise
from directorloop.audit.models import (
    COMPARISON_VERSION,
    AuditFinding,
    AuditReport,
    CoverageRecord,
    StabilityRecord,
)
from directorloop.audit.revise import aligned_sample_times, compare_versions, render_candidate
from directorloop.creative.mutate import identity_plan, source_manifest
from directorloop.domain.creative import BeatRole
from directorloop.domain.edit_plan import (
    CropRect,
    MoveSegment,
    RemoveSegment,
    SetCrop,
    TrimSegment,
    apply_ops,
)
from directorloop.domain.ids import sha256_file
from directorloop.media.probe import FFMPEG
from directorloop.providers.base import CompletionResult, ProviderCapability
from tests.unit.test_creative_mutations import make_genome

ROLES = [BeatRole.HOOK, BeatRole.CONTEXT, BeatRole.PROOF, BeatRole.PAYOFF]


@pytest.fixture(scope="module")
def clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("align") / "src.mp4"
    subprocess.run([FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=360x640:d=8:r=30",
                    "-f", "lavfi", "-i", "sine=frequency=330:duration=8", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
                   check=True, timeout=90)
    return path


class RecordingJudge:
    """No preference on every pairwise question, 'kept' for every protected item; keeps what each call was shown."""

    def __init__(self) -> None:
        self.capability = ProviderCapability(name="fake", role="probe", model="scripted", modalities={"text", "image"}, structured_output=True, docs_ref="", present=True)
        self.calls: list[tuple[str, Any]] = []

    def judge_json(self, media: Any, instruction: str, schema: dict[str, Any]) -> CompletionResult:
        self.calls.append((instruction, media))
        if "first decide its kind" in instruction:
            items = [{"item": line[2:], "kind": "content", "status": "kept", "evidence": "scripted"} for line in instruction.splitlines() if line.startswith("- ")]
            return CompletionResult(data={"items": items}, latency_ms=1, model="scripted")
        return CompletionResult(data={"choice": "no_preference", "reason": "same"}, latency_ms=1, model="scripted")


def gray(jpeg: bytes) -> np.ndarray:
    """A small grayscale copy, so a re-encoded render at a different output size compares with its source."""
    return np.asarray(Image.open(io.BytesIO(jpeg)).convert("L").resize((90, 160)), dtype=np.float32)


def test_a_catch_all_issue_type_alone_does_not_make_a_weakness_persist() -> None:
    def f(issue: str, objective: str) -> AuditFinding:
        return AuditFinding(id="x", version_id="v", start_ms=0, end_ms=1000, issue_type=issue, objective=objective)

    dead_tail = f("other", "attention")
    assert not revise.same_weakness(dead_tail, f("other", "emotional_payoff")), "found on a real run: payoff framing is not the dead tail"
    assert revise.same_weakness(dead_tail, f("other", "attention"))
    assert revise.same_weakness(f("visual_stagnation", "attention"), f("visual_stagnation", "comprehension"))
    assert not revise.same_weakness(f("visual_stagnation", "attention"), f("unclear_opening_or_subject", "comprehension"))


def test_sample_times_follow_the_edit_plan() -> None:
    base = identity_plan(make_genome("h" * 64, ROLES))
    trimmed = apply_ops(base, [TrimSegment(segment_id="beat_03", source_in_ms=6000, source_out_ms=6400)])
    o, c = aligned_sample_times(trimmed, 8000, 6400, count=8)
    assert o == [889, 1778, 2667, 3556, 4444, 5333, 6222, 7111] and c == [889, 1778, 2667, 3556, 4444, 5333, 6222], "the removed 6.4-8.0 s moment has no candidate frame"
    moved = apply_ops(base, [MoveSegment(segment_id="beat_01", after_segment_id="beat_02")])
    assert aligned_sample_times(moved, 8000, 8000, count=8)[1] == [889, 1778, 2444, 3333, 4667, 5556, 6222, 7111], "moved footage is shown where it now plays"
    cropped = apply_ops(base, [SetCrop(segment_id="beat_01", crop=CropRect(x=0, y=0, w=270, h=480))])
    assert aligned_sample_times(cropped, 8000, 8000, count=8) == (o, o)
    assert aligned_sample_times(apply_ops(base, [RemoveSegment(segment_id="beat_03")]), 8000, 6000, count=2) == ([2667, 5333], [2667, 5333])


def test_whole_video_and_protected_frames_show_the_same_footage_in_both_versions(clip: Path, tmp_path: Path) -> None:
    g = make_genome(sha256_file(clip), ROLES)
    plan = apply_ops(identity_plan(g), [RemoveSegment(segment_id="beat_03")])
    r = render_candidate(plan, source_manifest("p", clip, g.artifact_hash), clip, tmp_path)
    finding = AuditFinding(id="f", version_id="v0", start_ms=6000, end_ms=8000, weakness="the ending holds too long")
    orig = AuditReport(id="audit_1_1", video_id="t1", version_id="v0", artifact_hash=g.artifact_hash, artifact_path=str(clip), duration_ms=8000, created_at="t",
                       coverage=CoverageRecord(mode="sampled_frames_plus_asr", duration_ms=8000), findings=[finding])
    cand = AuditReport(id="audit_2_2", video_id="t1", version_id="v1", artifact_hash=r["artifact_hash"], artifact_path=r["path"], duration_ms=r["duration_ms"],
                       created_at="t", coverage=CoverageRecord(mode="sampled_frames_plus_asr", duration_ms=r["duration_ms"]))
    judge = RecordingJudge()
    comp = compare_versions(provider=judge, original=orig, candidate=cand, finding=finding, candidate_plan=plan, protected_items=["do not add new words or on-screen text"])  # type: ignore[arg-type]
    assert comp.comparison_version == COMPARISON_VERSION and comp.protected_unchecked == []
    shown = [media for instruction, media in judge.calls if "same short video" in instruction or "first decide its kind" in instruction]
    assert len(shown) == 5, "four whole-video judgments (two orders, twice) and one protected-content check"
    orders = set()
    for media in shown:
        first = {f.timestamp_ms: f for f in media.frames if f.timestamp_ms < 100000}
        second = {f.timestamp_ms - 100000: f for f in media.frames if f.timestamp_ms >= 100000}
        original_side, candidate_side = (first, second) if len(first) > len(second) else (second, first)  # both presentation orders occur
        orders.add(len(first) > len(second))
        assert sorted(original_side) == [889, 1778, 2667, 3556, 4444, 5333, 6222, 7111] and sorted(candidate_side) == [889, 1778, 2667, 3556, 4444, 5333]
        for t, frame in candidate_side.items():
            same = float(np.abs(gray(frame.jpeg) - gray(original_side[t].jpeg)).mean())
            other = float(np.abs(gray(frame.jpeg) - gray(original_side[sorted(original_side)[(sorted(original_side).index(t) + 3) % 8]].jpeg)).mean())
            assert same < 2.0 and other > 2 * same + 2.0, f"at {t} ms both versions must show the same picture (diff {same:.1f}), unlike another moment (diff {other:.1f})"
    assert orders == {True, False}


def test_a_target_moment_the_original_wins_is_a_regression(clip: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    g = make_genome(sha256_file(clip), ROLES)
    plan = apply_ops(identity_plan(g), [SetCrop(segment_id="beat_01", crop=CropRect(x=40, y=72, w=276, h=492))])
    finding = AuditFinding(id="f", version_id="v0", start_ms=2000, end_ms=4000, weakness="the picture holds")
    orig = AuditReport(id="audit_1_1", video_id="t1", version_id="v0", artifact_hash=g.artifact_hash, artifact_path=str(clip), duration_ms=8000, created_at="t",
                       coverage=CoverageRecord(mode="sampled_frames_plus_asr", duration_ms=8000), findings=[finding])
    cand = orig.model_copy(update={"id": "audit_2_2", "version_id": "v1", "findings": []})
    agree = StabilityRecord(runs=2, agreeing_runs=2, order_flips=0, agreement="2 of 2 order-swapped pairs agreed")

    def scripted(provider: Any, original: Any, candidate: Any, question: str, repeats: int = 2) -> tuple[float, StabilityRecord, list[str]]:
        return (0.0, agree, ["original: the zoom cuts off the action"]) if "same moment" in question else (0.5, agree, ["tie: same"])

    monkeypatch.setattr(revise, "pairwise", scripted)
    comp = compare_versions(provider=RecordingJudge(), original=orig, candidate=cand, finding=finding, candidate_plan=plan, protected_items=[])  # type: ignore[arg-type]
    assert "target moment 2.0-4.0s: the original was preferred (0.00 for the candidate; 2 of 2 order-swapped pairs agreed)" in comp.regressed
    assert comp.outcome == "regression" and comp.target_resolved != "yes"
