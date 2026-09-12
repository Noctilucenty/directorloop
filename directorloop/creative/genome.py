"""Video -> CreativeGenome.

Mechanical: duration, geometry, shot cuts, motion curve, audio energy, static spans.
ASR: timestamped transcript of the rendered audio (whisper.cpp), sentence-aligned beats.
Vision/language model (one call): beat roles, questions and claims, redundancy, open loops,
hook type, first-visual description. Each feature records its source and confidence.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..domain.creative import (
    BeatRole,
    CreativeGenome,
    Feature,
    FeatureSource,
    HookProfile,
    HookType,
    OpenLoop,
    Shot,
    TimelineBeat,
)
from ..domain.ids import new_id, sha256_file, utc_now_iso
from ..media.frames import extract_frame
from ..media.probe import inspect_media
from ..media.transcribe import Transcript, transcribe
from ..observability.weave_ops import traced
from ..providers.base import MediaProbeProvider, ProbeMedia, ProviderError
from .signals import (
    VideoSignals,
    extract_signals,
    first_meaningful_motion_ms,
    first_visual_change_ms,
    snap_to_quiet,
    static_spans,
)

GENOME_VERSION = "1.1"
HOOK_WINDOW_MS = 3000
MAX_BEAT_MS = 6500
MIN_BEAT_MS = 1100


@dataclass
class RawBeat:
    start_ms: int
    end_ms: int
    text: str


def split_segments_at_sentences(transcript: Transcript) -> list[RawBeat]:
    """Split whisper segments at sentence punctuation using token timestamps (approximate; cuts are snapped later)."""
    out: list[RawBeat] = []
    for seg in transcript.segments:
        text = seg.text.strip()
        if not text:
            continue
        if not seg.tokens:
            out.append(RawBeat(seg.start_ms, seg.end_ms, text))
            continue
        cur_tokens: list[str] = []
        cur_start: int | None = None
        for tok in seg.tokens:
            if cur_start is None:
                cur_start = tok.start_ms  # the sentence starts when its first word starts, not when the last one ended
            cur_tokens.append(tok.text)
            if re.search(r"[.?!]['\"]?\s*$", tok.text):
                sentence = "".join(cur_tokens).strip()
                if sentence:
                    out.append(RawBeat(cur_start, tok.end_ms, sentence))
                cur_tokens = []
                cur_start = None
        rest = "".join(cur_tokens).strip()
        if rest:
            out.append(RawBeat(cur_start if cur_start is not None else seg.start_ms, seg.end_ms, rest))
    return out


def sentence_beats(transcript: Transcript, duration_ms: int, signals: VideoSignals) -> list[RawBeat]:
    """Sentence-sized beats that tile the whole video; short sentences merge with the next one."""
    segs = split_segments_at_sentences(transcript)
    if not segs:
        return [RawBeat(0, duration_ms, "")]
    beats: list[RawBeat] = []
    cur_start, cur_end, cur_text = segs[0].start_ms, segs[0].end_ms, segs[0].text
    for s in segs[1:]:
        ends_sentence = bool(re.search(r"[.?!]['\"]?$", cur_text))
        too_long = s.end_ms - cur_start > MAX_BEAT_MS
        if (ends_sentence and cur_end - cur_start >= MIN_BEAT_MS) or too_long:
            beats.append(RawBeat(cur_start, cur_end, cur_text))
            cur_start, cur_end, cur_text = s.start_ms, s.end_ms, s.text
        else:
            cur_end = s.end_ms
            cur_text = f"{cur_text} {s.text}".strip()
    beats.append(RawBeat(cur_start, cur_end, cur_text))
    # merge a trailing fragment that is too short to stand alone
    if len(beats) > 1 and beats[-1].end_ms - beats[-1].start_ms < MIN_BEAT_MS // 2:
        last = beats.pop()
        beats[-1] = RawBeat(beats[-1].start_ms, last.end_ms, f"{beats[-1].text} {last.text}".strip())
    # tile: cut points at the quietest moment between consecutive beats; first starts at 0, last ends at the end
    tiled: list[RawBeat] = []
    for i, b in enumerate(beats):
        start = 0 if i == 0 else tiled[-1].end_ms
        if i == len(beats) - 1:
            end = duration_ms
        else:
            nxt = beats[i + 1]
            gap_mid = (b.end_ms + nxt.start_ms) // 2 if nxt.start_ms > b.end_ms else b.end_ms
            end = snap_to_quiet(signals.rms, gap_mid, window_ms=max(200, min(300, (nxt.start_ms - b.end_ms) // 2 + 120)))
            end = max(start + 300, min(end, duration_ms - 300))
        tiled.append(RawBeat(start, end, b.text))
    return tiled


LABEL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "beats": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "role": {"type": "string", "enum": [r.value for r in BeatRole]},
                    "role_confidence": {"type": "number"},
                    "is_question": {"type": "boolean"},
                    "is_claim": {"type": "boolean"},
                    "introduces_new_information": {"type": "boolean"},
                    "redundant_with_index": {"type": ["integer", "null"]},
                },
                "required": ["index", "role", "role_confidence", "is_question", "is_claim", "introduces_new_information", "redundant_with_index"],
            },
        },
        "hook_type": {"type": "string", "enum": [h.value for h in HookType]},
        "hook_type_confidence": {"type": "number"},
        "first_visual_description": {"type": "string"},
        "face_visible_at_start": {"type": "boolean"},
        "subject_visible_at_start": {"type": "boolean"},
        "open_loops": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "opened_index": {"type": "integer"},
                    "resolved_index": {"type": ["integer", "null"]},
                    "description": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["opened_index", "resolved_index", "description", "confidence"],
            },
        },
    },
    "required": ["beats", "hook_type", "hook_type_confidence", "first_visual_description", "face_visible_at_start", "subject_visible_at_start", "open_loops"],
}

LABEL_INSTRUCTION = """You are analyzing the structure of a short vertical video. You see frames from the video (the first three are from its opening seconds) and its spoken transcript split into numbered beats with timestamps.

