"""Cold-audience audit of an actual rendered video.

1. Unprimed prefix review: one independent call per chronological window. The reviewer receives only what an
   unfamiliar viewer has seen and heard up to that moment (earlier moments as small frames, the latest window as
   larger frames, the words heard so far, and loudness/silence measurements). No creator notes, no labels.
2. Diagnostic pass: the whole story (frames across the video, timed transcript, mechanical signals) plus the window
   reactions produce findings, preserved strengths and audience predictions, observations separate from interpretation.
3. Close-up verification: dense frames around each suspected problem confirm or correct what happens and when.
Coverage records exactly what was sent.
"""

from __future__ import annotations

import concurrent.futures as cf
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import weave

from ..creative.signals import RMS_HOP_MS, VideoSignals, extract_signals, silence_gaps, static_spans
from ..domain.ids import new_id, sha256_file, utc_now_iso
from ..media.frames import SampledFrame, extract_frame
from ..media.probe import inspect_media
from ..media.transcribe import Transcript, transcribe
from ..observability.weave_ops import current_call_ref, set_display_name, traced
from ..providers.base import MediaProbeProvider, ProbeMedia, ProviderError
from .models import (
    ISSUE_TYPES,
    OBJECTIVES,
    AudienceResponses,
    AudienceVerdict,
    AuditFinding,
    AuditReport,
    CoverageRecord,
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


def _audio_lines(signals: VideoSignals, words: list[Word], start_ms: int, end_ms: int) -> list[str]:
    spoken = [w for w in words if w.start_ms < end_ms and w.end_ms > start_ms]
    lines = [f"speech: {'yes, ' + str(len(spoken)) + ' words' if spoken else 'no words transcribed'}"]
    if len(signals.rms):
        a, b = start_ms // RMS_HOP_MS, max(start_ms // RMS_HOP_MS + 1, end_ms // RMS_HOP_MS)
        seg = signals.rms[a:b]
        if len(seg):
            ref = float(np.percentile(signals.rms, 90)) or 1e-6
            lines.append(f"loudness: {float(seg.mean()) / ref:.2f} of the video's typical speech level")
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
    },
    "required": ["understanding", "expectation", "open_question", "reaction", "attention_risk", "cause"],
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
- cause: the specific thing in the moment you just watched (what you saw, read on screen, or heard) that caused this reaction"""


def _words_block(words: list[Word], start_ms: int, end_ms: int) -> str:
    heard = [w for w in words if w.start_ms < end_ms]
    if not heard:
        return "(no words yet)"
    lines = []
    for w in heard:
        mark = ">>" if w.start_ms >= start_ms else "  "
        lines.append(f"{mark} {w.start_ms / 1000:.1f}s {w.text}")
    return "\n".join(lines[-60:])


@traced("cold_review_window", kind="llm")
def review_window(provider: MediaProbeProvider, frames: FrameCache, signals: VideoSignals, words: list[Word], start_ms: int, end_ms: int) -> tuple[WindowReaction, InspectedWindow]:
    set_display_name(f"cold_review_{start_ms / 1000:.1f}-{end_ms / 1000:.1f}s")
    ctx_times = _times(0, start_ms, CONTEXT_FPS) if start_ms > 0 else []
    cur_times = _times(start_ms, end_ms, CURRENT_FPS)
    ctx = frames.many(ctx_times, CONTEXT_WIDTH)
    cur = frames.many(cur_times, CURRENT_WIDTH)
    media = ProbeMedia(kind="frames", duration_ms=end_ms, frames=ctx + cur, transcript=None)
    instruction = WINDOW_INSTRUCTION.format(start=start_ms / 1000, end=end_ms / 1000, words=_words_block(words, start_ms, end_ms), audio="; ".join(_audio_lines(signals, words, start_ms, end_ms)))
    window = InspectedWindow(kind="prefix", start_ms=start_ms, end_ms=end_ms, frame_timestamps_ms=cur_times, frame_width=CURRENT_WIDTH,
                             context_frames=len(ctx_times), context_width=CONTEXT_WIDTH, transcript_words=len([w for w in words if w.start_ms < end_ms]))
    t0 = time.monotonic()
    try:
        res = provider.judge_json(media, instruction, WINDOW_SCHEMA)
    except ProviderError as exc:
        return WindowReaction(start_ms=start_ms, end_ms=end_ms, error=str(exc)[:200], latency_ms=int((time.monotonic() - t0) * 1000)), window
    d = res.data
    return WindowReaction(
        start_ms=start_ms, end_ms=end_ms, understanding=str(d.get("understanding", ""))[:400], expectation=str(d.get("expectation", ""))[:300],
        open_question=str(d.get("open_question", ""))[:240], reaction=d.get("reaction", "unknown") if d.get("reaction") in ("engaged", "neutral", "losing_interest", "confused") else "unknown",
        attention_risk=d.get("attention_risk", "unknown") if d.get("attention_risk") in ("low", "medium", "high") else "unknown", cause=str(d.get("cause", ""))[:300],
        latency_ms=res.latency_ms,
    ), window


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

Produce:
1. findings: only moments with real evidence of a weakness in attention, understanding, emotional payoff or motivation to engage. Zero findings is allowed. Do not invent problems in every interval; stillness, silence, slow pacing or a delayed answer can be deliberate and good. For each finding give the exact interval, the directly OBSERVED facts (what is visible, what caption text is on screen, what is said, what the measurements show; each with its time) kept separate from interpretation, what the viewer likely understands or expects at that moment, the predicted reaction (a model judgment), the suspected weakness, other plausible explanations, the affected objective, severity and uncertainty, a specific repair (never "make it more engaging"), what must stay unchanged, the kind of repair it needs, and the times of the most representative frames.
2. strengths: sections that work and must be preserved, with why.
3. audience predictions (YES, MAYBE, NO or INSUFFICIENT_EVIDENCE with a short reason): would the opening make an unfamiliar viewer stop; would they keep watching and where interest may weaken; would they finish; is there a reason to like; would they send it and to whom; a natural reason to comment; value in saving or replaying; a reason to visit the creator. Not every video needs every action.
4. overall_summary: two or three plain sentences.
Use only evidence present in these inputs. Keep timestamps as precise as the frames allow; do not claim precision you cannot see."""


