"""Temporal attention analysis: deterioration detection, precision planning, timeline events and regions, expectation
threads, the cold viewer's information boundary, precision failures, A/B timeline comparison and the no-repair stop.
No network and no model calls; scripted providers stand in for the reviewer."""

from __future__ import annotations

import copy
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from directorloop.audit import attention as A
from directorloop.audit import review as R
from directorloop.audit.models import (
    AuditFinding,
    AuditReport,
    CoverageRecord,
    EvaluatorRecord,
    WindowReaction,
)
from directorloop.audit.revise import attention_evidence
from directorloop.creative.mutate import identity_plan
from directorloop.creative.signals import VideoSignals
from directorloop.domain.creative import BeatRole
from directorloop.domain.edit_plan import RemoveSegment, apply_ops
from directorloop.media.frames import SampledFrame
from directorloop.media.transcribe import Transcript, TranscriptSegment, TranscriptToken
from directorloop.providers.base import CompletionResult, ProbeMedia, ProviderCapability, ProviderError
from directorloop.runtime.budget import BudgetExceeded
from tests.unit.test_creative_mutations import make_genome

CFG = A.AttentionConfig()
EVAL = EvaluatorRecord(cold_viewer_prompt_version=A.COLD_VIEWER_PROMPT_VERSION, provider="offline", model="scripted", coarse_window_ms=2000, precision_enabled=True,
                       precision_window_ms=500, precision_lead_ms=500, max_precision_regions=2, max_precision_windows=10)


def w(start: int, end: int, risk: str, reaction: str = "engaged", scan: str = "coarse", **kw: Any) -> WindowReaction:
    return WindowReaction(start_ms=start, end_ms=end, attention_risk=risk, reaction=reaction, scan=scan, cause=kw.pop("cause", f"cause at {start}"), **kw)  # type: ignore[arg-type]


def coarse(*spec: str, last_end: int | None = None) -> list[WindowReaction]:
    out = []
    for i, s in enumerate(spec):
        risk, _, reaction = s.partition("/")
        end = (i + 1) * 2000 if (last_end is None or i < len(spec) - 1) else last_end
        out.append(w(i * 2000, end, risk, reaction or "engaged"))
    return out


def timeline(c: list[WindowReaction], p: list[WindowReaction] | None = None, duration: int | None = None, scans: list[Any] | None = None) -> Any:
    return A.build_attention_timeline(c, p or [], scans or [], duration or c[-1].end_ms, EVAL)


# ----------------------------------------------------------------------------- A-F: detection, planning, regions


def test_a_all_low_has_no_region_and_no_precision_scan() -> None:
    c = coarse("low", "low", "low")
    assert A.detect_deterioration(c) == []
    assert A.plan_precision(c, 6000, CFG) == []
    t = timeline(c)
    assert t.failure_regions == [] and t.summary.peak_risk == "low" and not t.summary.first_risk_increase
    assert not [e for e in t.events if e.type in ("risk_increase", "predicted_dropoff", "initial_high_risk")]
    assert t.summary.text.startswith("Low predicted attention risk in all 3")


def test_b_rise_is_detected_and_precision_covers_the_transition() -> None:
    c = coarse("low", "low", "medium/neutral", "high/losing_interest")
    triggers = A.detect_deterioration(c)
    assert [i for i, _ in triggers] == [2, 3]
    plans = A.plan_precision(c, 8000, CFG)
    assert len(plans) == 1, "overlapping regions around consecutive rises are merged into one scan"
    scan = plans[0]
    assert scan.scan.region.start_ms == 3500 and scan.scan.region.end_ms == 8000
    assert scan.windows[0] == (3500, 4000) and all(b - a == 500 for a, b in scan.windows)
    assert len(scan.windows) == len(set(scan.windows)) <= CFG.max_precision_windows


