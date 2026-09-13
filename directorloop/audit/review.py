"""Cold-audience audit of an actual rendered video.

1. Unprimed prefix review (coarse attention scan): one independent call per chronological window. The reviewer receives
   only what an unfamiliar viewer has seen and heard up to the end of that window (earlier moments as small frames, the
   latest window as larger frames, the words finished by then, and loudness measured against the video so far). No
   creator notes, no labels, no later reviews.
2. Precision scan: where predicted attention risk rises between coarse windows, the same prefix review runs again on
   shorter windows around that moment, within fixed limits (audit/attention.py). The reviewer is not told why.
3. Attention timeline: deterministic events, failure regions and expectation threads built from those reviews.
4. Diagnostic pass: the whole story (frames across the video, timed transcript, mechanical signals) plus the window
   reactions and the derived attention changes produce findings, preserved strengths and audience predictions,
   observations separate from interpretation.
5. Close-up verification: dense frames around each suspected problem confirm or correct what happens and when.
Coverage records exactly what was sent, including each review's information boundary.
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import weave

from ..config import get_settings
from ..creative.signals import RMS_HOP_MS, VideoSignals, extract_signals, silence_gaps, static_spans
from ..domain.ids import new_id, sha256_file, utc_now_iso
from ..jobs.worker import safe_failure, safe_limit_reason
from ..media.frames import SampledFrame, extract_frame
from ..media.probe import inspect_media
from ..media.transcribe import Transcript, transcribe
from ..observability.weave_ops import current_call_ref, traced
from ..observability.workflow import workflow_session, workflow_stage
from ..providers.base import MediaProbeProvider, ProbeMedia, ProviderError
from .attention import (
    COLD_VIEWER_PROMPT_VERSION,
    AttentionConfig,
    PlannedScan,
    attention_brief,
    build_attention_timeline,
    fmt_range,
    link_findings,
    plan_precision,
)
from .models import (
    ISSUE_TYPES,
    OBJECTIVES,
    AttentionTimeline,
    AudienceResponses,
    AudienceVerdict,
    AuditFinding,
    AuditReport,
    CallUsage,
    CoverageRecord,
    EvaluatorRecord,
    EvidenceFrame,
    InspectedWindow,
    Observation,
    Strength,
    WindowReaction,
)

WINDOW_MS = 2000
CONTEXT_FPS = 1.0
CURRENT_FPS = 3.0
CONTEXT_WIDTH = 256
CURRENT_WIDTH = 448
DIAG_FPS = 2.0
DIAG_WIDTH = 384
CLOSEUP_FPS = 5.0
CLOSEUP_WIDTH = 512
DIAG_MAX_FRAMES = 40
CLOSEUP_MAX_FRAMES = 16
VIEWER_WORD_LIMIT = 60
UNATTEMPTED_WINDOW = "not attempted after terminal provider failure: "


class _AuditProviderGate:
    """Stop new logical requests after a hard quota/auth failure; let in-flight calls settle."""

    def __init__(self, inner: MediaProbeProvider) -> None:
        self.inner = inner
        self._lock = threading.Lock()
        self.reason: str | None = None
        self.started = self.succeeded = self.failed = self.active = 0
        self.in_flight_at_stop = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def judge_json(self, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            if self.reason:
                raise ProviderError(UNATTEMPTED_WINDOW + self.reason)
            self.started += 1
            self.active += 1
        try:
            result = self.inner.judge_json(*args, **kwargs)
        except ProviderError as exc:
            reason = safe_failure(exc)
            # An ordinary transient HTTP429 or timeout is not a hard quota failure.
            terminal = any(category in reason for category in (
                "provider credits exhausted", "provider authentication or permission failed",
                "spending reservation limit reached; no request sent", "spending guard rejected request; no request sent",
            ))
            with self._lock:
                self.failed += 1
                if terminal and self.reason is None:
                    self.reason = reason
                    self.in_flight_at_stop = self.active - 1
            if terminal:
                raise ProviderError(reason) from exc
            raise
        else:
            with self._lock:
                self.succeeded += 1
            return result
        finally:
            with self._lock:
                self.active -= 1

    def stop_receipt(self) -> dict[str, Any] | None:
        with self._lock:
            if self.reason is None:
                return None
            return {"reason": self.reason, "logical_calls_started": self.started,
                    "logical_calls_succeeded": self.succeeded, "logical_calls_failed": self.failed,
                    "in_flight_when_stop_detected": self.in_flight_at_stop, "in_flight_remaining": self.active,
                    "diagnosis_attempted": False, "failed_request_billed_usage": "unknown"}


def _unattempted_window(start_ms: int, end_ms: int, scan: str, reason: str) -> tuple[WindowReaction, InspectedWindow]:
    reaction = WindowReaction(start_ms=start_ms, end_ms=end_ms, error=UNATTEMPTED_WINDOW + reason, scan=scan)
    window = InspectedWindow(kind="precision" if scan == "precision" else "prefix", start_ms=start_ms, end_ms=end_ms,
                             frame_timestamps_ms=[], frame_width=CURRENT_WIDTH, boundary_ms=end_ms)
    return reaction, window


def _review_windows(provider: MediaProbeProvider, frames: FrameCache, signals: VideoSignals, words: list[Word],
                    windows: list[tuple[int, int]], *, scan: str = "coarse", fps: float | None = None,
                    precision_frames: int | None = None) -> list[tuple[WindowReaction, InspectedWindow]]:
    """Rolling fan-out preserves chronological output without scheduling every window in advance."""
    if not windows:
        return []
    results: dict[int, tuple[WindowReaction, InspectedWindow]] = {}
    next_index = 0
    with weave.ThreadPoolExecutor(max_workers=min(6, len(windows))) as executor:
        pending = {}
        while next_index < len(windows) or pending:
            while next_index < len(windows) and len(pending) < 6 and not (isinstance(provider, _AuditProviderGate) and provider.reason):
                index = next_index
                start_ms, end_ms = windows[index]
                pending[executor.submit(_review_window_step, provider, frames, signals, words, start_ms, end_ms,
                                        scan=scan, fps=fps, precision_frames=precision_frames)] = index
                next_index += 1
            if not pending:
                break
            completed, _ = cf.wait(pending, return_when=cf.FIRST_COMPLETED)
            for future in completed:
                results[pending.pop(future)] = future.result()
    for index, (start_ms, end_ms) in enumerate(windows):
        if index not in results:
            results[index] = _unattempted_window(start_ms, end_ms, scan, provider.reason)
    return [results[i] for i in range(len(windows))]


@dataclass
class Word:
    text: str
    start_ms: int
    end_ms: int


def words_from_transcript(t: Transcript) -> list[Word]:
    words: list[Word] = []
    for seg in t.segments:
        if not seg.tokens:
            parts = seg.text.split()
            if not parts:
                continue
            step = max(1, (seg.end_ms - seg.start_ms) // len(parts))
            for i, p in enumerate(parts):
                words.append(Word(p, seg.start_ms + i * step, seg.start_ms + (i + 1) * step))
            continue
        for tok in seg.tokens:
            raw = tok.text
            if not raw.strip():
                continue
            if raw.startswith(" ") or not words or words[-1].end_ms < seg.start_ms:
                words.append(Word(raw.strip(), tok.start_ms, tok.end_ms))
            else:
                words[-1].text += raw.strip()
                words[-1].end_ms = tok.end_ms
    return words


def plan_windows(duration_ms: int, window_ms: int = WINDOW_MS) -> list[tuple[int, int]]:
    wins: list[tuple[int, int]] = []
    t = 0
    while t < duration_ms:
        e = min(duration_ms, t + window_ms)
        if duration_ms - e < window_ms * 0.4:
            e = duration_ms
        wins.append((t, e))
        t = e
    return wins


class FrameCache:
    """Frames extracted once per (timestamp, width) for a file; evidence frames are written from here."""

    def __init__(self, path: Path, duration_ms: int) -> None:
        self.path = path
        self.duration_ms = duration_ms
        self._frames: dict[tuple[int, int], SampledFrame] = {}

    def get(self, t_ms: int, width: int) -> SampledFrame:
        t = int(min(max(0, t_ms), max(0, self.duration_ms - 60)))
        key = (t, width)
        if key not in self._frames:
            self._frames[key] = extract_frame(self.path, t, max_width=width)
        return self._frames[key]

    @traced(
        "directorloop.sample_frames", kind="tool",
        display=lambda i: f"Sample {len(i.get('times', []))} frames at {i.get('width', '?')}px",
        summarize=lambda frames: {"frame_count": len(frames), "timestamps_ms": [f.timestamp_ms for f in frames]},
    )
    def many(self, times: list[int], width: int) -> list[SampledFrame]:
        with cf.ThreadPoolExecutor(max_workers=6) as ex:
            return list(ex.map(lambda t: self.get(t, width), times))


def _times(start_ms: int, end_ms: int, fps: float) -> list[int]:
    step = 1000.0 / fps
    out, t = [], start_ms + step / 2
    while t < end_ms:
        out.append(int(t))
        t += step
    return out or [int((start_ms + end_ms) / 2)]


def spread(times: list[int], limit: int) -> list[int]:
    """At most `limit` of `times`, spread evenly from the first to the last, so a frame budget covers the whole interval."""
    if len(times) <= limit:
        return times
    if limit <= 1:
        return times[:1]
    return sorted({times[round(i * (len(times) - 1) / (limit - 1))] for i in range(limit)})


def diagnosis_frame_times(duration_ms: int) -> list[int]:
    return spread(_times(0, duration_ms, DIAG_FPS), DIAG_MAX_FRAMES)


def frame_guard_ms(fps: float | None) -> int:
    """A frame requested at t can come back as the first frame whose timestamp is at or after t, up to one frame interval
    later. Frames for a window are requested at least this long before its end, so no returned frame starts after it."""
    return int(math.ceil(1000.0 / fps)) + 1 if fps and fps > 0 else 84


def viewer_frame_times(start_ms: int, end_ms: int, *, fps: float | None, precision_frames: int | None = None) -> tuple[list[int], list[int]]:
    """(earlier-moment frame times, moment frame times) for a prefix review of [start, end]. Coarse windows keep 3 frames
    per second; a precision window gets `precision_frames` frames spread over its start, middle and end."""
    guard = frame_guard_ms(fps)
    ctx = _times(0, start_ms, CONTEXT_FPS) if start_ms > 0 else []
    if precision_frames:
        span = end_ms - start_ms
        cur = [int(start_ms + (k + 0.5) * span / precision_frames) for k in range(precision_frames)]
    else:
        cur = _times(start_ms, end_ms, CURRENT_FPS)
    cur = sorted({max(start_ms, min(t, end_ms - guard)) for t in cur})
    return ctx, cur


def words_heard_by(words: list[Word], end_ms: int) -> list[Word]:
    """Words fully spoken by end_ms. A word still being spoken at the boundary is left out: its text would reveal audio the
    viewer has not heard yet. The resolution is the ASR token timing (whisper.cpp token offsets), not phonemes."""
    return [w for w in words if w.end_ms <= end_ms]


def _audio_lines(signals: VideoSignals, words: list[Word], start_ms: int, end_ms: int) -> list[str]:
    spoken = [w for w in words_heard_by(words, end_ms) if w.end_ms > start_ms]
    lines = [f"speech: {'yes, ' + str(len(spoken)) + ' words' if spoken else 'no words transcribed'}"]
    if len(signals.rms):
        a, b = start_ms // RMS_HOP_MS, max(start_ms // RMS_HOP_MS + 1, end_ms // RMS_HOP_MS)
        seg = signals.rms[a:b]
        if len(seg):
            # the reference is the audio heard so far, never the rest of the video
            prefix = signals.rms[: max(b, 1)]
            ref = float(np.percentile(prefix, 90)) or 1e-6
            lines.append(f"loudness: {float(seg.mean()) / ref:.2f} of the typical speech level so far")
        gaps = silence_gaps(signals.rms, start_ms, end_ms, min_gap_ms=300)
        if gaps:
            lines.append("near-silent stretches: " + ", ".join(f"{g0 / 1000:.1f}-{g1 / 1000:.1f}s" for g0, g1 in gaps))
    return lines


WINDOW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "understanding": {"type": "string"},
        "expectation": {"type": "string"},
        "open_question": {"type": "string"},
        "reaction": {"type": "string", "enum": ["engaged", "neutral", "losing_interest", "confused"]},
        "attention_risk": {"type": "string", "enum": ["low", "medium", "high"]},
        "cause": {"type": "string"},
        "payoff": {"type": "string", "enum": ["none", "waiting", "delivered"]},
        "waiting_since_s": {"type": ["number", "null"]},
    },
    "required": ["understanding", "expectation", "open_question", "reaction", "attention_risk", "cause", "payoff", "waiting_since_s"],
}

WINDOW_INSTRUCTION = """You are scrolling a short-video feed on your phone and this video just started. You know nothing about it.
You have now watched from 0.0s to {end:.1f}s. Frames before {start:.1f}s are small snapshots of what you already saw (about one per second).
Frames from {start:.1f}s to {end:.1f}s are the moment you just watched.
Words you have heard so far, with times in seconds (words in the moment you just watched are marked >>):
{words}
Sound measurements for the moment you just watched (measured by software; you did not hear the audio):
{audio}
Answer as that viewer, only from what was shown or said up to {end:.1f}s:
- understanding: what you think this video is about and what you understand so far
- expectation: what you expect or hope happens next
- open_question: the question on your mind right now (empty if none)
- reaction to the moment you just watched: engaged, neutral, losing_interest or confused
- attention_risk: how likely you are to scroll away right after this moment (low, medium, high)
- cause: the specific thing in the moment you just watched (what you saw, read on screen, or heard) that caused this reaction
- payoff: has the video set up something you are still waiting to see or learn? "waiting" if it has not been delivered yet, "delivered" if the moment you just watched delivered it, "none" if nothing is set up
- waiting_since_s: if waiting, the time in seconds when the video first set up what you are waiting for; otherwise null
Be blunt, the way a real viewer talks to a friend: if this moment is boring, confusing or pointless, say so plainly and name exactly what caused it. Do not be polite, and do not invent problems that are not there."""


def _words_block(words: list[Word], start_ms: int, end_ms: int) -> str:
    heard = words_heard_by(words, end_ms)
    if not heard:
        return "(no words yet)"
    lines = []
    for w in heard:
        mark = ">>" if w.start_ms >= start_ms else "  "
        lines.append(f"{mark} {w.start_ms / 1000:.1f}s {w.text}")
    return "\n".join(lines[-VIEWER_WORD_LIMIT:])


@dataclass
class ViewerPayload:
    """Exactly what one prefix or precision review sends, and the information boundary it respects."""

    media: ProbeMedia
    instruction: str
    window: InspectedWindow


def build_viewer_payload(frames: FrameCache, signals: VideoSignals, words: list[Word], start_ms: int, end_ms: int, *, scan: str = "coarse",
                         fps: float | None = None, precision_frames: int | None = None) -> ViewerPayload:
    """Frames, words and sound measurements a cold viewer at end_ms may have: nothing from later in the video, no earlier
    reviews, no labels. Raises if any frame or word would cross the boundary, so a sampling bug cannot leak the future."""
    ctx_times, cur_times = viewer_frame_times(start_ms, end_ms, fps=fps, precision_frames=precision_frames)
    ctx = frames.many(ctx_times, CONTEXT_WIDTH)
    cur = frames.many(cur_times, CURRENT_WIDTH)
    heard = words_heard_by(words, end_ms)[-VIEWER_WORD_LIMIT:]
    sent = ctx + cur
    guard = frame_guard_ms(fps)
    if any(f.timestamp_ms + guard > end_ms and f.timestamp_ms > start_ms for f in cur) or any(f.timestamp_ms >= end_ms for f in sent) or any(w.end_ms > end_ms for w in heard):
        raise ValueError(f"information boundary violated for the review ending at {end_ms} ms")
    media = ProbeMedia(kind="frames", duration_ms=end_ms, frames=sent, transcript=None)
    instruction = WINDOW_INSTRUCTION.format(start=start_ms / 1000, end=end_ms / 1000, words=_words_block(words, start_ms, end_ms), audio="; ".join(_audio_lines(signals, words, start_ms, end_ms)))
    window = InspectedWindow(kind="precision" if scan == "precision" else "prefix", start_ms=start_ms, end_ms=end_ms, frame_timestamps_ms=[f.timestamp_ms for f in cur],
                             frame_width=CURRENT_WIDTH, context_frames=len(ctx), context_width=CONTEXT_WIDTH,
                             context_frame_timestamps_ms=[f.timestamp_ms for f in ctx], transcript_words=len(heard),
                             latest_frame_ms=max((f.timestamp_ms for f in sent), default=None), latest_word_end_ms=max((w.end_ms for w in heard), default=None), boundary_ms=end_ms)
    return ViewerPayload(media=media, instruction=instruction, window=window)


@traced(
    "cold_review_window", kind="llm",
    display=lambda i: (f"Precision review {i['start_ms'] / 1000:.1f}-{i['end_ms'] / 1000:.1f}s (prefix 0.0-{i['end_ms'] / 1000:.1f}s)" if i.get("scan") == "precision"
                       else f"Evaluate prefix 0.0-{i['end_ms'] / 1000:.1f}s"),
    summarize=lambda result: {"reaction": result[0], "inspected_window": result[1]},
)
def review_window(provider: MediaProbeProvider, frames: FrameCache, signals: VideoSignals, words: list[Word], start_ms: int, end_ms: int, scan: str = "coarse",
                  fps: float | None = None, precision_frames: int | None = None) -> tuple[WindowReaction, InspectedWindow]:
    payload = build_viewer_payload(frames, signals, words, start_ms, end_ms, scan=scan, fps=fps, precision_frames=precision_frames)
    scan_kind = "precision" if scan == "precision" else "coarse"
    t0 = time.monotonic()
    try:
        res = provider.judge_json(payload.media, payload.instruction, WINDOW_SCHEMA)
    except ProviderError as exc:
        if str(exc).startswith(UNATTEMPTED_WINDOW):
            return _unattempted_window(start_ms, end_ms, scan_kind, str(exc).removeprefix(UNATTEMPTED_WINDOW))
        return WindowReaction(start_ms=start_ms, end_ms=end_ms, error=safe_failure(exc), latency_ms=int((time.monotonic() - t0) * 1000), scan=scan_kind), payload.window
    d = res.data
    payoff = d.get("payoff") if d.get("payoff") in ("none", "waiting", "delivered") else "unknown"
    since = d.get("waiting_since_s")
    waiting_since_ms = int(float(since) * 1000) if payoff == "waiting" and isinstance(since, int | float) and 0 <= float(since) * 1000 <= end_ms else None
    return WindowReaction(
        start_ms=start_ms, end_ms=end_ms, understanding=str(d.get("understanding", ""))[:400], expectation=str(d.get("expectation", ""))[:300],
        open_question=str(d.get("open_question", ""))[:240], reaction=d.get("reaction", "unknown") if d.get("reaction") in ("engaged", "neutral", "losing_interest", "confused") else "unknown",
        attention_risk=d.get("attention_risk", "unknown") if d.get("attention_risk") in ("low", "medium", "high") else "unknown", cause=str(d.get("cause", ""))[:300],
        latency_ms=res.latency_ms, scan=scan_kind, payoff=payoff, waiting_since_ms=waiting_since_ms,  # type: ignore[arg-type]
        input_tokens=res.input_tokens, output_tokens=res.output_tokens,
    ), payload.window


def _review_window_step(provider: MediaProbeProvider, frames: FrameCache, signals: VideoSignals, words: list[Word], start_ms: int, end_ms: int,
                        scan: str = "coarse", fps: float | None = None, precision_frames: int | None = None) -> tuple[WindowReaction, InspectedWindow]:
    """Record a window in the session graph without adding anything to the evaluator's inputs."""
    precision = scan == "precision"
    if isinstance(provider, _AuditProviderGate) and provider.reason:
        return _unattempted_window(start_ms, end_ms, scan, provider.reason)
    with workflow_stage(
        "precision_window" if precision else "review_window",
        f"Precision review {start_ms / 1000:.1f}-{end_ms / 1000:.1f}s" if precision else f"Review {start_ms / 1000:.1f}-{end_ms / 1000:.1f}s", kind="agent",
        inputs={"start_ms": start_ms, "end_ms": end_ms, "prefix_start_ms": 0, "scan": "precision" if precision else "coarse"},
    ) as stage:
        try:
            reaction, window = review_window(provider, frames, signals, words, start_ms, end_ms, scan=scan, fps=fps, precision_frames=precision_frames)
        except RuntimeError as exc:
            # A spent run budget or deadline ends a precision review; the coarse reading it would refine stays in place.
            if not precision or type(exc).__name__ != "BudgetExceeded":
                raise
            reaction = WindowReaction(start_ms=start_ms, end_ms=end_ms, error=f"not reviewed: {safe_limit_reason(exc)}", scan="precision")
            window = InspectedWindow(kind="precision", start_ms=start_ms, end_ms=end_ms, frame_timestamps_ms=[], frame_width=CURRENT_WIDTH, boundary_ms=end_ms)
        if reaction.error:
            stage.status = "skipped" if reaction.error.startswith(UNATTEMPTED_WINDOW) else "incomplete"
        stage.update(
            window_start_ms=start_ms, window_end_ms=end_ms, scan=reaction.scan,
            reaction=reaction.reaction, attention_risk=reaction.attention_risk, predicted_attention_risk=reaction.attention_risk,
            understanding=reaction.understanding, expectation=reaction.expectation, open_question=reaction.open_question, cause=reaction.cause,
            payoff=reaction.payoff, waiting_since_ms=reaction.waiting_since_ms, review_available=reaction.error is None,
            model_call_attempted=not bool(reaction.error and reaction.error.startswith(UNATTEMPTED_WINDOW)),
            evidence_label=reaction.label, current_frames=len(window.frame_timestamps_ms),
            context_frames=window.context_frames, transcript_words=window.transcript_words,
            latest_frame_ms=window.latest_frame_ms, latest_word_end_ms=window.latest_word_end_ms, information_boundary_ms=window.boundary_ms,
        )
        return reaction, window