@traced("diagnose_audit", kind="llm")
def diagnostic_pass(provider: MediaProbeProvider, frames: FrameCache, signals: VideoSignals, words: list[Word], reactions: list[WindowReaction], duration_ms: int) -> tuple[dict[str, Any], InspectedWindow]:
    times = _times(0, duration_ms, DIAG_FPS)
    if len(times) > 40:
        times = [times[int(i * len(times) / 40)] for i in range(40)]
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
    instruction = DIAG_INSTRUCTION.format(words="\n".join(f"{w.start_ms / 1000:.1f}s {w.text}" for w in words) or "(no speech)", measurements="\n".join(measurements), reactions="\n".join(reaction_lines) or "(none available)")
    res = provider.judge_json(media, instruction, DIAG_SCHEMA)
    return res.data, InspectedWindow(kind="diagnostic", start_ms=0, end_ms=duration_ms, frame_timestamps_ms=times, frame_width=DIAG_WIDTH, transcript_words=len(words))


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


@traced("closeup_verify", kind="llm")
def closeup_verify(provider: MediaProbeProvider, frames: FrameCache, words: list[Word], finding: dict[str, Any], duration_ms: int) -> tuple[dict[str, Any] | None, InspectedWindow]:
    s = max(0, int((float(finding["start_s"]) - 0.4) * 1000))
    e = min(duration_ms, int((float(finding["end_s"]) + 0.4) * 1000))
    times = _times(s, e, CLOSEUP_FPS)[:16]
    media = ProbeMedia(kind="frames", duration_ms=duration_ms, frames=frames.many(times, CLOSEUP_WIDTH), transcript=None)
    said = " ".join(f"{w.start_ms / 1000:.1f}s {w.text}" for w in words if w.start_ms < e and w.end_ms > s) or "(no words)"
    obs = "\n".join(f"- [{o.get('kind')}] {o.get('text')} (t={o.get('t_s')})" for o in finding.get("observed", []))
    instruction = (
        f"Closer inspection of {s / 1000:.1f}-{e / 1000:.1f}s. Words said in this span: {said}\n"
        f"A reviewer described this moment as: {finding.get('weakness')}\nTheir observations:\n{obs}\n"
        "Using only these frames and words, check each observation. Does the described moment actually happen here? "
        "Give the precise start and end seconds where it happens (null if it does not), list which observations are confirmed, "
        "and correct any that are wrong."
    )
    window = InspectedWindow(kind="closeup", start_ms=s, end_ms=e, frame_timestamps_ms=times, frame_width=CLOSEUP_WIDTH)
    try:
        return provider.judge_json(media, instruction, CLOSEUP_SCHEMA).data, window
    except ProviderError:
        return None, window


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