def test_c_failure_region_with_precision_onset_high_onset_and_recovery() -> None:
    c = coarse("low", "medium/neutral", "high/losing_interest", "high/losing_interest", "low")
    precision = [w(1500, 2000, "low", scan="precision"), w(2000, 2500, "low", scan="precision"), w(2500, 3000, "medium", "neutral", scan="precision"),
                 w(3000, 3500, "medium", "neutral", scan="precision"), w(3500, 4000, "high", "losing_interest", scan="precision"), w(4000, 4500, "high", "losing_interest", scan="precision")]
    t = timeline(c, precision)
    assert len(t.failure_regions) == 1
    r = t.failure_regions[0]
    assert (r.onset.start_ms, r.onset.end_ms) == (2500, 3000) and r.refined_by_precision and r.temporal_resolution_ms == 500
    assert r.high_risk_onset is not None and (r.high_risk_onset.start_ms, r.high_risk_onset.end_ms) == (3500, 4000)
    assert (r.start_ms, r.end_ms) == (2500, 8000) and r.severity == "high" and r.failure_type == "interest_loss"
    assert r.recovered and r.recovery is not None and (r.recovery.start_ms, r.recovery.end_ms) == (8000, 10000)
    assert r.later_content_needs_survival, "a return to low after high risk is only reached by viewers who stayed"
    assert r.viewer_before is not None and r.viewer_before.attention_risk == "low"
    types = [e.type for e in t.events]
    assert "risk_increase" in types and "predicted_dropoff" in types and "recovery" in types and "interest_loss" in types
    segs = [(s.start_ms, s.end_ms, s.scan) for s in t.segments]
    assert segs[0] == (0, 1500, "coarse") and (2500, 3000, "precision") in segs and segs[-1] == (8000, 10000, "coarse")
    assert all(a.end_ms == b.start_ms for a, b in zip(t.segments, t.segments[1:], strict=False)), "segments tile the timeline without overlap"
    assert t.summary.first_high_risk is not None and t.summary.first_high_risk.start_ms == 3500


def test_d_video_that_opens_at_high_risk_needs_no_transition() -> None:
    c = coarse("high/losing_interest", "high/losing_interest", "high/losing_interest")
    assert A.detect_deterioration(c)[0][0] == 0
    plans = A.plan_precision(c, 6000, CFG)
    assert plans[0].windows == [(0, 500), (500, 1000), (1000, 1500), (1500, 2000)]
    t = timeline(c)
    assert t.events[0].type == "initial_high_risk"
    r = t.failure_regions[0]
    assert r.failure_type == "hook_failure" and r.start_ms == 0 and not r.recovered and t.summary.starts_elevated and t.summary.ends_elevated


def test_e_separated_problems_stay_separate_and_regions_are_limited() -> None:
    c = coarse("low", "high", "low", "high")
    plans = A.plan_precision(c, 8000, CFG)
    assert [(p.scan.region.start_ms, p.scan.region.end_ms) for p in plans] == [(1500, 4000), (5500, 8000)]
    t = timeline(c)
    assert [(r.start_ms, r.end_ms) for r in t.failure_regions] == [(2000, 4000), (6000, 8000)]
    three = coarse("low", "high", "low", "high", "low", "high")
    planned = A.plan_precision(three, 12000, CFG)
    assert sum(1 for p in planned if p.scan.status != "skipped") == 2
    skipped = [p for p in planned if p.scan.status == "skipped"]
    assert len(skipped) == 1 and "not scanned" in skipped[0].scan.skipped_reason and not skipped[0].windows


def test_f_overlapping_regions_merge_and_the_window_budget_keeps_the_onset() -> None:
    c = coarse("low", "medium", "high", "high")
    plans = A.plan_precision(c, 8000, CFG)
    assert len(plans) == 1 and (plans[0].scan.region.start_ms, plans[0].scan.region.end_ms) == (1500, 6000)
    tight = A.plan_precision(c, 8000, A.AttentionConfig(max_precision_windows=4))
    windows = [win for p in tight for win in p.windows]
    assert len(windows) == 4 and windows[0] == (1500, 2000), "the budget keeps the windows nearest the first rise"


def test_steady_medium_or_curiosity_alone_triggers_nothing() -> None:
    c = [w(0, 2000, "medium", "neutral"), w(2000, 4000, "medium", "neutral"), w(4000, 6000, "low", "engaged", open_question="What happens to the top?")]
    assert A.detect_deterioration(c) == []
    t = timeline(c)
    assert [e.type for e in t.events] == ["initial_elevated_risk", "recovery"], "a return from medium to low is a recovery, not a new problem"
    curious = [w(0, 2000, "low", open_question="Why is it rising?"), w(2000, 4000, "low", open_question="How does it stand up?")]
    assert timeline(curious).events == [] and timeline(curious).failure_regions == []