def precision_scan(provider: MediaProbeProvider, frames: FrameCache, signals: VideoSignals, words: list[Word], plans: list[PlannedScan], cfg: AttentionConfig,
                   fps: float | None) -> tuple[list[WindowReaction], list[InspectedWindow]]:
    """Run the planned precision windows. Each is an ordinary prefix review on a shorter window; failures are recorded on the
    scan and leave the coarse reading in place."""
    reactions: list[WindowReaction] = []
    inspected: list[InspectedWindow] = []
    for plan in plans:
        scan = plan.scan
        if scan.status == "skipped" or not plan.windows:
            continue
        with workflow_stage(
            "precision_scan", f"Precision scan {fmt_range(scan.region.start_ms, scan.region.end_ms)}", kind="agent",
            inputs={"trigger": scan.trigger, "coarse_region_ms": [scan.trigger_window.start_ms, scan.trigger_window.end_ms],
                    "region_ms": [scan.region.start_ms, scan.region.end_ms], "precision_resolution_ms": cfg.precision_window_ms, "windows": len(plan.windows)},
        ) as stage:
            results = _review_windows(provider, frames, signals, words, plan.windows, scan="precision", fps=fps,
                                      precision_frames=cfg.precision_frames)
            got = [r for r, _ in results]
            errors = [r.error for r in got if r.error]
            scan.windows_reviewed = len(got) - len(errors)
            scan.errors = [e for e in errors if e][:5]
            scan.status = "complete" if not errors else ("failed" if len(errors) == len(got) else "partial")
            if scan.status != "complete":
                stage.status = "incomplete"
            skipped = sum(bool(r.error and r.error.startswith(UNATTEMPTED_WINDOW)) for r in got)
            if skipped == len(got):
                scan.status = "skipped"
                scan.skipped_reason = provider.reason
                stage.status = "skipped"
            stage.update(trigger=scan.trigger, windows_reviewed=scan.windows_reviewed, windows_failed=len(errors) - skipped,
                         windows_skipped=skipped, status=scan.status,
                         readings=[f"{fmt_range(r.start_ms, r.end_ms)} {r.attention_risk}/{r.reaction}" for r in got],
                         evidence_label="Model judgments from prefix reviews of shorter windows, not measured audience retention")
            reactions.extend(got)
            inspected.extend(w for _, w in results)
    return reactions, inspected


