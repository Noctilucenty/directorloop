"""Render a repair, verify the intended change happened, review the candidate fresh, and compare honestly.

The candidate audit runs in a fresh context: the reviewer never sees the original finding, the proposed repair or which
version is expected to win. Comparison checks the target moment (original interval vs its mapped interval), the whole
video, new weaknesses, audience predictions and protected content, with both presentation orders to expose instability.
"""

from __future__ import annotations

import io
import time
from pathlib import Path
from typing import Any

import numpy as np
import weave
from PIL import Image

from ..creative.mutate import VIDEO_ASSET_ID
from ..domain.edit_plan import EditPlan
from ..domain.ids import new_id, utc_now_iso
from ..media import render_plan
from ..media.frames import SampledFrame, extract_frame, sample_frames
from ..media.transcribe import transcribe
from ..observability.weave_ops import set_display_name, traced
from ..providers.base import MediaProbeProvider, ProbeMedia, ProviderError
from .models import (
    AuditComparison,
    AuditFinding,
    AuditReport,
    ChangeVerification,
    IntervalMapping,
    StabilityRecord,
)

VERDICT_RANK = {"NO": 0, "MAYBE": 1, "YES": 2}


def map_interval(plan: EditPlan, start_ms: int, end_ms: int) -> list[tuple[int, int]]:
    """Where source interval [start, end] of the original (identity timeline) lands in the edited plan."""
    out: list[tuple[int, int]] = []
    t = 0
    for seg in plan.segments:
        a, b = max(start_ms, seg.source_in_ms), min(end_ms, seg.source_out_ms)
        if b > a:
            out.append((t + (a - seg.source_in_ms), t + (b - seg.source_in_ms)))
        t += seg.duration_ms
    return out


def interval_mappings(plan: EditPlan, findings: list[AuditFinding]) -> list[IntervalMapping]:
    maps = []
    for f in findings:
        pieces = map_interval(plan, f.start_ms, f.end_ms)
        if not pieces:
            maps.append(IntervalMapping(orig_start_ms=f.start_ms, orig_end_ms=f.end_ms, new_start_ms=None, new_end_ms=None, note=f"{f.id}: removed in the candidate"))
        else:
            maps.append(IntervalMapping(orig_start_ms=f.start_ms, orig_end_ms=f.end_ms, new_start_ms=min(p[0] for p in pieces), new_end_ms=max(p[1] for p in pieces),
                                        note=f"{f.id}" + (f": split into {len(pieces)} pieces" if len(pieces) > 1 else "")))
    return maps


@traced("render_candidate", kind="tool")
def render_candidate(plan: EditPlan, manifest: Any, video_path: Path, data_dir: Path) -> dict[str, Any]:
    r = render_plan(plan, manifest, {VIDEO_ASSET_ID: video_path}, publish_dir=data_dir / "renders", work_dir=data_dir / "work")
    return {"path": str(r.path), "artifact_hash": r.artifact_hash, "duration_ms": r.duration_ms, "render_ms": r.render_ms + r.verify_ms}


def _norm_tokens(text: str) -> list[str]:
    import re

    return re.findall(r"[a-z0-9']+", text.lower())


def sentence_presence(words: list[Any], sentence: str, start_ms: int | None = None, end_ms: int | None = None, pad_ms: int = 400) -> float:
    """Share of a sentence's consecutive word pairs heard in a transcript (single words for one-word sentences),
    optionally only among words timed inside [start - pad, end + pad]. Pairs make common words insufficient."""
    toks = _norm_tokens(sentence)
    if not toks:
        return 0.0
    heard = [w for w in words if start_ms is None or (w.end_ms > start_ms - pad_ms and w.start_ms < (end_ms if end_ms is not None else 10**9) + pad_ms)]
    heard_toks = _norm_tokens(" ".join(w.text for w in heard))
    if len(toks) == 1:
        return 1.0 if toks[0] in heard_toks else 0.0
    want = list(zip(toks, toks[1:], strict=False))
    have = set(zip(heard_toks, heard_toks[1:], strict=False))
    return sum(1 for b in want if b in have) / len(want)