# ----------------------------------------------------------------------------- G-H: windows at the edges


def test_g_very_short_video_windows_stay_inside_the_video() -> None:
    assert R.plan_windows(1200) == [(0, 1200)]
    c = [w(0, 1200, "high", "confused")]
    plans = A.plan_precision(c, 1200, CFG)
    assert plans[0].windows == [(0, 500), (500, 1000), (1000, 1200)]
    for a, b in plans[0].windows:
        _, cur = R.viewer_frame_times(a, b, fps=30, precision_frames=3)
        assert cur and all(a <= t and t + R.frame_guard_ms(30) <= b for t in cur)


def test_h_last_partial_window_keeps_true_times_and_never_scans_past_the_end() -> None:
    wins = R.plan_windows(11100)
    assert wins[-1] == (10000, 11100)
    c = [w(a, b, "low") for a, b in wins[:-1]] + [w(10000, 11100, "medium", "neutral")]
    plans = A.plan_precision(c, 11100, CFG)
    assert plans[0].windows[-1] == (10500, 11100) and all(b <= 11100 for _, b in plans[0].windows)
    t = timeline(c, duration=11100)
    assert t.segments[-1].end_ms == 11100 and t.failure_regions[0].end_ms == 11100 and t.summary.ends_elevated


def test_grid_windows_fold_a_sliver_into_the_previous_window() -> None:
    assert A.grid_windows(10000, 11100, 500, 11100) == [(10000, 10500), (10500, 11100)]
    assert A.grid_windows(0, 700, 500, 700) == [(0, 500), (500, 700)]
    assert A.grid_windows(0, 600, 500, 600) == [(0, 600)]


# ----------------------------------------------------------------------------- expectation threads and cause regions


def test_expectation_thread_supplies_a_cause_region_without_matching_wording() -> None:
    c = [
        w(0, 2000, "low", payoff="waiting", waiting_since_ms=1700, expectation="I want to know what they found"),
        w(2000, 4000, "low", payoff="waiting", waiting_since_ms=1500, expectation="Still waiting for the discovery"),
        w(4000, 6000, "medium", "neutral", payoff="waiting", waiting_since_ms=1800, expectation="The result, finally?"),
        w(6000, 8000, "high", "losing_interest", payoff="waiting", waiting_since_ms=1700),
        w(8000, 10000, "low", payoff="delivered"),
    ]
    t = timeline(c)
    assert len(t.expectation_threads) == 1
    th = t.expectation_threads[0]
    assert th.set_up_ms == 1700 and th.resolved is not None and th.resolved.start_ms == 8000
    assert th.elevated_while_waiting is not None and th.elevated_while_waiting.start_ms == 4000
    r = t.failure_regions[0]
    assert r.cause_region is not None and (r.cause_region.start_ms, r.cause_region.end_ms) == (1700, 4000) and "waiting since 1.7s" in r.cause_basis
    assert any(e.type == "unresolved_expectation" for e in t.events) and any(e.type == "payoff_delivered" for e in t.events)


def test_different_set_up_times_or_missing_times_do_not_join_a_thread() -> None:
    c = [w(0, 2000, "low", payoff="waiting", waiting_since_ms=500), w(2000, 4000, "low", payoff="waiting", waiting_since_ms=3500),
         w(4000, 6000, "medium", payoff="waiting", waiting_since_ms=None)]
    threads = timeline(c).expectation_threads
    assert [th.set_up_ms for th in threads] == [500, 3500]
    assert timeline(c).failure_regions[0].cause_region is None, "no reported set-up time, no cause region"


# ----------------------------------------------------------------------------- information boundary