VERDICT = {"type": "object", "properties": {"verdict": {"type": "string", "enum": ["YES", "MAYBE", "NO", "INSUFFICIENT_EVIDENCE"]}, "reason": {"type": "string"}}, "required": ["verdict", "reason"]}
REPAIR_KINDS = ["trim_or_tighten", "remove_segment", "reorder", "reframe_or_zoom", "caption_or_text", "pause_adjust", "source_animation_or_timing",
                "source_camera_or_composition", "new_or_replacement_shot", "narration_rewrite", "other"]

DIAG_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "findings": {"type": "array", "items": {"type": "object", "properties": {
            "start_s": {"type": "number"}, "end_s": {"type": "number"},
            "observed": {"type": "array", "items": {"type": "object", "properties": {"kind": {"type": "string", "enum": ["visual", "audio", "caption", "mechanical"]}, "text": {"type": "string"}, "t_s": {"type": ["number", "null"]}}, "required": ["kind", "text", "t_s"]}},
            "viewer_understanding": {"type": "string"}, "predicted_reaction": {"type": "string"}, "weakness": {"type": "string"},
            "issue_type": {"type": "string", "enum": ISSUE_TYPES}, "alternatives": {"type": "array", "items": {"type": "string"}},
            "objective": {"type": "string", "enum": OBJECTIVES}, "severity": {"type": "string", "enum": ["low", "medium", "high"]},
            "uncertainty": {"type": "string", "enum": ["low", "medium", "high"]}, "proposed_repair": {"type": "string"},
            "keep_unchanged": {"type": "array", "items": {"type": "string"}}, "repair_kind": {"type": "string", "enum": REPAIR_KINDS},
            "evidence_times_s": {"type": "array", "items": {"type": "number"}},
        }, "required": ["start_s", "end_s", "observed", "viewer_understanding", "predicted_reaction", "weakness", "issue_type", "alternatives", "objective", "severity", "uncertainty", "proposed_repair", "keep_unchanged", "repair_kind", "evidence_times_s"]}},
        "strengths": {"type": "array", "items": {"type": "object", "properties": {"start_s": {"type": "number"}, "end_s": {"type": "number"}, "what": {"type": "string"}, "why": {"type": "string"}, "evidence_times_s": {"type": "array", "items": {"type": "number"}}}, "required": ["start_s", "end_s", "what", "why", "evidence_times_s"]}},
        "audience": {"type": "object", "properties": {
            "stop": VERDICT, "continue_watching": {**VERDICT, "properties": {**VERDICT["properties"], "weakens_at_s": {"type": ["number", "null"]}}, "required": ["verdict", "reason", "weakens_at_s"]},
            "finish": VERDICT, "like": VERDICT, "send": {**VERDICT, "properties": {**VERDICT["properties"], "to_whom": {"type": ["string", "null"]}}, "required": ["verdict", "reason", "to_whom"]},
            "comment": VERDICT, "save_or_replay": VERDICT, "visit_creator": VERDICT,
        }, "required": ["stop", "continue_watching", "finish", "like", "send", "comment", "save_or_replay", "visit_creator"]},
        "overall_summary": {"type": "string"},
    },
    "required": ["findings", "strengths", "audience", "overall_summary"],
}