def removed_source_ranges(original_plan: EditPlan, candidate_plan: EditPlan) -> list[tuple[int, int]]:
    """Source intervals present in the original timeline but absent from the candidate (same single source asset)."""
    kept = sorted((s.source_in_ms, s.source_out_ms) for s in candidate_plan.segments)
    out: list[tuple[int, int]] = []
    for seg in original_plan.segments:
        cur = [(seg.source_in_ms, seg.source_out_ms)]
        for k0, k1 in kept:
            nxt = []
            for a, b in cur:
                if k1 <= a or k0 >= b:
                    nxt.append((a, b))
                    continue
                if k0 > a:
                    nxt.append((a, k0))
                if k1 < b:
                    nxt.append((k1, b))
            cur = nxt
        out += [(a, b) for a, b in cur if b - a >= 40]
    return out


def audio_fidelity(original_path: Path, candidate_path: Path, candidate_plan: EditPlan) -> float | None:
    """Correlation between the candidate's loudness envelope and the envelope the plan implies (source slices in plan order)."""
    from ..creative.signals import RMS_HOP_MS, audio_rms

    src = audio_rms(original_path)
    if len(src) == 0 or float(src.max()) <= 1e-6:
        return None
    parts = []
    for seg in candidate_plan.segments:
        a, b = seg.source_in_ms // RMS_HOP_MS, seg.source_out_ms // RMS_HOP_MS
        piece = src[a:b] if seg.audio_policy == "keep" else np.zeros(max(0, b - a), dtype=src.dtype)
        parts.append(piece)
    planned = np.concatenate(parts) if parts else np.zeros(0)
    cand = audio_rms(candidate_path)
    n = min(len(planned), len(cand))
    if n < 20:
        return 0.0
    x, y = planned[:n].astype(np.float64), cand[:n].astype(np.float64)
    if x.std() < 1e-9 or y.std() < 1e-9:
        return 0.0
    return float(np.corrcoef(x, y)[0, 1])


def _longest_quiet_ms(path: Path, start_ms: int, end_ms: int) -> int:
    from ..creative.signals import audio_rms, silence_gaps

    gaps = silence_gaps(audio_rms(path), max(0, start_ms), end_ms, min_gap_ms=60)
    return max((b - a for a, b in gaps), default=0)


def _zoom_consistency(original: SampledFrame, candidate: SampledFrame, crop: Any, source_w: int, source_h: int) -> tuple[float, float]:
    """(difference to the plain original frame, difference to the original cropped by the planned rectangle)."""
    io_ = Image.open(io.BytesIO(original.jpeg)).convert("L")
    ic = Image.open(io.BytesIO(candidate.jpeg)).convert("L").resize((96, 170))
    sx, sy = io_.width / max(1, source_w), io_.height / max(1, source_h)
    cropped = io_.crop((int(crop.x * sx), int(crop.y * sy), int((crop.x + crop.w) * sx), int((crop.y + crop.h) * sy))).resize((96, 170))
    a = np.asarray(ic).astype(np.int16)
    plain = float(np.abs(np.asarray(io_.resize((96, 170))).astype(np.int16) - a).mean() / 255.0)
    zoom = float(np.abs(np.asarray(cropped).astype(np.int16) - a).mean() / 255.0)
    return plain, zoom