@traced("directorloop.cold_audience_audit", kind="agent")
def run_audit(*, video_id: str, video_path: Path, version_id: str, provider: MediaProbeProvider, data_dir: Path, on_stage: Any = None, closeups: bool = True) -> AuditReport:
    def emit(stage: str, msg: str, data: dict | None = None) -> None:
        if on_stage:
            on_stage(stage, msg, data)

    set_display_name(f"audit_{video_id}_{version_id}")
    started = time.monotonic()
    audit_id = new_id("audit")
    out_dir = data_dir / "audit" / audit_id
    timings: dict[str, int] = {}
    incomplete: list[str] = []
    info = inspect_media(video_path)
    duration = int(info.duration_ms or 0)
    artifact_hash = sha256_file(video_path)
    if not info.has_audio:
        incomplete.append("no audio stream: speech and sound could not be reviewed")

    t = time.monotonic()
    emit("ANALYZING", "measuring cuts, motion and loudness; transcribing the rendered audio")
    signals = extract_signals(video_path, duration, int(info.width or 0), int(info.height or 0))
    transcript = transcribe(video_path) if info.has_audio else Transcript(text="", has_speech=False, note="no audio stream")
    words = words_from_transcript(transcript)
    frames = FrameCache(video_path, duration)
    timings["signals_asr_ms"] = int((time.monotonic() - t) * 1000)

    t = time.monotonic()
    wins = plan_windows(duration)
    emit("COLD_REVIEW", f"unprimed chronological review of {len(wins)} windows")
    with weave.ThreadPoolExecutor(max_workers=min(6, len(wins))) as ex:
        results = list(ex.map(lambda w: review_window(provider, frames, signals, words, w[0], w[1]), wins))
    reactions = [r for r, _ in results]
    windows = [w for _, w in results]
    failed = [r for r in reactions if r.error]
    if failed:
        incomplete.append(f"{len(failed)} of {len(reactions)} window reviews failed; those intervals are not covered")
    timings["cold_review_ms"] = int((time.monotonic() - t) * 1000)
    emit("COLD_REVIEW", "window reactions: " + "; ".join(f"{r.start_ms / 1000:.0f}-{r.end_ms / 1000:.0f}s {r.reaction}/{r.attention_risk}" for r in reactions))

    t = time.monotonic()
    emit("DIAGNOSING", "whole-story diagnosis from frames, timed words, measurements and the window reactions")
    diag, diag_window = diagnostic_pass(provider, frames, signals, words, reactions, duration)
    windows.append(diag_window)
    timings["diagnose_ms"] = int((time.monotonic() - t) * 1000)

    findings: list[AuditFinding] = []
    raw_findings = [f for f in diag.get("findings", []) if isinstance(f, dict)]
    t = time.monotonic()
    closeup_results: list[tuple[dict[str, Any] | None, InspectedWindow]] = []
    if closeups and raw_findings:
        emit("VERIFYING", f"closer inspection of {len(raw_findings)} suspected moments")
        with weave.ThreadPoolExecutor(max_workers=min(4, len(raw_findings))) as ex:
            closeup_results = list(ex.map(lambda f: closeup_verify(provider, frames, words, f, duration), raw_findings))
    timings["closeup_ms"] = int((time.monotonic() - t) * 1000)
    for i, f in enumerate(raw_findings):
        start_ms = int(max(0.0, float(f.get("start_s", 0))) * 1000)
        end_ms = int(min(duration / 1000, float(f.get("end_s", 0))) * 1000)
        note, verified = "", False
        if closeup_results:
            cu, cw = closeup_results[i]
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
                note = "closer inspection failed (provider error)"
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
    total_frames = sum(len(w.frame_timestamps_ms) + w.context_frames for w in windows)
    coverage = CoverageRecord(
        mode="sampled_frames_plus_asr", duration_ms=duration, windows=windows, total_frames_sent=total_frames,
        transcript_source=f"{transcript.source}:{transcript.model}" if transcript.text else "no speech transcribed",
        audio_inspection="whisper.cpp transcript with word timings plus software loudness and silence measurements; the reviewer did not listen to music, sound effects, voice tone or audio-visual sync",
        provider=provider.capability.name, model=provider.capability.model,
        limitations=[
            f"frames are samples ({CURRENT_FPS:g} per second in reviewed windows, {DIAG_FPS:g} per second for diagnosis, {CLOSEUP_FPS:g} per second in close-ups); events between samples can be missed",
            "not a frame-by-frame audiovisual review; the provider receives still images, not the video file",
            "native video input unavailable with the configured provider (the Gemini native-video path is out of credit)",
        ] + incomplete,
    )
    call_id, url = current_call_ref()
    report = AuditReport(
        id=audit_id, video_id=video_id, version_id=version_id, artifact_hash=artifact_hash, artifact_path=str(video_path), duration_ms=duration, created_at=utc_now_iso(),
        status="incomplete" if incomplete and any("failed" in x for x in incomplete) else "complete", coverage=coverage, window_reactions=reactions, findings=findings,
        strengths=strengths, audience=audience, overall_summary=str(diag.get("overall_summary", ""))[:800], weave_url=url, weave_call_id=call_id,
        timings_ms={**timings, "total_ms": int((time.monotonic() - started) * 1000)}, model_calls=len(wins) + 1 + len(closeup_results), incomplete_reasons=incomplete,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "audit.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
    emit("AUDIT_DONE", f"audit {audit_id}: {len(findings)} findings, {len(strengths)} strengths", {"audit_id": audit_id, "weave_url": url})
    return report