DIAG_INSTRUCTION = """You are auditing a short vertical video as an unfamiliar audience member would experience it, then diagnosing it like a careful editor.
You receive frames across the whole video (timestamps shown), the words spoken with times, software measurements of cuts, still stretches and quiet stretches, and moment-by-moment reactions that an unprimed viewer model gave while watching chronologically (those reactions are model predictions, not real viewers).
You did NOT hear the audio: music, sound effects and voice tone are unknown to you beyond the transcript and loudness measurements.

Words with times (seconds):
{words}

Measurements:
{measurements}

Moment-by-moment unprimed reactions:
{reactions}

Closer looks and predicted attention changes, derived from those reactions by software (still model predictions, not real viewers):
{attention}

Produce:
1. findings: only moments with real evidence of a weakness in attention, understanding, emotional payoff or motivation to engage. Zero findings is allowed. Do not invent problems in every interval; stillness, silence, slow pacing or a delayed answer can be deliberate and good. For each finding give the exact interval, the directly OBSERVED facts (what is visible, what caption text is on screen, what is said, what the measurements show; each with its time) kept separate from interpretation, what the viewer likely understands or expects at that moment, the predicted reaction (a model judgment), the suspected weakness, other plausible explanations, the affected objective, severity and uncertainty, a specific repair (never "make it more engaging"), what must stay unchanged, the kind of repair it needs, and the times of the most representative frames.
2. strengths: sections that work and must be preserved, with why.
3. audience predictions (YES, MAYBE, NO or INSUFFICIENT_EVIDENCE with a short reason): would the opening make an unfamiliar viewer stop; would they keep watching and where interest may weaken; would they finish; is there a reason to like; would they send it and to whom; a natural reason to comment; value in saving or replaying; a reason to visit the creator. Not every video needs every action.
4. overall_summary: two or three plain sentences.
Use only evidence present in these inputs. Keep timestamps as precise as the frames allow; do not claim precision you cannot see.
Write like a blunt, experienced editor giving notes to a colleague: state each weakness plainly, say exactly what fails and what it costs the viewer, with no softening, hedging filler or generic praise. Blunt is not harsh for effect: every criticism needs evidence from these inputs, and zero findings is still the right answer when nothing fails."""