@traced("verify_change", kind="tool")
def verify_change(mutation_type: str, description: str, original_path: Path, candidate_path: Path, original_plan: EditPlan, candidate_plan: EditPlan,
                  removed_or_moved_text: str | None, target: tuple[int, int], moved_source: tuple[int, int] | None = None) -> ChangeVerification:
    """Mechanical and ASR checks that the rendered file contains the intended change, not just a successful exit code.

    REMOVE_BEAT: the sentence's word pairs are gone from the candidate speech (beyond what the rest of the original already says).
    MOVE_BEAT_*: the sentence is heard at the position the plan moved it to, and that position differs from where it was.
    TRIM_PAUSE: the sentence is still heard and the longest quiet stretch at the cut is shorter than the original pause.
    PUNCH_IN: the frame at the target differs from the original and matches the original cropped by the planned rectangle.
    Every type: the file's duration matches the plan.
    """
    from ..media.probe import inspect_media
    from .review import words_from_transcript

    checks: list[str] = []
    ok = True
    info = inspect_media(candidate_path)
    exp = candidate_plan.timeline_duration_ms()
    dur_ok = info.duration_ms is not None and abs(info.duration_ms - exp) <= 80
    checks.append(f"duration {info.duration_ms} ms vs planned {exp} ms: {'ok' if dur_ok else 'MISMATCH'}")
    ok &= dur_ok
    fidelity = audio_fidelity(original_path, candidate_path, candidate_plan)
    if fidelity is None:
        checks.append("audio fidelity not measured (no audio in the current version)")
    else:
        good = fidelity >= 0.8
        checks.append(f"rendered loudness follows the planned audio: correlation {fidelity:.2f} (threshold 0.80): {'ok' if good else 'AUDIO DOES NOT MATCH THE PLAN'}")
        ok &= good
    needs_text = mutation_type in ("REMOVE_BEAT", "MOVE_BEAT_EARLIER", "MOVE_BEAT_LATER", "TRIM_PAUSE")
    if needs_text and not removed_or_moved_text:
        checks.append("no sentence text available to verify the edit in the speech: NOT VERIFIED")
        ok = False
    if needs_text and removed_or_moved_text:
        cand_words = words_from_transcript(transcribe(candidate_path))
        if not cand_words:
            checks.append("the candidate has no transcribed speech; the edit cannot be verified from speech: NOT VERIFIED")
            ok = False
        elif mutation_type == "REMOVE_BEAT":
            orig_words = words_from_transcript(transcribe(original_path))
            src = moved_source or target
            elsewhere = [w for w in orig_words if w.end_ms <= src[0] or w.start_ms >= src[1]]
            base = sentence_presence(elsewhere, removed_or_moved_text)
            present = sentence_presence(cand_words, removed_or_moved_text)
            good = present - base < 0.34
            checks.append(f"removed sentence's word pairs heard in the candidate: {present:.0%} (the rest of the original already contains {base:.0%}): "
                          f"{'ok, removed' if good else 'NOT REMOVED'}")
            ok &= good
        else:
            src = moved_source or target
            pieces = map_interval(candidate_plan, src[0], src[1])
            if not pieces:
                checks.append("the sentence's footage is not in the candidate plan: NOT VERIFIED")
                ok = False
            else:
                ns, ne = min(p[0] for p in pieces), max(p[1] for p in pieces)
                here = sentence_presence(cand_words, removed_or_moved_text, ns, ne)
                good = here >= 0.6
                checks.append(f"sentence heard at its planned position {ns / 1000:.2f}-{ne / 1000:.2f}s: {here:.0%} of its word pairs ({'ok' if good else 'MISSING'})")
                ok &= good
                if mutation_type.startswith("MOVE_BEAT"):
                    moved = abs(ns - src[0]) >= 300
                    checks.append(f"planned start moved from {src[0] / 1000:.2f}s to {ns / 1000:.2f}s: {'ok' if moved else 'NOT MOVED'}")
                    ok &= moved
                if mutation_type == "TRIM_PAUSE":
                    removed = removed_source_ranges(original_plan, candidate_plan)
                    if not removed:
                        checks.append("no source time was removed: NOT TRIMMED")
                        ok = False
                    else:
                        r0, r1 = max(removed, key=lambda r: r[1] - r[0])
                        orig_q = _longest_quiet_ms(original_path, r0 - 200, r1 + 200)
                        junction = map_interval(candidate_plan, max(0, r0 - 40), r0)
                        jt = junction[0][1] if junction else ns
                        cand_q = _longest_quiet_ms(candidate_path, jt - 200 - (r1 - r0) // 2, jt + 200 + (r1 - r0) // 2)
                        good = cand_q <= orig_q - 150
                        checks.append(f"longest quiet stretch at the cut: {orig_q} ms in the original, {cand_q} ms in the candidate ({'ok, shorter' if good else 'NOT SHORTER'})")
                        ok &= good
    if mutation_type == "PUNCH_IN":
        seg = next((s for s in candidate_plan.segments if s.crop is not None), None)
        mid_src = (seg.source_in_ms + seg.source_out_ms) // 2 if seg is not None else (target[0] + target[1]) // 2
        mid_new = map_interval(candidate_plan, mid_src, mid_src + 40)
        if not mid_new or seg is None:
            checks.append("no cropped segment covers the target in the plan: NOT VERIFIED")
            ok = False
        else:
            o_info = inspect_media(original_path)
            of, cfr = extract_frame(original_path, mid_src, 384), extract_frame(candidate_path, mid_new[0][0], 384)
            plain, zoom = _zoom_consistency(of, cfr, seg.crop, int(o_info.width or of.width), int(o_info.height or of.height))
            good = plain > 0.03 and zoom < plain * 0.8
            checks.append(f"frame at {mid_src / 1000:.2f}s: difference to the original {plain:.3f}, to the original cropped as planned {zoom:.3f} "
                          f"({'ok, reframed as planned' if good else 'NOT THE PLANNED REFRAME'})")
            ok &= good
    return ChangeVerification(intended=description, verified=bool(ok), checks=checks)


PAIR_SCHEMA = {"type": "object", "properties": {"choice": {"type": "string", "enum": ["1", "2", "no_preference"]}, "reason": {"type": "string"}}, "required": ["choice", "reason"]}


def _clip_media(path: Path, start_ms: int, end_ms: int, fps: float, width: int, words_text: str) -> ProbeMedia:
    step = 1000.0 / fps
    times = [int(start_ms + step / 2 + i * step) for i in range(max(1, int((end_ms - start_ms) / step)))][:10]
    frames = [extract_frame(path, t, width) for t in times]
    return ProbeMedia(kind="frames", duration_ms=end_ms - start_ms, frames=frames, transcript=words_text)


def _pair(a: ProbeMedia, b: ProbeMedia) -> ProbeMedia:
    frames = [SampledFrame(timestamp_ms=f.timestamp_ms, jpeg=f.jpeg, width=f.width, height=f.height) for f in a.frames]
    frames += [SampledFrame(timestamp_ms=100000 + f.timestamp_ms, jpeg=f.jpeg, width=f.width, height=f.height) for f in b.frames]
    return ProbeMedia(kind="frames", duration_ms=0, frames=frames, transcript=f"Version 1 words: {a.transcript or '(none)'}\nVersion 2 words: {b.transcript or '(none)'}")


@traced("pairwise_compare", kind="llm")
def pairwise(provider: MediaProbeProvider, original: ProbeMedia, candidate: ProbeMedia, question: str, repeats: int = 2) -> tuple[float | None, StabilityRecord, list[str]]:
    set_display_name("pairwise_" + question[:24].replace(" ", "_"))
    instruction = ("Frames with timestamps under 100 s belong to Version 1; frames at 100 s or later belong to Version 2 (subtract 100 s). "
                   f"{question} Answer '1', '2' or 'no_preference' with one short, blunt reason naming what is worse in the other version.")
    jobs = []
    for r in range(repeats):
        jobs.append((r, False, _pair(original, candidate)))  # candidate is version 2
        jobs.append((r, True, _pair(candidate, original)))  # candidate is version 1

    def one(job: tuple[int, bool, ProbeMedia]) -> tuple[int, bool, str | None, str]:
        r, cand_first, media = job
        try:
            d = provider.judge_json(media, instruction, PAIR_SCHEMA).data
        except ProviderError as exc:
            return r, cand_first, None, str(exc)[:80]
        c = d.get("choice")
        if c == "no_preference":
            return r, cand_first, "tie", str(d.get("reason", ""))[:160]
        if c not in ("1", "2"):
            return r, cand_first, None, "invalid"
        return r, cand_first, ("candidate" if (c == "1") == cand_first else "original"), str(d.get("reason", ""))[:160]

    with weave.ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        results = list(ex.map(one, jobs))
    score, valid, reasons = 0.0, 0, []
    by_repeat: dict[int, list[str]] = {}
    for r, _, outcome, reason in results:
        if outcome is None:
            continue
        valid += 1
        score += 1.0 if outcome == "candidate" else 0.5 if outcome == "tie" else 0.0
        by_repeat.setdefault(r, []).append(outcome)
        reasons.append(f"{outcome}: {reason}")
    flips = sum(1 for outs in by_repeat.values() if len(outs) == 2 and outs[0] != outs[1])
    agreeing = sum(1 for outs in by_repeat.values() if len(outs) == 2 and outs[0] == outs[1])
    stability = StabilityRecord(runs=valid, agreeing_runs=agreeing, order_flips=flips,
                                agreement=f"{agreeing} of {len(by_repeat)} order-swapped pairs agreed" if by_repeat else "no valid comparisons")
    return (score / valid if valid else None), stability, reasons


PROTECT_SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
    "item": {"type": "string"}, "kind": {"type": "string", "enum": ["content", "rule"]},
    "status": {"type": "string", "enum": ["kept", "lost", "respected", "violated", "unclear"]}, "evidence": {"type": "string"}},
    "required": ["item", "kind", "status", "evidence"]}}}, "required": ["items"]}