Label every beat with exactly one role:
- hook: the opening line or moment meant to stop someone scrolling (a question, a surprising claim, a striking image)
- setup: introduces who or what the video is about
- context: background information that helps understanding but is not the surprising point itself
- problem: raises a problem, mystery or stakes
- tension: builds suspense toward an answer
- proof: shows or states the evidence that the surprising claim is true
- mechanism: explains why or how it happens
- payoff: the answer or reveal that resolves the question the video raised
- cta: a call to action, outro or follow prompt
- other: none of the above

Also mark whether each beat asks a question, makes a factual claim, introduces new information, and which earlier beat it merely repeats (or null).
Identify the hook type, describe the first visual in one plain sentence, say whether a human face is visible at the start and whether the video's main subject is visible at the start.
List open loops: a question or promise raised in one beat (opened_index) and the beat that resolves it (resolved_index, or null if never resolved).
Judge only what is in these frames and this transcript. Confidence values are between 0 and 1.

Beats:
{beats}
"""


def _label_with_model(
    provider: MediaProbeProvider, path: Path, duration_ms: int, beats: list[RawBeat]
) -> tuple[dict[str, Any] | None, str | None, int]:
    stamps = [400, 1400, 2500] + [int(duration_ms * f) for f in (0.35, 0.55, 0.75, 0.92)]
    frames = [extract_frame(path, min(max(0, t), max(0, duration_ms - 100)), max_width=448) for t in stamps]
    beat_text = "\n".join(f"[{i}] {b.start_ms / 1000:.1f}-{b.end_ms / 1000:.1f}s: {b.text or '(no speech)'}" for i, b in enumerate(beats))
    media = ProbeMedia(kind="frames", duration_ms=duration_ms, frames=frames, transcript=None)
    t0 = time.monotonic()
    try:
        res = provider.judge_json(media, LABEL_INSTRUCTION.format(beats=beat_text), LABEL_SCHEMA)
    except ProviderError as exc:
        return None, str(exc)[:200], int((time.monotonic() - t0) * 1000)
    return res.data, None, res.latency_ms


def _shots(signals: VideoSignals) -> list[Shot]:
    bounds = [0] + [c for c in signals.cuts_ms if 0 < c < signals.duration_ms] + [signals.duration_ms]
    shots = []
    for i, (a, b) in enumerate(zip(bounds, bounds[1:], strict=False)):
        vals = [v for t, v in signals.motion if a <= t < b]
        shots.append(Shot(id=f"shot_{i:02d}", start_ms=a, end_ms=b, motion=float(sum(vals) / len(vals)) if vals else 0.0))
    return shots


@traced("analyze_creative", kind="tool")
def extract_genome(
    video_path: str | Path,
    provider: MediaProbeProvider | None,
    cache_dir: Path,
    category: str = "educational_short",
    declared_objective: str = "",
    audience: str = "general",
    artifact_hash: str | None = None,
    use_cache: bool = True,
) -> CreativeGenome:
    video_path = Path(video_path)
    artifact_hash = artifact_hash or sha256_file(video_path)
    model = provider.capability.model if provider else "none"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"genome_{artifact_hash[:24]}_{GENOME_VERSION}_{model.replace('/', '_')}.json"
    if use_cache and cache_file.exists():
        return CreativeGenome.model_validate_json(cache_file.read_text(encoding="utf-8"))

    notes: list[str] = []
    info = inspect_media(video_path)
    duration = int(info.duration_ms or 0)
    width, height = int(info.width or 0), int(info.height or 0)
    signals = extract_signals(video_path, duration, width, height)
    transcript = transcribe(video_path)
    if not transcript.text:
        notes.append(f"no speech transcribed: {transcript.note}")
    raw_beats = sentence_beats(transcript, duration, signals)
    labels, label_error, label_ms = (None, "no provider", 0)
    if provider is not None:
        labels, label_error, label_ms = _label_with_model(provider, video_path, duration, raw_beats)
    if label_error:
        notes.append(f"structure labels unavailable: {label_error}")

    shots = _shots(signals)
    beat_labels = {int(b.get("index", -1)): b for b in (labels or {}).get("beats", [])}
    beats: list[TimelineBeat] = []
    for i, rb in enumerate(raw_beats):
        lab = beat_labels.get(i, {})
        inside = [c for c in signals.cuts_ms if rb.start_ms < c < rb.end_ms]
        shot_ids = [s.id for s in shots if s.start_ms < rb.end_ms and s.end_ms > rb.start_ms]
        vals = [v for t, v in signals.motion if rb.start_ms <= t < rb.end_ms]
        try:
            role = BeatRole(lab.get("role", "other"))
        except ValueError:
            role = BeatRole.OTHER
        red = lab.get("redundant_with_index")
        beats.append(
            TimelineBeat(
                id=f"beat_{i:02d}",
                index=i,
                start_ms=rb.start_ms,
                end_ms=rb.end_ms,
                text=rb.text,
                role=role,
                role_source=FeatureSource.LANGUAGE_MODEL if lab else FeatureSource.MECHANICAL,
                role_confidence=float(lab.get("role_confidence", 0.0)) if lab else 0.0,
                is_question=bool(lab.get("is_question", rb.text.strip().endswith("?"))),
                is_claim=bool(lab.get("is_claim", False)),
                introduces_new_information=bool(lab.get("introduces_new_information", True)),
                redundant_with_beat_id=f"beat_{int(red):02d}" if isinstance(red, int) and 0 <= red < len(raw_beats) and red != i else None,
                shot_ids=shot_ids,
                shot_changes_inside=len(inside),
                motion=float(sum(vals) / len(vals)) if vals else 0.0,
                words=len(rb.text.split()),
            )
        )

    loops: list[OpenLoop] = []
    for j, lp in enumerate((labels or {}).get("open_loops", [])):
        oi = lp.get("opened_index")
        ri = lp.get("resolved_index")
        if not isinstance(oi, int) or not 0 <= oi < len(beats):
            continue
        resolved = beats[ri] if isinstance(ri, int) and 0 <= ri < len(beats) else None
        loops.append(
            OpenLoop(
                id=f"loop_{j:02d}",
                opened_beat_id=beats[oi].id,
                opened_ms=beats[oi].start_ms,
                resolved_beat_id=resolved.id if resolved else None,
                resolved_ms=resolved.start_ms if resolved else None,
                description=str(lp.get("description", ""))[:200],
                confidence=float(lp.get("confidence", 0.5)),
            )
        )

    lm_conf = 0.7 if labels else 0.0

    def first_role_ms(*roles: BeatRole) -> Feature:
        hit = next((b for b in beats if b.role in roles), None)
        if hit is None:
            return Feature(value=None, source=FeatureSource.LANGUAGE_MODEL, confidence=lm_conf, note=f"no beat labeled {'/'.join(r.value for r in roles)}")
        return Feature(value=hit.start_ms, source=FeatureSource.LANGUAGE_MODEL, confidence=min(lm_conf, hit.role_confidence or lm_conf), note=hit.id)

    first_proof = first_role_ms(BeatRole.PROOF)
    first_payoff = first_role_ms(BeatRole.PAYOFF)
    anchors = [f.value for f in (first_proof, first_payoff) if isinstance(f.value, int)]
    anchor = min(anchors) if anchors else None
    context_ms = sum(b.duration_ms for b in beats if b.role in (BeatRole.SETUP, BeatRole.CONTEXT, BeatRole.PROBLEM) and anchor is not None and b.start_ms < anchor)
    spans = static_spans(signals.cuts_ms, signals.motion, duration)
    longest = spans[0] if spans else None
    words = sum(b.words for b in beats)
    speech_ms = sum(s.end_ms - s.start_ms for s in transcript.segments) or duration
    first_change = first_visual_change_ms(signals.cuts_ms, signals.motion)
    first_motion = first_meaningful_motion_ms(signals.motion)
    q_beat = next((b for b in beats if b.is_question), None)
    c_beat = next((b for b in beats if b.is_claim), None)
    cta = next((b for b in beats if b.role == BeatRole.CTA), None)
    first_words = " ".join((beats[0].text if beats else "").split()[:10])

    try:
        hook_type = HookType((labels or {}).get("hook_type", "unknown"))
    except ValueError:
        hook_type = HookType.UNKNOWN
    hook = HookProfile(
        hook_start_ms=0,
        hook_end_ms=min(HOOK_WINDOW_MS, duration),
        hook_type=Feature(value=hook_type.value, source=FeatureSource.LANGUAGE_MODEL, confidence=float((labels or {}).get("hook_type_confidence", 0.0))),
        first_spoken_words=Feature(value=first_words, source=FeatureSource.ASR, confidence=0.8 if first_words else 0.0),
        first_visual_description=Feature(value=str((labels or {}).get("first_visual_description", ""))[:240] or None, source=FeatureSource.VISION_MODEL, confidence=lm_conf),
        face_visible_at_start=Feature(value=(labels or {}).get("face_visible_at_start"), source=FeatureSource.VISION_MODEL, confidence=lm_conf),
        product_or_subject_visible_at_start=Feature(value=(labels or {}).get("subject_visible_at_start"), source=FeatureSource.VISION_MODEL, confidence=lm_conf),
        first_visual_change_ms=Feature(value=first_change, source=FeatureSource.MECHANICAL, confidence=0.9, note=f"first shot cut (scene>{signals.scene_threshold}) or motion spike >=0.05 after 150 ms"),
        first_meaningful_motion_ms=Feature(value=first_motion, source=FeatureSource.MECHANICAL, confidence=0.9, note="mean frame difference >= 0.02 at 10 fps"),
        first_claim_ms=Feature(value=c_beat.start_ms if c_beat else None, source=FeatureSource.LANGUAGE_MODEL, confidence=lm_conf),
        first_question_ms=Feature(value=q_beat.start_ms if q_beat else None, source=FeatureSource.LANGUAGE_MODEL, confidence=lm_conf),
    )
    genome = CreativeGenome(
        id=new_id("genome"),
        genome_version=GENOME_VERSION,
        artifact_hash=artifact_hash,
        duration_ms=duration,
        width=width,
        height=height,
        aspect_ratio="9:16" if height > width else ("16:9" if width > height else "1:1"),
        category=category,
        declared_objective=declared_objective,
        audience=audience,
        hook=hook,
        beats=beats,
        shots=shots,
        open_loops=loops,
        transcript_source=f"{transcript.source}:{transcript.model}",
        speech_rate_wps=Feature(value=round(words / max(0.001, speech_ms / 1000), 2), source=FeatureSource.ASR, confidence=0.8),
        first_proof_ms=first_proof,
        first_payoff_ms=first_payoff,
        context_before_proof_ms=Feature(value=context_ms if anchor is not None else None, source=FeatureSource.LANGUAGE_MODEL, confidence=lm_conf, note="setup/context/problem beats before the first proof or payoff"),
        longest_static_span_ms=Feature(value=(longest[1] - longest[0]) if longest else 0, source=FeatureSource.MECHANICAL, confidence=0.85, note="no cut and motion < 0.012"),
        longest_static_span_start_ms=Feature(value=longest[0] if longest else None, source=FeatureSource.MECHANICAL, confidence=0.85),
        shots_per_10s=Feature(value=round(len(shots) / max(0.001, duration / 10000), 2), source=FeatureSource.MECHANICAL, confidence=0.9),
        cta_ms=Feature(value=cta.start_ms if cta else None, source=FeatureSource.LANGUAGE_MODEL, confidence=lm_conf),
        motion_curve=[(t, round(v, 4)) for t, v in signals.motion[:: max(1, len(signals.motion) // 150)]],
        extracted_at=utc_now_iso(),
        extractor={"genome": GENOME_VERSION, "labels": model, "asr": transcript.model, "label_ms": str(label_ms)},
        notes=notes,
    )
    cache_file.write_text(genome.model_dump_json(), encoding="utf-8")
    return genome


def genome_summary(g: CreativeGenome) -> dict[str, Any]:
    """Compact view for UI, traces and planner prompts."""
    return {
        "duration_ms": g.duration_ms,
        "hook_type": g.hook.hook_type.value,
        "first_words": g.hook.first_spoken_words.value,
        "first_visual": g.hook.first_visual_description.value,
        "first_visual_change_ms": g.hook.first_visual_change_ms.value,
        "first_proof_ms": g.first_proof_ms.value,
        "first_payoff_ms": g.first_payoff_ms.value,
        "context_before_proof_ms": g.context_before_proof_ms.value,
        "longest_static_span_ms": g.longest_static_span_ms.value,
        "shots_per_10s": g.shots_per_10s.value,
        "speech_rate_wps": g.speech_rate_wps.value,
        "beats": [{"id": b.id, "ms": [b.start_ms, b.end_ms], "role": b.role.value, "text": b.text} for b in g.beats],
        "open_loops": [{"opened": lp.opened_ms, "resolved": lp.resolved_ms, "what": lp.description} for lp in g.open_loops],
    }


def save_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