@traced(
    "diagnose_audit", kind="llm", display="Diagnose the whole story",
    summarize=lambda result: {"diagnosis": result[0], "inspected_window": result[1]},
)
def diagnostic_pass(provider: MediaProbeProvider, frames: FrameCache, signals: VideoSignals, words: list[Word], reactions: list[WindowReaction], duration_ms: int,
                    attention_lines: str = "(no attention analysis available)") -> tuple[dict[str, Any], InspectedWindow, CallUsage]:
    times = diagnosis_frame_times(duration_ms)
    media = ProbeMedia(kind="frames", duration_ms=duration_ms, frames=frames.many(times, DIAG_WIDTH), transcript=None)
    spans = static_spans(signals.cuts_ms, signals.motion, duration_ms)
    gaps = silence_gaps(signals.rms, 0, duration_ms, min_gap_ms=300) if len(signals.rms) else []
    measurements = [
        f"duration {duration_ms / 1000:.1f}s",
        "shot cuts at: " + (", ".join(f"{c / 1000:.1f}s" for c in signals.cuts_ms) or "none detected"),
        "still stretches (no cut, little motion): " + (", ".join(f"{a / 1000:.1f}-{b / 1000:.1f}s" for a, b in spans[:5]) or "none"),
        "quiet stretches: " + (", ".join(f"{a / 1000:.1f}-{b / 1000:.1f}s" for a, b in gaps[:8]) or "none"),
    ]
    reaction_lines = [f"{r.start_ms / 1000:.1f}-{r.end_ms / 1000:.1f}s: {r.reaction}, attention risk {r.attention_risk}; understands: {r.understanding}; expects: {r.expectation}; cause: {r.cause}" for r in reactions if not r.error]
    instruction = DIAG_INSTRUCTION.format(words="\n".join(f"{w.start_ms / 1000:.1f}s {w.text}" for w in words) or "(no speech)", measurements="\n".join(measurements),
                                          reactions="\n".join(reaction_lines) or "(none available)", attention=attention_lines)
    res = provider.judge_json(media, instruction, DIAG_SCHEMA)
    usage = CallUsage(calls=1, input_tokens=res.input_tokens or 0, output_tokens=res.output_tokens or 0, latency_ms=res.latency_ms)
    return res.data, InspectedWindow(kind="diagnostic", start_ms=0, end_ms=duration_ms, frame_timestamps_ms=[f.timestamp_ms for f in media.frames],
                                    frame_width=DIAG_WIDTH, transcript_words=len(words)), usage


CLOSEUP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "happens": {"type": "boolean"},
        "start_s": {"type": ["number", "null"]}, "end_s": {"type": ["number", "null"]},
        "confirmed_observations": {"type": "array", "items": {"type": "string"}},
        "corrected_observations": {"type": "array", "items": {"type": "string"}},
        "note": {"type": "string"},
    },
    "required": ["happens", "start_s", "end_s", "confirmed_observations", "corrected_observations", "note"],
}


@traced(
    "closeup_verify", kind="llm",
    display=lambda i: f"Check observations at {float(i['finding']['start_s']):.1f}-{float(i['finding']['end_s']):.1f}s",
    summarize=lambda result: {"verification": result[0], "inspected_window": result[1], "usage": result[2]},
)
def closeup_verify(provider: MediaProbeProvider, frames: FrameCache, words: list[Word], finding: dict[str, Any],
                   duration_ms: int) -> tuple[dict[str, Any] | None, InspectedWindow, CallUsage]:
    s = max(0, int((float(finding["start_s"]) - 0.4) * 1000))
    e = min(duration_ms, int((float(finding["end_s"]) + 0.4) * 1000))
    if isinstance(provider, _AuditProviderGate) and provider.reason:
        return None, InspectedWindow(kind="closeup", start_ms=s, end_ms=e, frame_timestamps_ms=[], frame_width=CLOSEUP_WIDTH), CallUsage()
    times = spread(_times(s, e, CLOSEUP_FPS), CLOSEUP_MAX_FRAMES)
    media = ProbeMedia(kind="frames", duration_ms=duration_ms, frames=frames.many(times, CLOSEUP_WIDTH), transcript=None)
    spoken = [w for w in words if w.start_ms < e and w.end_ms > s]
    said = " ".join(f"{w.start_ms / 1000:.1f}s {w.text}" for w in spoken) or "(no words)"
    obs = "\n".join(f"- [{o.get('kind')}] {o.get('text')} (t={o.get('t_s')})" for o in finding.get("observed", []))
    instruction = (
        f"Closer inspection of {s / 1000:.1f}-{e / 1000:.1f}s. Words said in this span: {said}\n"
        f"A reviewer described this moment as: {finding.get('weakness')}\nTheir observations:\n{obs}\n"
        "Using only these frames and words, check each observation. Does the described moment actually happen here? "
        "Give the precise start and end seconds where it happens (null if it does not), list which observations are confirmed, "
        "and correct any that are wrong."
    )
    window = InspectedWindow(kind="closeup", start_ms=s, end_ms=e, frame_timestamps_ms=[f.timestamp_ms for f in media.frames],
                             frame_width=CLOSEUP_WIDTH, transcript_words=len(spoken))
    try:
        res = provider.judge_json(media, instruction, CLOSEUP_SCHEMA)
    except ProviderError as exc:
        if str(exc).startswith(UNATTEMPTED_WINDOW):
            return None, window.model_copy(update={"frame_timestamps_ms": [], "transcript_words": 0}), CallUsage()
        return None, window, CallUsage(calls=1, failed=1)
    return res.data, window, CallUsage(calls=1, input_tokens=res.input_tokens or 0, output_tokens=res.output_tokens or 0, latency_ms=res.latency_ms)


def _verdict(d: dict[str, Any] | None, key: str = "") -> AudienceVerdict:
    if not isinstance(d, dict):
        return AudienceVerdict()
    v = d.get("verdict") if d.get("verdict") in ("YES", "MAYBE", "NO", "INSUFFICIENT_EVIDENCE") else "INSUFFICIENT_EVIDENCE"
    at = d.get("weakens_at_s")
    return AudienceVerdict(verdict=v, reason=str(d.get("reason", ""))[:300], at_ms=int(float(at) * 1000) if isinstance(at, int | float) else None,
                           to_whom=str(d["to_whom"])[:120] if d.get("to_whom") else None)