PROTECT_INSTRUCTION = (
    "Frames with timestamps under 100 s belong to Version 1 (the current video); frames at 100 s or later belong to Version 2 (an edited "
    "candidate; subtract 100 s). For each item below, first decide its kind: 'content' is something in the video that must remain (a moment, "
    "a line, a visual); 'rule' is an instruction the edit must respect (for example a prohibition). For content, status is 'kept' if Version 2 "
    "still contains it intact, 'lost' if it is missing or damaged, 'unclear' if these frames and words cannot tell. For a rule, status is "
    "'respected' if Version 2 does not break it compared with Version 1, 'violated' if it does, 'unclear' if you cannot tell. A rule is never "
    "'lost' just because the rule itself is not shown on screen. Give the evidence you used.\n"
)


@traced("check_protected_content", kind="llm")
def check_protected(provider: MediaProbeProvider, original: ProbeMedia, candidate: ProbeMedia, items: list[str]) -> list[dict[str, str]]:
    """Content that must remain and rules the edit must respect, judged on both versions side by side."""
    if not items:
        return []
    media = _pair(original, candidate)
    try:
        d = provider.judge_json(media, PROTECT_INSTRUCTION + "\n".join(f"- {i}" for i in items), PROTECT_SCHEMA).data
    except ProviderError as exc:
        return [{"item": i, "kind": "unknown", "status": "unclear", "evidence": f"check failed: {str(exc)[:60]}"} for i in items]
    return [x for x in d.get("items", []) if isinstance(x, dict)]


