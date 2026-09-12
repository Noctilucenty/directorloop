"""Executable C options built from two edits of the same idea, and verification that a rendered C is what was planned.

Options (all typed, validated plans over two authorized source files; nothing is generated):
- swap_unit: replace one aligned story unit of the base with the other edit's version of it (its shots, narration take and
  burned-in captions travel together, so the change is a bundle and is recorded as one).
- insert_unit: add a unit that only the other edit contains, between the aligned neighbours.
- remove_unit: drop a unit that only the base contains.
- repair: a single-video edit of the base aimed at one of its own audit findings (cut, move, trim pause, punch-in).
Joins fall on sentence boundaries snapped to quiet points. Loudness mismatches, wording changes and caption-style risks are
listed as side effects rather than hidden.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from ..audit.models import AuditFinding, AuditReport, ChangeVerification
from ..audit.repairs import build_candidates
from ..audit.revise import sentence_presence
from ..creative.genome import CreativeGenome
from ..creative.signals import RMS_HOP_MS, audio_rms
from ..domain.assets import AssetKind, AssetManifest, AssetOrigin, AssetRecord
from ..domain.brief import (
    Budget,
    ConstraintKind,
    CreativeBrief,
    GoalProfile,
    ProtectedConstraint,
    RepairAction,
)
from ..domain.edit_plan import EditPlan, OutputProfile, Segment
from ..domain.ids import utc_now_iso
from ..media.frames import extract_frame
from ..media.probe import inspect_media
from ..media.validate import validate_plan
from ..observability.weave_ops import traced
from .models import AlignedUnit, COption

ASSET_ID = {"A": "asset_a", "B": "asset_b"}
JOIN_FADE_MS = 12
MAX_REPAIR_OPTIONS_PER_BASE = 6


@dataclass
class VersionMaterial:
    label: str  # "A" or "B"
    path: Path  # the evaluated file
    artifact_hash: str
    genome: CreativeGenome
    words: list[Any]  # timed words (review.Word) of this file's speech
    audit: AuditReport | None
    width: int
    height: int
    duration_ms: int
    rms: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))


def other(label: str) -> str:
    return "B" if label == "A" else "A"


def two_asset_manifest(project_id: str, a: VersionMaterial, b: VersionMaterial) -> AssetManifest:
    recs = []
    for m in (a, b):
        info = inspect_media(m.path)
        recs.append(AssetRecord(id=ASSET_ID[m.label], project_id=project_id, kind=AssetKind.VIDEO, origin=AssetOrigin.UPLOADED, content_hash=m.artifact_hash,
                                storage_key=m.path.name, label=f"edit {m.label}", duration_ms=info.duration_ms, stream=info,
                                rights_note="authorized alternative edit supplied by the owner", created_at=utc_now_iso()))
    return AssetManifest(project_id=project_id, assets=recs)


def c_brief(project_id: str, a: VersionMaterial, b: VersionMaterial, objective: str) -> CreativeBrief:
    longest = max(a.duration_ms, b.duration_ms)
    return CreativeBrief(
        id=f"brief_{project_id}_c", project_id=project_id, profile=GoalProfile.EDUCATIONAL, objective=objective,
        target_duration_ms_min=max(2000, int(min(a.duration_ms, b.duration_ms) * 0.5)), target_duration_ms_max=longest + 250,
        protected_constraints=[
            ProtectedConstraint(id="pc_no_new_text", kind=ConstraintKind.NO_NEW_TEXT, reason="C reuses existing footage, narration and captions only"),
            ProtectedConstraint(id="pc_no_generation", kind=ConstraintKind.NO_GENERATION, reason="no generated assets"),
            ProtectedConstraint(id="pc_max_duration", kind=ConstraintKind.MAX_DURATION_MS, value_ms=longest + 250, reason="never longer than the longer input"),
        ],
        allowed_actions=[RepairAction.TRIM_OR_RETIME, RepairAction.REORDER_SEGMENTS, RepairAction.CROP_EXISTING_SHOT, RepairAction.REPLACE_WITH_EXISTING_ASSET],
        generation_permitted=False, narration_change_permitted=False, budget=Budget(max_usd=1.0, max_llm_calls=200), review_status="approved",
        approved_by="A/B-to-C defaults: existing material only",
    )


def output_profile(a: VersionMaterial, b: VersionMaterial) -> OutputProfile:
    w, h = (a.width, a.height) if (a.width, a.height) == (b.width, b.height) else (min(a.width, b.width), min(a.height, b.height))
    return OutputProfile(width=w // 2 * 2, height=h // 2 * 2, fps_num=30, fps_den=1)


def seg_id(label: str, beat_id: str) -> str:
    return f"{label.lower()}_{beat_id}"


def version_plan(m: VersionMaterial, output: OutputProfile) -> EditPlan:
    return EditPlan(output=output, audio_join_fade_ms=JOIN_FADE_MS, change_rationale=f"edit {m.label} re-rendered through the same pipeline as C",
                    segments=[Segment(id=seg_id(m.label, bt.id), asset_id=ASSET_ID[m.label], source_in_ms=bt.start_ms, source_out_ms=bt.end_ms, fit="cover",
                                      audio_policy="keep", label=f"{m.label}: {bt.text[:40]}") for bt in m.genome.beats])


def timeline_spans(plan: EditPlan) -> dict[str, tuple[int, int]]:
    out, t = {}, 0
    for s in plan.segments:
        out[s.id] = (t, t + s.duration_ms)
        t += s.duration_ms
    return out


def _unit_beats(u: AlignedUnit, label: str) -> list[str]:
    return u.a_beats if label == "A" else u.b_beats


def _loudness(m: VersionMaterial, start_ms: int, end_ms: int) -> float:
    if len(m.rms) == 0:
        return 0.0
    seg = m.rms[start_ms // RMS_HOP_MS: end_ms // RMS_HOP_MS]
    return float(np.median(seg)) if len(seg) else 0.0


def _frame_gray(path: Path, t_ms: int) -> np.ndarray:
    fr = extract_frame(path, max(0, t_ms), 256)
    return np.asarray(Image.open(io.BytesIO(fr.jpeg)).convert("L").resize((64, 114))).astype(np.int16)


def _diff(x: np.ndarray, y: np.ndarray) -> float:
    return float(np.abs(x - y).mean() / 255.0)


def _quote(text: str, n: int = 60) -> str:
    t = text.strip()
    return f"'{t[:n]}{'...' if len(t) > n else ''}'"


def build_c_options(materials: dict[str, VersionMaterial], units: list[AlignedUnit], project_id: str, objective: str, exclude: set[str] | None = None) -> tuple[list[tuple[COption, EditPlan]], AssetManifest, OutputProfile]:
    a, b = materials["A"], materials["B"]
    manifest = two_asset_manifest(project_id, a, b)
    brief = c_brief(project_id, a, b, objective)
    out_profile = output_profile(a, b)
    exclude = exclude or set()
    options: list[tuple[COption, EditPlan]] = []
    beat_by_id = {lab: {bt.id: bt for bt in materials[lab].genome.beats} for lab in ("A", "B")}

    def add(opt: COption, plan: EditPlan, base_plan: EditPlan) -> None:
        if opt.key in exclude:
            return
        if not validate_plan(plan, manifest, brief, baseline=base_plan).ok:
            return
        options.append((opt, plan.model_copy(update={"change_rationale": opt.description})))

    for base_label in ("A", "B"):
        donor_label = other(base_label)
        base, donor = materials[base_label], materials[donor_label]
        base_plan = version_plan(base, out_profile)
        base_ids = [s.id for s in base_plan.segments]

        def unit_segment_ids(u: AlignedUnit, label: str) -> list[str]:
            return [seg_id(label, x) for x in _unit_beats(u, label)]

        for u in units:
            xs, ys = unit_segment_ids(u, base_label), unit_segment_ids(u, donor_label)
            prev_ids = next((unit_segment_ids(p, base_label) for p in reversed(units[: u.index]) if _unit_beats(p, base_label)), [])
            next_ids = next((unit_segment_ids(p, base_label) for p in units[u.index + 1:] if _unit_beats(p, base_label)), [])
            donor_segs = [Segment(id=f"c_{sid}", asset_id=ASSET_ID[donor_label], source_in_ms=beat_by_id[donor_label][bid].start_ms,
                                  source_out_ms=beat_by_id[donor_label][bid].end_ms, fit="cover", audio_policy="keep",
                                  label=f"{donor_label}: {beat_by_id[donor_label][bid].text[:40]}") for sid, bid in zip(ys, _unit_beats(u, donor_label), strict=True)]
            base_spans = timeline_spans(base_plan)

            def base_region(ids: list[str], _spans: dict[str, tuple[int, int]] = base_spans) -> tuple[int, int]:
                spans = [_spans[i] for i in ids if i in _spans]
                return (min(s[0] for s in spans), max(s[1] for s in spans)) if spans else (0, 0)

            def cand_region(plan: EditPlan, ids: list[str]) -> tuple[int, int]:
                spans = timeline_spans(plan)
                sel = [spans[i] for i in ids if i in spans]
                return (min(s[0] for s in sel), max(s[1] for s in sel)) if sel else (0, 0)

            if xs and ys:
                x0, x1 = beat_by_id[base_label][_unit_beats(u, base_label)[0]].start_ms, beat_by_id[base_label][_unit_beats(u, base_label)[-1]].end_ms
                y0, y1 = beat_by_id[donor_label][_unit_beats(u, donor_label)[0]].start_ms, beat_by_id[donor_label][_unit_beats(u, donor_label)[-1]].end_ms
                same_words = (u.similarity or 0) >= 0.97
                visual = _diff(_frame_gray(base.path, (x0 + x1) // 2), _frame_gray(donor.path, (y0 + y1) // 2))
                if same_words and visual < 0.03 and abs((x1 - x0) - (y1 - y0)) < 150:
                    continue  # the two edits are materially identical here
                i0, i1 = base_ids.index(xs[0]), base_ids.index(xs[-1]) + 1
                plan = base_plan.model_copy(update={"segments": base_plan.segments[:i0] + donor_segs + base_plan.segments[i1:]})
                side = [f"{donor_label}'s shots, narration take and burned-in captions for this part replace {base_label}'s together"]
                if not same_words:
                    side.append(f"wording changes from {_quote(u.text_a if base_label == 'A' else u.text_b)} to {_quote(u.text_b if base_label == 'A' else u.text_a)}")
                lx, ly = _loudness(base, x0, x1), _loudness(donor, y0, y1)
                if lx > 0 and ly > 0 and not 0.6 <= ly / lx <= 1.6:
                    side.append(f"loudness jump at the joins: {donor_label}'s part is {ly / lx:.1f}x as loud")
                side.append(f"duration changes by {((y1 - y0) - (x1 - x0)) / 1000:+.1f}s")
                if u.index == 0:
                    side.append("changes the opening")
                changed = [s.id for s in donor_segs]
                add(COption(key=f"swap_{base_label.lower()}_u{u.index}", kind="swap_unit", base=base_label, donor=donor_label, unit_index=u.index,  # type: ignore[arg-type]
                            description=f"Start from {base_label}; use {donor_label}'s version of part {u.index + 1} ({y0 / 1000:.1f}-{y1 / 1000:.1f}s in {donor_label}: "
                                        f"{_quote(u.text_b if base_label == 'A' else u.text_a)}) in place of {base_label}'s ({x0 / 1000:.1f}-{x1 / 1000:.1f}s).",
                            changes=[f"part {u.index + 1}: shots, narration take, wording and captions from {donor_label} (one bundled change)"], side_effects=side,
                            duration_ms=plan.timeline_duration_ms(), base_region_ms=base_region(prev_ids + xs + next_ids),
                            candidate_region_ms=cand_region(plan, prev_ids + changed + next_ids)), plan, base_plan)
            elif ys and not xs:
                insert_at = base_ids.index(prev_ids[-1]) + 1 if prev_ids else 0
                plan = base_plan.model_copy(update={"segments": base_plan.segments[:insert_at] + donor_segs + base_plan.segments[insert_at:]})
                y0 = beat_by_id[donor_label][_unit_beats(u, donor_label)[0]].start_ms
                y1 = beat_by_id[donor_label][_unit_beats(u, donor_label)[-1]].end_ms
                side = [f"adds {(y1 - y0) / 1000:.1f}s of {donor_label}'s footage, narration and captions", f"duration grows by {(y1 - y0) / 1000:.1f}s"]
                add(COption(key=f"insert_{base_label.lower()}_u{u.index}", kind="insert_unit", base=base_label, donor=donor_label, unit_index=u.index,  # type: ignore[arg-type]
                            description=f"Start from {base_label}; add {donor_label}'s part {_quote(u.text_b if base_label == 'A' else u.text_a)} "
                                        f"({y0 / 1000:.1f}-{y1 / 1000:.1f}s in {donor_label}), which {base_label} does not have.",
                            changes=[f"insert {donor_label}'s part {u.index + 1}"], side_effects=side, duration_ms=plan.timeline_duration_ms(),
                            base_region_ms=base_region(prev_ids + next_ids), candidate_region_ms=cand_region(plan, prev_ids + [s.id for s in donor_segs] + next_ids)),
                    plan, base_plan)
            elif xs and not ys and len(base_ids) > len(xs):
                plan = base_plan.model_copy(update={"segments": [s for s in base_plan.segments if s.id not in set(xs)]})
                x0 = beat_by_id[base_label][_unit_beats(u, base_label)[0]].start_ms
                x1 = beat_by_id[base_label][_unit_beats(u, base_label)[-1]].end_ms
                add(COption(key=f"remove_{base_label.lower()}_u{u.index}", kind="remove_unit", base=base_label, donor=None, unit_index=u.index,  # type: ignore[arg-type]
                            description=f"Start from {base_label}; remove its part {_quote(u.text_a if base_label == 'A' else u.text_b)} "
                                        f"({x0 / 1000:.1f}-{x1 / 1000:.1f}s), which {donor_label} tells the story without.",
                            changes=[f"remove {base_label}'s part {u.index + 1}"], side_effects=[f"duration shrinks by {(x1 - x0) / 1000:.1f}s", "what that part showed and said is gone"],
                            duration_ms=plan.timeline_duration_ms(), base_region_ms=base_region(prev_ids + xs + next_ids), candidate_region_ms=cand_region(plan, prev_ids + next_ids)),
                    plan, base_plan)

        # single-video repairs of the base aimed at its own audit findings
        count = 0
        for f in (base.audit.findings if base.audit else []):
            if count >= MAX_REPAIR_OPTIONS_PER_BASE:
                break
            cands, _, _, _ = build_candidates(f, base.genome, base.path, project_id)
            for cand in cands:
                if count >= MAX_REPAIR_OPTIONS_PER_BASE:
                    break
                segs = [s.model_copy(update={"id": seg_id(base_label, s.id), "asset_id": ASSET_ID[base_label]}) for s in cand.plan.segments]
                plan = base_plan.model_copy(update={"segments": segs})
                key = f"repair_{base_label.lower()}_{cand.key}"
                region = _finding_region(f, base_plan)
                add(COption(key=key, kind="repair", base=base_label, donor=None, unit_index=None,  # type: ignore[arg-type]
                            description=f"Start from {base_label}; {cand.description} (aimed at {base_label}'s finding at {f.start_ms / 1000:.1f}-{f.end_ms / 1000:.1f}s: {f.weakness[:120]})",
                            changes=[cand.description], side_effects=list(cand.secondary_changes), duration_ms=plan.timeline_duration_ms(),
                            base_region_ms=region, candidate_region_ms=_mapped_region(plan, base_label, region)), plan, base_plan)
                count += 1
    return options, manifest, out_profile


def _finding_region(f: AuditFinding, base_plan: EditPlan) -> tuple[int, int]:
    spans = [(s.source_in_ms, s.source_out_ms) for s in base_plan.segments]
    before = [sp for sp in spans if sp[1] <= f.start_ms]
    after = [sp for sp in spans if sp[0] >= f.end_ms]
    start = before[-1][0] if before else 0
    end = after[0][1] if after else spans[-1][1]
    return start, end


def _mapped_region(plan: EditPlan, label: str, region: tuple[int, int]) -> tuple[int, int]:
    t, lo, hi = 0, None, None
    for s in plan.segments:
        if s.asset_id == ASSET_ID[label]:
            a, b = max(region[0], s.source_in_ms), min(region[1], s.source_out_ms)
            if b > a:
                lo = t + (a - s.source_in_ms) if lo is None else lo
                hi = t + (b - s.source_in_ms)
        t += s.duration_ms
    return (lo or 0, hi or 0)


def planned_words_multi(plan: EditPlan, words_by_asset: dict[str, list[Any]]) -> list[Any]:
    """Words of each source in plan order on the C timeline (source speech travels with its segment)."""
    from ..audit.review import Word

    out, t = [], 0
    for s in plan.segments:
        for w in words_by_asset.get(s.asset_id, []):
            mid = (w.start_ms + w.end_ms) // 2
            if s.source_in_ms <= mid < s.source_out_ms:
                out.append(Word(w.text, t + (w.start_ms - s.source_in_ms), t + (w.end_ms - s.source_in_ms)))
        t += s.duration_ms
    return out


def planned_envelope(plan: EditPlan, rms_by_asset: dict[str, np.ndarray]) -> np.ndarray:
    parts = []
    for s in plan.segments:
        src = rms_by_asset.get(s.asset_id, np.zeros(0, dtype=np.float32))
        a, b = s.source_in_ms // RMS_HOP_MS, s.source_out_ms // RMS_HOP_MS
        piece = src[a:b] if s.audio_policy == "keep" and len(src) else np.zeros(max(0, b - a), dtype=np.float32)
        if len(piece) < b - a:
            piece = np.concatenate([piece, np.zeros(b - a - len(piece), dtype=np.float32)])
        parts.append(piece)
    return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)


@traced("verify_c_render", kind="tool")
def verify_c_render(option: COption, plan: EditPlan, c_path: Path, materials: dict[str, VersionMaterial]) -> ChangeVerification:
    """Did the rendered C implement the planned recombination? Duration, loudness envelope, per-segment picture provenance, and
    the donor's words heard where the plan put them (or the removed words gone)."""
    from ..audit.review import words_from_transcript
    from ..media.transcribe import transcribe

    checks: list[str] = []
    ok = True
    info = inspect_media(c_path)
    exp = plan.timeline_duration_ms()
    dur_ok = info.duration_ms is not None and abs(info.duration_ms - exp) <= 80
    checks.append(f"duration {info.duration_ms} ms vs planned {exp} ms: {'ok' if dur_ok else 'MISMATCH'}")
    ok &= dur_ok
    rms_by_asset = {ASSET_ID[k]: m.rms for k, m in materials.items()}
    planned = planned_envelope(plan, rms_by_asset)
    cand = audio_rms(c_path)
    n = min(len(planned), len(cand))
    if n >= 20 and planned[:n].std() > 1e-9 and cand[:n].std() > 1e-9:
        corr = float(np.corrcoef(planned[:n].astype(np.float64), cand[:n].astype(np.float64))[0, 1])
        good = corr >= 0.8
        checks.append(f"loudness follows the planned sources: correlation {corr:.2f} (threshold 0.80): {'ok' if good else 'AUDIO DOES NOT MATCH THE PLAN'}")
        ok &= good
    else:
        checks.append("loudness envelope not comparable (silent or too short)")
    spans = timeline_spans(plan)
    by_asset = {v: k for k, v in ASSET_ID.items()}
    donor_first = sorted(plan.segments, key=lambda s: (0 if option.donor and s.asset_id == ASSET_ID[option.donor] else 1, spans[s.id][0]))
    for s in donor_first[:4]:
        t0, t1 = spans[s.id]
        src = materials[by_asset[s.asset_id]]
        d = _diff(_frame_gray(c_path, (t0 + t1) // 2), _frame_gray(src.path, (s.source_in_ms + s.source_out_ms) // 2))
        good = d < 0.06
        checks.append(f"picture at {(t0 + t1) / 2000:.2f}s matches {by_asset[s.asset_id]} at {(s.source_in_ms + s.source_out_ms) / 2000:.2f}s: difference {d:.3f} "
                      f"(threshold 0.06): {'ok' if good else 'WRONG SOURCE OR TIME'}")
        ok &= good
    if option.kind in ("swap_unit", "insert_unit") and option.donor:
        donor = materials[option.donor]
        donor_ids = [s for s in plan.segments if s.asset_id == ASSET_ID[option.donor]]
        text = " ".join(w.text for w in donor.words if any(s.source_in_ms <= (w.start_ms + w.end_ms) // 2 < s.source_out_ms for s in donor_ids))
        if text.strip():
            heard = words_from_transcript(transcribe(c_path))
            c0, c1 = min(spans[s.id][0] for s in donor_ids), max(spans[s.id][1] for s in donor_ids)
            pres = sentence_presence(heard, text, c0, c1)
            good = pres >= 0.6
            checks.append(f"{option.donor}'s words heard at {c0 / 1000:.2f}-{c1 / 1000:.2f}s in C: {pres:.0%} of their word pairs ({'ok' if good else 'MISSING'})")
            ok &= good
    return ChangeVerification(intended=option.description, verified=bool(ok), checks=checks)