def _save_frames(frames: FrameCache, times_ms: list[int], out_dir: Path) -> list[EvidenceFrame]:
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for t in sorted(set(times_ms))[:4]:
        fr = frames.get(t, CLOSEUP_WIDTH)
        p = out_dir / f"{fr.timestamp_ms:06d}.jpg"
        if not p.exists():
            p.write_bytes(fr.jpeg)
        saved.append(EvidenceFrame(t_ms=fr.timestamp_ms, path=str(p)))
    return saved


def _save_chronological_reactions(out_dir: Path, *, audit_id: str, video_id: str, version_id: str, artifact_hash: str, evaluator: EvaluatorRecord,
                                  coarse: list[WindowReaction], precision: list[WindowReaction], inspected: list[InspectedWindow],
                                  timeline: AttentionTimeline, provider_stop: dict[str, Any] | None = None) -> Path:
    """Persist the prefix-only window reactions before the whole-video diagnosis runs, so the chronological record exists
    independently of anything the diagnosis later concludes."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "chronological_reactions.json"
    payload = {
        "label": "Saved before the whole-video diagnosis. Each reading used only frames before its window end and words fully spoken by then. "
                 "MODEL-PREDICTED reactions, not audience data.",
        "saved_at": utc_now_iso(), "audit_id": audit_id, "video_id": video_id, "version_id": version_id, "artifact_hash": artifact_hash,
        "evaluator": evaluator.model_dump(mode="json"),
        "coarse_reactions": [r.model_dump(mode="json") for r in coarse],
        "precision_reactions": [r.model_dump(mode="json") for r in precision],
        "inspected_windows": [w.model_dump(mode="json") for w in inspected],
        "attention_timeline": timeline.model_dump(mode="json"),
    }
    if provider_stop is not None:
        payload["provider_stop"] = provider_stop
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    tmp.replace(path)
    return path


def _persist_audit_report(report: AuditReport, data_dir: Path) -> None:
    out_dir = data_dir / "audit" / report.id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "audit.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")


@traced(
    "directorloop.cold_audience_audit", kind="agent",
    display=lambda i: f"Cold audience audit | {i['version_id']} | {i['video_id']}",
    summarize=lambda report: {
        "audit_id": report.id, "status": report.status, "overall_summary": report.overall_summary,
        "findings": len(report.findings), "strengths": len(report.strengths), "model_calls": report.model_calls,
        "coverage_mode": report.coverage.mode, "coverage_ms": report.coverage.covered_ms(),
        "duration_ms": report.duration_ms, "incomplete_reasons": report.incomplete_reasons,
        "evidence_label": report.audience.label, "audience": report.audience,
    },
)
@workflow_session(persist=_persist_audit_report)
def run_audit(*, video_id: str, video_path: Path, version_id: str, provider: MediaProbeProvider, data_dir: Path, on_stage: Any = None, closeups: bool = True,
              attention: AttentionConfig | None = None) -> AuditReport:
    def emit(stage: str, msg: str, data: dict | None = None) -> None:
        if on_stage:
            on_stage(stage, msg, data)

    cfg = attention or AttentionConfig.from_settings(get_settings())
    provider = _AuditProviderGate(provider)
    started = time.monotonic()
    audit_id = new_id("audit")
    out_dir = data_dir / "audit" / audit_id
    timings: dict[str, int] = {}
    incomplete: list[str] = []
    with workflow_stage("ingest", "Inspect video and prepare evidence", inputs={"video_id": video_id, "version_id": version_id}) as ingest:
        with workflow_stage("inspect_media", "Inspect rendered video") as inspect:
            info = inspect_media(video_path)
            duration = int(info.duration_ms or 0)
            artifact_hash = sha256_file(video_path)
            inspect.update(duration_ms=duration, has_audio=info.has_audio, artifact_hash=artifact_hash)
        if not info.has_audio:
            incomplete.append("no audio stream: speech and sound could not be reviewed")

        t = time.monotonic()
        emit("ANALYZING", "measuring cuts, motion and loudness; transcribing the rendered audio")
        with workflow_stage("measure_signals", "Measure cuts, motion and loudness") as signal_stage:
            signals = extract_signals(video_path, duration, int(info.width or 0), int(info.height or 0))
            signal_stage.update(cuts_ms=signals.cuts_ms, motion_samples=len(signals.motion), audio_energy_samples=len(signals.rms), evidence_label="Mechanical measurements")
        if info.has_audio:
            with workflow_stage("transcribe_audio", "Transcribe rendered audio") as asr:
                transcript = transcribe(video_path)
                asr.update(source=transcript.source, model=transcript.model, has_speech=transcript.has_speech, note=transcript.note,
                           requested_language=transcript.requested_language, detected_language=transcript.detected_language)
        else:
            transcript = Transcript(text="", has_speech=False, note="no audio stream")
        words = words_from_transcript(transcript)
        frames = FrameCache(video_path, duration)
        timings["signals_asr_ms"] = int((time.monotonic() - t) * 1000)
        ingest.update(duration_ms=duration, transcript_words=len(words), transcript_note=transcript.note,
                      coverage_mode="sampled_frames_plus_asr", frames_extracted="on demand in each review; not continuous video")

    fps = getattr(info, "fps", None)
    t = time.monotonic()
    wins = plan_windows(duration, cfg.coarse_window_ms)
    emit("COLD_REVIEW", f"unprimed chronological review of {len(wins)} windows")
    with workflow_stage("cold_judge", "Coarse attention scan", kind="agent", inputs={"window_count": len(wins), "duration_ms": duration, "window_ms": cfg.coarse_window_ms}) as judge:
        results = _review_windows(provider, frames, signals, words, wins, fps=fps)
        reactions = [r for r, _ in results]
        windows = [w for _, w in results]
        failed = [r for r in reactions if r.error]
        if failed:
            incomplete.append(f"{len(failed)} of {len(reactions)} window reviews failed; those intervals are not covered")
            judge.status = "incomplete"
        skipped = sum(bool(r.error and r.error.startswith(UNATTEMPTED_WINDOW)) for r in reactions)
        judge.update(windows_reviewed=len(reactions) - len(failed), windows_failed=len(failed) - skipped, windows_skipped=skipped,
                     readings=[f"{fmt_range(r.start_ms, r.end_ms)} {r.attention_risk}/{r.reaction}" for r in reactions],
                     evidence_label="Model judgments from independent prefix reviews, not measured audience retention")
    timings["cold_review_ms"] = int((time.monotonic() - t) * 1000)
    emit("COLD_REVIEW", "window reactions: " + "; ".join(f"{r.start_ms / 1000:.0f}-{r.end_ms / 1000:.0f}s {r.reaction}/{r.attention_risk}" for r in reactions))

    # Where predicted risk rises, look again with shorter windows. The scheduler sees every coarse result; each precision
    # reviewer sees only the video up to the end of its own window and is never told why it was asked.
    with workflow_stage("attention_transitions", "Detect attention transitions", inputs={"windows": len(reactions)}) as transitions:
        plans = [] if provider.reason else plan_precision(reactions, duration, cfg)
        if provider.reason:
            transitions.status = "skipped"
            transitions.update(reason=provider.reason)
        transitions.update(
            precision_enabled=cfg.precision_enabled,
            transitions=[{"transition_type": p.scan.trigger, "coarse_region_ms": [p.scan.trigger_window.start_ms, p.scan.trigger_window.end_ms],
                          "region_ms": [p.scan.region.start_ms, p.scan.region.end_ms], "status": p.scan.status, "skipped_reason": p.scan.skipped_reason} for p in plans],
            precision_windows_planned=sum(len(p.windows) for p in plans),
        )
    t = time.monotonic()
    precision_reactions: list[WindowReaction] = []
    if any(p.windows for p in plans):
        emit("PRECISION_SCAN", "closer prefix reviews where predicted attention risk rose: " + "; ".join(p.scan.trigger for p in plans if p.windows))
        precision_reactions, precision_windows = precision_scan(provider, frames, signals, words, plans, cfg, fps)
        windows.extend(precision_windows)
    timings["precision_ms"] = int((time.monotonic() - t) * 1000)
    evaluator = EvaluatorRecord(
        cold_viewer_prompt_version=COLD_VIEWER_PROMPT_VERSION, provider=provider.capability.name, model=provider.capability.model,
        reasoning_effort=getattr(provider, "reasoning_effort", None), coarse_window_ms=cfg.coarse_window_ms, precision_enabled=cfg.precision_enabled,
        precision_window_ms=cfg.precision_window_ms, precision_lead_ms=cfg.precision_lead_ms, max_precision_regions=cfg.max_precision_regions,
        max_precision_windows=cfg.max_precision_windows,
        asr_language=transcript.requested_language,
        frame_sampling=(f"earlier moments {CONTEXT_FPS:g}/s at {CONTEXT_WIDTH}px; coarse windows {CURRENT_FPS:g}/s at {CURRENT_WIDTH}px; "
                        f"precision windows {cfg.precision_frames} frames at {CURRENT_WIDTH}px; frames requested at least one frame interval before each window end"),
    )
    with workflow_stage("attention_timeline", "Build attention timeline") as timeline_stage:
        timeline = build_attention_timeline(reactions, precision_reactions, [p.scan for p in plans], duration, evaluator)
        s = timeline.summary
        timeline_stage.update(
            summary=s.text, peak_risk=s.peak_risk, first_risk_increase=s.first_risk_increase, first_high_risk=s.first_high_risk,
            peak_regions=s.peak_regions, recovery_regions=s.recovery_regions, predicted_dropoff_regions=s.predicted_dropoff_regions,
            precision_scans_triggered=s.precision_scans_triggered, precision_windows_reviewed=s.precision_windows_reviewed,
            failure_regions=[{"region_ms": [r.start_ms, r.end_ms], "failure_type": r.failure_type, "severity": r.severity,
                              "onset_ms": [r.onset.start_ms, r.onset.end_ms], "refined_by_precision": r.refined_by_precision} for r in timeline.failure_regions],
            evidence_label="Deterministic analysis of model-predicted reactions; not measured retention",
        )
        stop_receipt = provider.stop_receipt()
        if stop_receipt is not None:
            stop_receipt["windows_unattempted"] = sum(bool(r.error and r.error.startswith(UNATTEMPTED_WINDOW)) for r in reactions + precision_reactions)
        checkpoint = _save_chronological_reactions(out_dir, audit_id=audit_id, video_id=video_id, version_id=version_id, artifact_hash=artifact_hash,
                                                   evaluator=evaluator, coarse=reactions, precision=precision_reactions, inspected=windows, timeline=timeline,
                                                   provider_stop=stop_receipt)
        timeline_stage.update(chronological_reactions_saved=str(checkpoint))
    precision_failed = [r for r in precision_reactions if r.error]

    if provider.reason:
        with workflow_stage("diagnose", "Diagnose weaknesses and preserve strengths", kind="agent") as diagnosis:
            diagnosis.status = "skipped"
            diagnosis.update(reason=provider.reason, provider_stop=stop_receipt, audit_id=audit_id,
                             chronological_reactions_saved=str(checkpoint))
        emit("PROVIDER_STOPPED", provider.reason, {"audit_id": audit_id, "provider_stop": stop_receipt})
        raise ProviderError(provider.reason)

    t = time.monotonic()
    emit("DIAGNOSING", "whole-story diagnosis from frames, timed words, measurements and the window reactions")
    with workflow_stage("diagnose", "Diagnose weaknesses and preserve strengths", kind="agent") as diagnosis:
        diag, diag_window, diag_usage = diagnostic_pass(provider, frames, signals, words, reactions, duration, attention_brief(timeline, precision_reactions))
        windows.append(diag_window)
        diagnosis.update(findings=len(diag.get("findings", [])), strengths=len(diag.get("strengths", [])),
                         overall_summary=str(diag.get("overall_summary", ""))[:800], evidence_label="Model judgment")
    timings["diagnose_ms"] = int((time.monotonic() - t) * 1000)

    findings: list[AuditFinding] = []
    raw_findings = [f for f in diag.get("findings", []) if isinstance(f, dict)]
    t = time.monotonic()
    closeup_results: list[tuple[dict[str, Any] | None, InspectedWindow, CallUsage]] = []
    if closeups and raw_findings:
        emit("VERIFYING", f"closer inspection of {len(raw_findings)} suspected moments")
        with workflow_stage("closeup_checks", "Verify suspected moments in close-up", kind="agent", inputs={"findings": len(raw_findings)}) as checks:
            with weave.ThreadPoolExecutor(max_workers=min(4, len(raw_findings))) as ex:
                closeup_results = list(ex.map(lambda f: closeup_verify(provider, frames, words, f, duration), raw_findings))
            unavailable = sum(result is None for result, _, _ in closeup_results)
            if unavailable:
                checks.status = "incomplete"
            skipped = sum(usage.calls == 0 for _, _, usage in closeup_results)
            if provider.reason:
                incomplete.append(f"close-up verification failed: {provider.reason}; {skipped} checks were not attempted")
                receipt = provider.stop_receipt()
                receipt["diagnosis_attempted"] = True
                receipt["closeups_unattempted"] = skipped
                checks.update(provider_stop=receipt)
            checks.update(checked=sum(usage.calls for _, _, usage in closeup_results), unavailable=unavailable - skipped, skipped=skipped,
                          confirmed=sum(result is not None and result.get("happens") is not False for result, _, _ in closeup_results),
                          evidence_label="Model verification of sampled frames and transcript")
    timings["closeup_ms"] = int((time.monotonic() - t) * 1000)
    for i, f in enumerate(raw_findings):
        start_ms = int(max(0.0, float(f.get("start_s", 0))) * 1000)
        end_ms = int(min(duration / 1000, float(f.get("end_s", 0))) * 1000)
        note, verified = "", False
        if closeup_results:
            cu, cw, closeup_usage = closeup_results[i]
            windows.append(cw)
            if cu is not None:
                if cu.get("happens") is False:
                    note = "closer inspection did not find this moment; kept with high uncertainty: " + str(cu.get("note", ""))[:200]
                    f["uncertainty"] = "high"
                else:
                    verified = True
                    if isinstance(cu.get("start_s"), int | float) and isinstance(cu.get("end_s"), int | float) and cu["end_s"] > cu["start_s"]:
                        start_ms, end_ms = int(cu["start_s"] * 1000), int(cu["end_s"] * 1000)
                    corrections = cu.get("corrected_observations") or []
                    note = ("corrections: " + "; ".join(corrections)[:300]) if corrections else str(cu.get("note", ""))[:200]
            else:
                note = "closer inspection was not attempted after terminal provider failure" if closeup_usage.calls == 0 else "closer inspection failed (provider error)"
        if end_ms <= start_ms:
            end_ms = min(duration, start_ms + 500)
        ev_times = [int(float(x) * 1000) for x in f.get("evidence_times_s", []) if isinstance(x, int | float)] or [start_ms, (start_ms + end_ms) // 2, end_ms - 1]
        findings.append(AuditFinding(
            id=f"{audit_id}_f{i + 1}", version_id=version_id, start_ms=start_ms, end_ms=end_ms,
            evidence_frames=_save_frames(frames, [min(max(start_ms, x), end_ms) for x in ev_times], out_dir / "frames"),
            observed=[Observation(kind=o.get("kind", "visual") if o.get("kind") in ("visual", "audio", "caption", "mechanical") else "visual", text=str(o.get("text", ""))[:300],
                                  t_ms=int(float(o["t_s"]) * 1000) if isinstance(o.get("t_s"), int | float) else None) for o in f.get("observed", []) if isinstance(o, dict)],
            viewer_understanding=str(f.get("viewer_understanding", ""))[:400], predicted_reaction=str(f.get("predicted_reaction", ""))[:300],
            weakness=str(f.get("weakness", ""))[:400], issue_type=f.get("issue_type") if f.get("issue_type") in ISSUE_TYPES else "other",
            alternatives=[str(a)[:200] for a in f.get("alternatives", [])][:4], objective=f.get("objective") if f.get("objective") in OBJECTIVES else "other",
            severity=f.get("severity") if f.get("severity") in ("low", "medium", "high") else "medium",
            uncertainty=f.get("uncertainty") if f.get("uncertainty") in ("low", "medium", "high") else "medium",
            proposed_repair=str(f.get("proposed_repair", ""))[:400], keep_unchanged=[str(k)[:160] for k in f.get("keep_unchanged", [])][:6],
            repair_kind=f.get("repair_kind") if f.get("repair_kind") in REPAIR_KINDS else "other",
            verified_closeup=verified, closeup_note=note,
        ))
    strengths = [
        Strength(id=f"{audit_id}_s{i + 1}", start_ms=int(float(s.get("start_s", 0)) * 1000), end_ms=int(float(s.get("end_s", 0)) * 1000), what=str(s.get("what", ""))[:300], why=str(s.get("why", ""))[:300],
                 evidence_frames=_save_frames(frames, [int(float(x) * 1000) for x in s.get("evidence_times_s", []) if isinstance(x, int | float)][:2] or [int(float(s.get("start_s", 0)) * 1000)], out_dir / "frames"))
        for i, s in enumerate(diag.get("strengths", [])) if isinstance(s, dict)
    ]
    aud = diag.get("audience", {}) if isinstance(diag.get("audience"), dict) else {}
    audience = AudienceResponses(**{k: _verdict(aud.get(k)) for k in ("stop", "continue_watching", "finish", "like", "send", "comment", "save_or_replay", "visit_creator")})
    link_findings(timeline, findings)
    total_frames = sum(len(w.frame_timestamps_ms) + w.context_frames for w in windows)
    attention_limits = [
        f"attention risk is a model prediction per reviewed window ({cfg.coarse_window_ms / 1000:g} s coarse windows"
        + (f", {cfg.precision_window_ms / 1000:g} s precision windows only around rises in predicted risk" if cfg.precision_enabled else "")
        + "); it is not measured retention, and no timestamp is more precise than the window that contains it",
    ]
    if precision_failed:
        attention_limits.append(f"{len(precision_failed)} of {len(precision_reactions)} precision reviews were unavailable; those moments keep their coarse reading")
    coverage = CoverageRecord(
        mode="sampled_frames_plus_asr", duration_ms=duration, windows=windows, total_frames_sent=total_frames,
        transcript_source=f"{transcript.source}:{transcript.model}" if transcript.text else "no speech transcribed",
        audio_inspection="whisper.cpp transcript with word timings plus software loudness and silence measurements; the reviewer did not listen to music, sound effects, voice tone or audio-visual sync",
        provider=provider.capability.name, model=provider.capability.model,
        limitations=[
            f"frames are samples: {CURRENT_FPS:g} per second in reviewed windows; the whole-story diagnosis saw {len(diag_window.frame_timestamps_ms)} frames spread across the video "
            f"(about one every {duration / 1000 / max(1, len(diag_window.frame_timestamps_ms)):.1f} s); close-ups saw at most {CLOSEUP_MAX_FRAMES} frames spread across each flagged span "
            f"(up to {CLOSEUP_FPS:g} per second); events between samples can be missed",
            "not a frame-by-frame audiovisual review; the provider receives still images, not the video file",
            "this audit uses sampled frames, a local whisper.cpp transcript and measured loudness/silence; it does not request native video or audio review",
            (f"local whisper.cpp ASR language setting: {transcript.requested_language}; detected language: {transcript.detected_language or 'unknown'}; "
             "automatic language identification and transcription may be wrong, especially for music, names or mixed-language speech"),
            f"each prefix review includes at most the latest {VIEWER_WORD_LIMIT} fully spoken transcript words; earlier speech may be omitted",
            "frame timestamps record the bounded requests sent with the images, not independently measured decoded presentation times; seeking can return the next frame or hold the final frame",
        ] + attention_limits + incomplete,
        cold_viewer_prompt_version=COLD_VIEWER_PROMPT_VERSION,
    )

    def usage_of(items: list[WindowReaction]) -> CallUsage:
        return CallUsage(calls=len(items), failed=sum(1 for r in items if r.error), input_tokens=sum(r.input_tokens or 0 for r in items),
                         output_tokens=sum(r.output_tokens or 0 for r in items), latency_ms=sum(r.latency_ms for r in items))

    call_usage = {"coarse": usage_of(reactions), "precision": usage_of(precision_reactions), "diagnosis": diag_usage,
                  "closeup": CallUsage(calls=sum(u.calls for _, _, u in closeup_results), failed=sum(u.failed for _, _, u in closeup_results),
                                       input_tokens=sum(u.input_tokens for _, _, u in closeup_results), output_tokens=sum(u.output_tokens for _, _, u in closeup_results),
                                       latency_ms=sum(u.latency_ms for _, _, u in closeup_results))}
    call_id, url = current_call_ref()
    report = AuditReport(
        id=audit_id, video_id=video_id, version_id=version_id, artifact_hash=artifact_hash, artifact_path=str(video_path), duration_ms=duration, created_at=utc_now_iso(),
        status="incomplete" if incomplete and any("failed" in x for x in incomplete) else "complete", coverage=coverage, window_reactions=reactions,
        precision_reactions=precision_reactions, attention=timeline, call_usage=call_usage, findings=findings,
        strengths=strengths, audience=audience, overall_summary=str(diag.get("overall_summary", ""))[:800], weave_url=url, weave_call_id=call_id,
        timings_ms={**timings, "total_ms": int((time.monotonic() - started) * 1000)},
        model_calls=sum(usage.calls for usage in call_usage.values()), incomplete_reasons=incomplete,
    )
    _persist_audit_report(report, data_dir)
    emit("AUDIT_DONE", f"audit {audit_id}: {len(findings)} findings, {len(strengths)} strengths; attention: {timeline.summary.text}", {"audit_id": audit_id, "weave_url": url})
    return report