def planned_words(plan: EditPlan, words: list[Any], start_ms: int = 0, end_ms: int | None = None) -> list[Any]:
    """The current version's timed words as they occur in an edited plan (candidate timeline), restricted to source [start, end].

    Route A edits only cut, move, trim or reframe existing footage and sound, so the candidate's speech is exactly the source speech
    mapped through the plan. Using this for both sides of a comparison keeps separate ASR runs from inventing differences; the render's
    audio fidelity is checked mechanically in verify_change."""
    from .review import Word

    out: list[Any] = []
    t = 0
    hi = end_ms if end_ms is not None else 10**9
    for seg in plan.segments:
        a, b = max(start_ms, seg.source_in_ms), min(hi, seg.source_out_ms)
        if b > a:
            for w in words:
                mid = (w.start_ms + w.end_ms) // 2
                if a <= mid < b:
                    out.append(Word(w.text, t + (w.start_ms - seg.source_in_ms), t + (w.end_ms - seg.source_in_ms)))
        t += seg.duration_ms
    return out


def _overlaps(a0: int, a1: int, b0: int, b1: int, min_frac: float = 0.3) -> bool:
    inter = max(0, min(a1, b1) - max(a0, b0))
    return inter >= min_frac * max(1, min(a1 - a0, b1 - b0))