def _signals(duration: int, quiet_until: int) -> VideoSignals:
    rms = np.full(duration // 10, 1.0, dtype=np.float32)
    rms[: quiet_until // 10] = 0.1
    return VideoSignals(duration, 360, 640, [], [], rms)


class _Frames:
    def __init__(self) -> None:
        self.requested: list[int] = []

    def many(self, times: list[int], width: int) -> list[SampledFrame]:
        self.requested += times
        return [SampledFrame(t, b"jpeg", width, width * 2) for t in times]


def test_precision_payload_contains_nothing_after_its_boundary() -> None:
    words = [R.Word("setup", 3000, 3400), R.Word("almost", 4200, 4450), R.Word("reveal", 4400, 4600), R.Word("later", 5200, 5600)]
    frames = _Frames()
    p = R.build_viewer_payload(frames, _signals(8000, quiet_until=6000), words, 4000, 4500, scan="precision", fps=30, precision_frames=3)  # type: ignore[arg-type]
    assert all(f.timestamp_ms < 4500 for f in p.media.frames) and max(frames.requested) + R.frame_guard_ms(30) <= 4500
    assert "almost" in p.instruction and "reveal" not in p.instruction and "later" not in p.instruction, "a word still being spoken at the boundary is not shown"
    assert p.media.duration_ms == 4500 and p.media.transcript is None
    assert p.window.kind == "precision" and p.window.boundary_ms == 4500 and p.window.latest_word_end_ms == 4450 and p.window.latest_frame_ms is not None and p.window.latest_frame_ms < 4500
    assert "loudness: 1.00 of the typical speech level so far" in p.instruction, "the loudness reference comes from the audio heard so far, not the louder rest of the video"
    assert [f.timestamp_ms for f in p.media.frames if f.width == R.CONTEXT_WIDTH] == [500, 1500, 2500, 3500]


def test_coarse_sampling_is_unchanged_and_low_frame_rates_keep_the_guard() -> None:
    ctx, cur = R.viewer_frame_times(2000, 4000, fps=30)
    assert ctx == [500, 1500] and cur == R._times(2000, 4000, R.CURRENT_FPS)
    _, slow = R.viewer_frame_times(4000, 4500, fps=5, precision_frames=3)
    assert all(t + R.frame_guard_ms(5) <= 4500 for t in slow), "a 5 fps frame requested near the end would come back from after the boundary"


def test_boundary_violation_raises_instead_of_sending(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(R, "viewer_frame_times", lambda *a, **k: ([], [4600]))
    with pytest.raises(ValueError, match="information boundary"):
        R.build_viewer_payload(_Frames(), _signals(8000, 0), [], 4000, 4500, fps=30)  # type: ignore[arg-type]


# ----------------------------------------------------------------------------- run_audit with precision scans (I)


class Scripted:
    capability = ProviderCapability(name="offline", role="probe", model="scripted")

    def __init__(self, risks: dict[int, str], fail_precision: str | None = None) -> None:
        self.risks = risks  # coarse window end -> risk
        self.fail_precision = fail_precision
        self.calls: list[tuple[ProbeMedia, str, dict[str, Any]]] = []
        self._lock = threading.Lock()

    def judge_json(self, media: ProbeMedia, instruction: str, schema: dict[str, Any]) -> CompletionResult:
        with self._lock:
            self.calls.append((media, instruction, schema))
        if schema is R.WINDOW_SCHEMA:
            precision = sum(1 for f in media.frames if f.width == R.CURRENT_WIDTH) == 3  # coarse 2 s windows carry 6 frames
            if precision and self.fail_precision == "provider":
                raise ProviderError("scripted precision failure")
            if precision and self.fail_precision == "budget":
                raise BudgetExceeded("model-call limit of 9 reached")
            risk = self.risks.get(media.duration_ms, "medium" if media.duration_ms > 2500 else "low")
            data: dict[str, Any] = {"understanding": "u", "expectation": "e", "open_question": "", "reaction": "neutral" if risk != "low" else "engaged",
                                    "attention_risk": risk, "cause": f"c{media.duration_ms}", "payoff": "waiting", "waiting_since_s": 1.0}
        elif schema is R.DIAG_SCHEMA:
            data = {"findings": [{"start_s": 2.5, "end_s": 4.0, "weakness": "w", "severity": "medium", "uncertainty": "low", "evidence_times_s": [3.0], "observed": []}],
                    "strengths": [], "audience": {}, "overall_summary": "s"}
        else:
            data = {"happens": True, "start_s": 2.5, "end_s": 4.0, "confirmed_observations": [], "corrected_observations": [], "note": ""}
        return CompletionResult(data=copy.deepcopy(data), latency_ms=5, model="scripted", input_tokens=100, output_tokens=10)


def _media(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, duration: int = 4000) -> Path:
    video = tmp_path / "v.mp4"
    video.write_bytes(b"offline")
    monkeypatch.setattr(R, "inspect_media", lambda path: SimpleNamespace(duration_ms=duration, has_audio=True, width=360, height=640, fps=30.0))
    monkeypatch.setattr(R, "extract_signals", lambda *a: VideoSignals(duration, 360, 640, [], [], np.ones(duration // 10, dtype=np.float32)))
    monkeypatch.setattr(R, "transcribe", lambda path: Transcript(text="one two", has_speech=True, source="fixture", model="offline", segments=[
        TranscriptSegment(0, 3900, "one two", tokens=(TranscriptToken(100, 400, " one"), TranscriptToken(3600, 3900, " two")))]))
    monkeypatch.setattr(R, "extract_frame", lambda path, t, max_width: SampledFrame(t, b"jpeg", max_width, max_width * 2))
    return video


def test_rising_risk_runs_a_bounded_precision_scan_inside_the_audit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    video = _media(monkeypatch, tmp_path)
    provider = Scripted({2000: "low", 4000: "high"})
    rep = R.run_audit(video_id="vid", video_path=video, version_id="v0", provider=provider, data_dir=tmp_path / "data")
    assert rep.status == "complete"
    assert [(p.start_ms, p.end_ms) for p in rep.precision_reactions] == [(1500, 2000), (2000, 2500), (2500, 3000), (3000, 3500), (3500, 4000)]
    assert rep.model_calls == len(provider.calls) == 2 + 5 + 1 + 1
    assert rep.call_usage["precision"].calls == 5 and rep.call_usage["coarse"].input_tokens == 200 and rep.call_usage["diagnosis"].calls == 1
    closeup = rep.call_usage["closeup"]
    assert (closeup.calls, closeup.input_tokens, closeup.output_tokens, closeup.latency_ms) == (1, 100, 10, 5), "close-up token usage is recorded, not left at zero"
    assert rep.attention is not None and rep.attention.precision_scans[0].status == "complete"
    region = rep.attention.failure_regions[0]
    assert region.refined_by_precision and (region.onset.start_ms, region.onset.end_ms) == (2500, 3000)
    assert region.finding_ids == [rep.findings[0].id]
    for media, instruction, schema in provider.calls:
        if schema is R.WINDOW_SCHEMA:
            assert all(f.timestamp_ms < media.duration_ms for f in media.frames)
            assert ("two" in instruction) is (media.duration_ms >= 3900), "words appear only once fully spoken"
            assert "precision" not in instruction.lower() and "risk rose" not in instruction, "a precision reviewer is never told why it was asked"
    diag = next(i for _, i, s in provider.calls if s is R.DIAG_SCHEMA)
    assert "closer look 2.5-3.0s" in diag and "predicted attention problem" in diag
    assert {s.key for s in rep.workflow_stages} >= {"attention_transitions", "precision_scan", "precision_window", "attention_timeline"}
    assert len([s for s in rep.workflow_stages if s.key == "review_window"]) == 2
    assert rep.coverage.cold_viewer_prompt_version == A.COLD_VIEWER_PROMPT_VERSION
    assert [wi.kind for wi in rep.coverage.windows].count("precision") == 5
    assert "the whole-story diagnosis saw 8 frames spread across the video (about one every 0.5 s)" in rep.coverage.limitations[0]
    diag_window = next(wi for wi in rep.coverage.windows if wi.kind == "diagnostic")
    assert len(diag_window.frame_timestamps_ms) == 8, "the coverage record and the frames actually sent agree"
    stored = AuditReport.model_validate_json((tmp_path / "data" / "audit" / rep.id / "audit.json").read_text())
    assert stored.attention == rep.attention


def test_chronological_reactions_are_saved_before_the_diagnosis_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import json

    video = _media(monkeypatch, tmp_path)
    seen_at_diagnosis: list[dict[str, Any]] = []

    class Watching(Scripted):
        def judge_json(self, media: ProbeMedia, instruction: str, schema: dict[str, Any]) -> CompletionResult:
            if schema is R.DIAG_SCHEMA:
                files = list((tmp_path / "data" / "audit").glob("*/chronological_reactions.json"))
                seen_at_diagnosis.append(json.loads(files[0].read_text()) if files else {})
            return super().judge_json(media, instruction, schema)

    rep = R.run_audit(video_id="vid", video_path=video, version_id="v0", provider=Watching({2000: "low", 4000: "high"}), data_dir=tmp_path / "data")
    assert len(seen_at_diagnosis) == 1 and seen_at_diagnosis[0], "the reactions file exists when the diagnosis is requested"
    saved = seen_at_diagnosis[0]
    assert saved["audit_id"] == rep.id and saved["artifact_hash"] == rep.artifact_hash and saved["evaluator"]["cold_viewer_prompt_version"] == A.COLD_VIEWER_PROMPT_VERSION
    assert [(r["start_ms"], r["end_ms"], r["attention_risk"]) for r in saved["coarse_reactions"]] == [(r.start_ms, r.end_ms, r.attention_risk) for r in rep.window_reactions]
    assert len(saved["precision_reactions"]) == 5 and all(w["kind"] in ("prefix", "precision") for w in saved["inspected_windows"])
    assert "findings" not in json.dumps(saved["coarse_reactions"]), "nothing from the diagnosis is in the chronological record"


@pytest.mark.parametrize("failure", ["provider", "budget"])
def test_i_precision_failure_keeps_the_coarse_result_usable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str) -> None:
    video = _media(monkeypatch, tmp_path)
    rep = R.run_audit(video_id="vid", video_path=video, version_id="v0", provider=Scripted({2000: "low", 4000: "high"}, fail_precision=failure), data_dir=tmp_path / "data")
    assert rep.status == "complete" and rep.incomplete_reasons == []
    assert rep.attention is not None and rep.attention.precision_scans[0].status == "failed"
    assert all(p.error for p in rep.precision_reactions)
    region = rep.attention.failure_regions[0]
    assert not region.refined_by_precision and (region.onset.start_ms, region.onset.end_ms) == (2000, 4000)
    assert any("precision reviews were unavailable" in lim for lim in rep.coverage.limitations)
    assert rep.findings, "the diagnosis still runs on the coarse reading"


def test_precision_can_be_disabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    video = _media(monkeypatch, tmp_path)
    provider = Scripted({2000: "low", 4000: "high"})
    rep = R.run_audit(video_id="vid", video_path=video, version_id="v0", provider=provider, data_dir=tmp_path / "data", attention=A.AttentionConfig(precision_enabled=False))
    assert rep.precision_reactions == [] and rep.model_calls == 4 and rep.attention is not None and rep.attention.failure_regions


def test_old_audit_records_still_load() -> None:
    old = {"id": "audit_x", "video_id": "v", "version_id": "v0", "artifact_hash": "0" * 64, "artifact_path": "x.mp4", "duration_ms": 4000, "created_at": "t",
           "coverage": {"mode": "sampled_frames_plus_asr", "duration_ms": 4000, "windows": [{"kind": "prefix", "start_ms": 0, "end_ms": 2000, "frame_timestamps_ms": [166], "frame_width": 448}]},
           "window_reactions": [{"start_ms": 0, "end_ms": 2000, "attention_risk": "low", "reaction": "engaged"}]}
    rep = AuditReport.model_validate(old)
    assert rep.attention is None and rep.precision_reactions == [] and rep.window_reactions[0].payoff == "unknown"
    assert rep.coverage.cold_viewer_prompt_version == "cold-viewer-v1"


# ----------------------------------------------------------------------------- A/B comparison


def test_comparison_sees_the_repaired_region_and_a_new_regression_in_the_opening() -> None:
    before = timeline(coarse("low", "low", "high", "high", "low"))
    after = timeline(coarse("medium", "low", "low", "low", "low"))
    cmp = A.compare_attention_timelines(before, after, target=(4000, 8000))
    assert cmp.comparable and cmp.target_region_improved and cmp.target_peak_before == "high" and cmp.target_peak_after == "low"
    assert [(c.start_ms, c.before, c.after) for c in cmp.regression_changes] == [(0, "low", "medium")]
    assert cmp.global_direction == "mixed" and cmp.high_ms_before == 4000 and cmp.high_ms_after == 0


def test_comparison_flags_different_evaluators_as_not_comparable() -> None:
    before = timeline(coarse("low", "high"))
    other = before.model_copy(deep=True)
    other.evaluator.cold_viewer_prompt_version = "cold-viewer-v1"
    cmp = A.compare_attention_timelines(before, other)
    assert not cmp.comparable and any("cold_viewer_prompt_version" in n for n in cmp.comparability_notes)


def _report(tl: Any, duration: int) -> AuditReport:
    return AuditReport(id="a", video_id="v", version_id="v", artifact_hash="0" * 64, artifact_path="x", duration_ms=duration, created_at="t",
                       coverage=CoverageRecord(mode="sampled_frames_plus_asr", duration_ms=duration), attention=tl)


def test_attention_evidence_maps_through_the_edit_and_only_counts_meaningful_regressions() -> None:
    genome = make_genome("0" * 64, [BeatRole.HOOK, BeatRole.CONTEXT, BeatRole.PROOF, BeatRole.PAYOFF])
    plan = identity_plan(genome)
    trimmed = apply_ops(plan, [RemoveSegment(segment_id="beat_01")])  # the candidate drops 2.0-4.0s of the original
    original = _report(timeline(coarse("low", "high/losing_interest", "low", "low")), 8000)
    candidate = _report(timeline(coarse("low", "medium", "low")), 6000)  # candidate 2.0-4.0s shows original 4.0-6.0s
    finding = AuditFinding(id="f", version_id="v0", start_ms=2000, end_ms=4000)
    cmp, regressed, improved, notes = attention_evidence(original, candidate, finding, trimmed)
    assert cmp.target_region_in_candidate is None, "the target interval was removed"
    assert [(c.start_ms, c.original_start_ms, c.before, c.after) for c in cmp.regression_changes] == [(2000, 4000, "low", "medium")]
    assert regressed == [], "a rise to medium after the opening is a note, not a regression"
    assert any("is at medium risk" in n for n in notes)
    opening = _report(timeline(coarse("high", "low", "low")), 6000)
    _, regressed2, _, _ = attention_evidence(original, opening, finding, trimmed)
    assert regressed2 and regressed2[0].endswith("(opening)")


# ----------------------------------------------------------------------------- J: a diagnosed problem with no executable repair


def test_j_attention_failure_stays_diagnosed_when_no_edit_can_fix_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from tests.unit import test_director as TD

    script = TD.Script(findings_by_version={"v0": [TD.finding("f1", 2000, 6000)]}, choices=[])
    video, data = TD.install(monkeypatch, tmp_path, script)
    plain_audit = TD.D.run_audit

    def audit_with_attention(**kw: Any) -> AuditReport:
        rep = plain_audit(**kw)
        c = coarse("low", "medium/losing_interest", "high/losing_interest", "low")
        rep.window_reactions = c
        rep.attention = timeline(c)
        A.link_findings(rep.attention, rep.findings)
        return rep

    monkeypatch.setattr(TD.D, "run_audit", audit_with_attention)
    run = TD.D.run_director(TD.config(video, iteration_budget=2), TD.bundle(), data)
    assert run.iterations[0].decision == "stop" and "no runnable existing-video repair remains" in run.stop_reason
    assert run.final_version_id == "v0" and run.repair_run_ids == [], "no edit is forced when none fits"
    saved = AuditReport.model_validate_json((data / "audit" / run.audit_ids[0] / "audit.json").read_text())
    assert saved.attention is not None and saved.attention.failure_regions[0].finding_ids == ["f1"]
    assert saved.findings[0].repair is not None and saved.findings[0].repair.runnable is False


def test_comparison_clip_never_samples_past_a_short_mapped_interval_or_the_file_end() -> None:
    from directorloop.audit.revise import clip_times

    # found on real footage: a finding at 12.8-14.0s mapped to a 90 ms sliver at the end of a 12.9 s candidate
    assert clip_times(12800, 12890, 3, 12900) == [12840]
    assert all(12800 <= t < 12890 for t in clip_times(12800, 12890, 3, None))
    times = clip_times(9200, 13600, 3, 13900)
    assert times and max(times) < 13600 and len(times) <= 10