@traced("compare_versions", kind="agent")
def compare_versions(*, provider: MediaProbeProvider, original: AuditReport, candidate: AuditReport, finding: AuditFinding, candidate_plan: EditPlan,
                     protected_items: list[str]) -> AuditComparison:
    notes: list[str] = []
    orig_path, cand_path = Path(original.artifact_path), Path(candidate.artifact_path)
    pieces = map_interval(candidate_plan, finding.start_ms, finding.end_ms)
    target_pref = None
    stab_target = None
    target_ev: list[str] = []
    from .review import words_from_transcript

    o_words_all = words_from_transcript(transcribe(orig_path))
    c_words_all = planned_words(candidate_plan, o_words_all)
    notes.append("candidate words are the current version's timed words mapped through the verified edit plan, so separate transcriptions cannot invent differences")

    def text(words: list) -> str:
        return " ".join(w.text for w in words)

    if pieces:
        ns, ne = min(p[0] for p in pieces), max(p[1] for p in pieces)
        om = _clip_media(orig_path, finding.start_ms, finding.end_ms, 3, 384, text([w for w in o_words_all if finding.start_ms <= (w.start_ms + w.end_ms) // 2 < finding.end_ms]))
        cm = _clip_media(cand_path, ns, ne, 3, 384, text(planned_words(candidate_plan, o_words_all, finding.start_ms, finding.end_ms)))
        target_pref, stab_target, reasons = pairwise(provider, om, cm, "These are two versions of the same moment of a short video. Which version makes this moment clearer and more engaging for someone seeing the video for the first time?")
        target_ev.append(f"target moment preference for the candidate: {target_pref if target_pref is None else round(target_pref, 2)} ({stab_target.agreement})")
        target_ev += reasons[:4]
    else:
        target_ev.append("the target interval no longer exists in the candidate (removed)")
    o_full = ProbeMedia(kind="frames", duration_ms=original.duration_ms, frames=sample_frames(orig_path, original.duration_ms, count=8, max_width=384), transcript=text(o_words_all))
    c_full = ProbeMedia(kind="frames", duration_ms=candidate.duration_ms, frames=sample_frames(cand_path, candidate.duration_ms, count=8, max_width=384), transcript=text(c_words_all))
    full_pref, stab_full, full_reasons = pairwise(provider, o_full, c_full, "These are two versions of the same short video. Which version would a typical viewer be more likely to watch to the end and understand?")

    # audit diff: does a matching weakness persist at the mapped interval? what is new?
    persists = False
    if pieces:
        ns, ne = min(p[0] for p in pieces), max(p[1] for p in pieces)
        for cf_ in candidate.findings:
            if _overlaps(cf_.start_ms, cf_.end_ms, ns, ne) and (cf_.issue_type == finding.issue_type or cf_.objective == finding.objective):
                persists = True
                target_ev.append(f"fresh audit still flags {cf_.start_ms / 1000:.1f}-{cf_.end_ms / 1000:.1f}s: {cf_.weakness[:140]}")
    mapped_orig: list[tuple[int, int]] = []  # where the current version's findings sit in the candidate (removed ones have no place)
    for f in original.findings:
        f_pieces = map_interval(candidate_plan, f.start_ms, f.end_ms)
        if f_pieces:
            mapped_orig.append((min(q[0] for q in f_pieces), max(q[1] for q in f_pieces)))
    new_weak = [f"{c.start_ms / 1000:.1f}-{c.end_ms / 1000:.1f}s {c.issue_type} ({c.severity}): {c.weakness[:160]}" for c in candidate.findings
                if c.severity in ("medium", "high") and not any(_overlaps(c.start_ms, c.end_ms, a, b) for a, b in mapped_orig)]

    improved, regressed, unchanged = [], [], []
    for key, ov in original.audience.model_dump().items():
        if not isinstance(ov, dict):
            continue
        cv = getattr(candidate.audience, key).model_dump()
        o_r, c_r = VERDICT_RANK.get(ov["verdict"]), VERDICT_RANK.get(cv["verdict"])
        if o_r is None or c_r is None:
            unchanged.append(f"{key}: {ov['verdict']} -> {cv['verdict']} (not comparable)")
        elif c_r > o_r:
            improved.append(f"predicted {key}: {ov['verdict']} -> {cv['verdict']}")
        elif c_r < o_r:
            regressed.append(f"predicted {key}: {ov['verdict']} -> {cv['verdict']} ({cv['reason'][:100]})")
        else:
            unchanged.append(f"{key}: {ov['verdict']}")
    o_prot = ProbeMedia(kind="frames", duration_ms=original.duration_ms, frames=sample_frames(orig_path, original.duration_ms, count=8, max_width=320), transcript=text(o_words_all))
    c_prot = ProbeMedia(kind="frames", duration_ms=candidate.duration_ms, frames=sample_frames(cand_path, candidate.duration_ms, count=8, max_width=320), transcript=text(c_words_all))
    for item in check_protected(provider, o_prot, c_prot, protected_items):
        status = item.get("status")
        if status == "lost":
            regressed.append(f"protected content lost: {item.get('item')} ({str(item.get('evidence', ''))[:120]})")
        elif status == "violated":
            regressed.append(f"constraint violated: {item.get('item')} ({str(item.get('evidence', ''))[:120]})")
        elif status == "unclear":
            notes.append(f"protected item unclear ({item.get('kind')}): {item.get('item')}: {str(item.get('evidence', ''))[:120]}")
        else:
            unchanged.append(f"{item.get('kind')} {status}: {item.get('item')}")
    if new_weak:
        regressed += [f"new weakness: {w}" for w in new_weak]
    if full_pref is not None and full_pref <= 0.25:
        regressed.append(f"whole video: the original was preferred ({full_pref:.2f} for the candidate; {stab_full.agreement})")
    elif full_pref is not None and full_pref >= 0.75:
        improved.append(f"whole video: the candidate was preferred ({full_pref:.2f}; {stab_full.agreement})")

    if not pieces:
        resolved = "yes" if not persists else "no"
    elif persists and (target_pref is None or target_pref <= 0.5):
        resolved = "no"
    elif not persists and target_pref is not None and target_pref >= 0.625:
        resolved = "yes"
        improved.append(f"target moment {finding.start_ms / 1000:.1f}-{finding.end_ms / 1000:.1f}s: preferred in the candidate ({target_pref:.2f})")
    else:
        resolved = "unclear"
    unstable = full_pref is None or (stab_full.order_flips > 0 and stab_full.agreeing_runs == 0)
    if unstable:
        outcome = "insufficient_evidence"
        notes.append("the whole-video comparison changed with presentation order or failed; the result is not stable")
    elif resolved == "yes" and not regressed:
        outcome = "improvement"
    elif resolved == "yes" and regressed:
        outcome = "mixed"
    elif resolved == "no" and regressed:
        outcome = "regression"
    elif resolved != "yes" and improved and not regressed:
        outcome = "mixed" if resolved == "no" else "tie"
    elif regressed:
        outcome = "regression"
    else:
        outcome = "tie"
    stability = StabilityRecord(runs=(stab_target.runs if stab_target else 0) + stab_full.runs, agreeing_runs=(stab_target.agreeing_runs if stab_target else 0) + stab_full.agreeing_runs,
                                order_flips=(stab_target.order_flips if stab_target else 0) + stab_full.order_flips,
                                agreement=f"target: {stab_target.agreement if stab_target else 'n/a'}; whole video: {stab_full.agreement}")
    notes += [f"whole-video reasons: {r}" for r in full_reasons[:4]]
    return AuditComparison(improved=improved, regressed=regressed, unchanged=unchanged, target_resolved=resolved, target_evidence=target_ev, new_weaknesses=new_weak,
                           outcome=outcome, target_preference=target_pref, full_preference=full_pref, stability=stability, notes=notes)


def new_repair_id() -> str:
    return new_id("repair")


def now() -> str:
    return utc_now_iso()


def elapsed(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)
