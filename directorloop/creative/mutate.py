"""Controlled creative mutations on real videos.

A finished short becomes an identity EditPlan with one segment per sentence beat (audio kept,
so speech, music and burned-in captions travel with their beat). Each operator changes ONE
variable (the position of one beat, the presence of one beat, the framing of one span, the
silence at one beat edge) and returns typed ops that the existing validator and FFmpeg
renderer execute. Operators return None when their precondition does not hold.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ..domain.assets import AssetKind, AssetManifest, AssetOrigin, AssetRecord
from ..domain.brief import (
    Budget,
    ConstraintKind,
    CreativeBrief,
    GoalProfile,
    ProtectedConstraint,
    RepairAction,
)
from ..domain.creative import (
    BeatRole,
    CreativeGenome,
    CreativeHypothesis,
    CreativeMutation,
    MutationType,
    TimelineBeat,
)
from ..domain.edit_plan import (
    CropRect,
    EditOp,
    EditPlan,
    InsertSegment,
    MoveSegment,
    OutputProfile,
    RemoveSegment,
    Segment,
    SetCrop,
    TrimSegment,
    apply_ops,
)
from ..domain.ids import new_id, utc_now_iso
from ..media.probe import inspect_media
from ..media.validate import validate_ops_scope, validate_plan
from .signals import audio_rms, silence_gaps

VIDEO_ASSET_ID = "asset_source"
JOIN_FADE_MS = 12


def source_manifest(project_id: str, video_path: Path, artifact_hash: str, label: str = "original video") -> AssetManifest:
    info = inspect_media(video_path)
    rec = AssetRecord(
        id=VIDEO_ASSET_ID,
        project_id=project_id,
        kind=AssetKind.VIDEO,
        origin=AssetOrigin.UPLOADED,
        content_hash=artifact_hash,
        storage_key=video_path.name,
        label=label,
        duration_ms=info.duration_ms,
        stream=info,
        rights_note="owned by the creator; analyzed with permission",
        created_at=utc_now_iso(),
    )
    return AssetManifest(project_id=project_id, assets=[rec])


def experiment_brief(project_id: str, genome: CreativeGenome, objective: str) -> CreativeBrief:
    """Creative-experiment brief: edits may reorder, remove, trim or reframe beats; nothing new is added."""
    return CreativeBrief(
        id=f"brief_{project_id}",
        project_id=project_id,
        profile=GoalProfile.EDUCATIONAL,
        objective=objective or "Keep people watching while the core message still comes through.",
        target_duration_ms_min=max(3000, int(genome.duration_ms * 0.5)),
        target_duration_ms_max=genome.duration_ms + 250,
        protected_constraints=[
            ProtectedConstraint(id="pc_no_new_text", kind=ConstraintKind.NO_NEW_TEXT, reason="experiments change structure, not wording"),
            ProtectedConstraint(id="pc_no_generation", kind=ConstraintKind.NO_GENERATION, reason="deterministic edit experiments only"),
            ProtectedConstraint(id="pc_max_duration", kind=ConstraintKind.MAX_DURATION_MS, value_ms=genome.duration_ms + 250, reason="never longer than the original"),
        ],
        allowed_actions=[RepairAction.TRIM_OR_RETIME, RepairAction.REORDER_SEGMENTS, RepairAction.CROP_EXISTING_SHOT, RepairAction.REPLACE_WITH_EXISTING_ASSET],
        generation_permitted=False,
        narration_change_permitted=False,
        budget=Budget(max_usd=1.0, max_llm_calls=80),
        review_status="approved",
        approved_by="experiment designer defaults",
    )


def identity_plan(genome: CreativeGenome, output: OutputProfile | None = None) -> EditPlan:
    return EditPlan(
        output=output or OutputProfile(width=720, height=1280, fps_num=30, fps_den=1),
        segments=[
            Segment(id=b.id, asset_id=VIDEO_ASSET_ID, source_in_ms=b.start_ms, source_out_ms=b.end_ms, fit="cover", audio_policy="keep", label=f"{b.role.value}: {b.text[:40]}")
            for b in genome.beats
        ],
        audio_join_fade_ms=JOIN_FADE_MS,
        change_rationale="identity: the original video re-rendered through the same pipeline as every variant",
    )


def _quote(b: TimelineBeat, n: int = 48) -> str:
    t = b.text.strip()
    return f"'{t[:n]}{'...' if len(t) > n else ''}'"


def _first(genome: CreativeGenome, *roles: BeatRole) -> TimelineBeat | None:
    return next((b for b in genome.beats if b.role in roles), None)


def _move(beat: TimelineBeat, after: str | None) -> list[EditOp]:
    return [MoveSegment(segment_id=beat.id, after_segment_id=after)]


def op_proof_earlier(genome: CreativeGenome, plan: EditPlan) -> tuple[list[EditOp], str, str, int, int] | None:
    proof = _first(genome, BeatRole.PROOF)
    if proof is None or len(plan.segments) < 3:
        return None
    idx = plan.segment_index(proof.id)
    if idx <= 1:
        return None
    hook = plan.segments[0].id
    return _move(proof, hook), f"Move the proof beat {_quote(proof)} from {proof.start_ms / 1000:.1f}s to right after the opening.", "position of the proof beat", proof.start_ms, proof.end_ms


def op_payoff_earlier(genome: CreativeGenome, plan: EditPlan) -> tuple[list[EditOp], str, str, int, int] | None:
    payoff = _first(genome, BeatRole.PAYOFF)
    if payoff is None or len(plan.segments) < 3:
        return None
    idx = plan.segment_index(payoff.id)
    if idx <= 1:
        return None
    return _move(payoff, plan.segments[0].id), f"Move the payoff beat {_quote(payoff)} from {payoff.start_ms / 1000:.1f}s to right after the opening.", "position of the payoff beat", payoff.start_ms, payoff.end_ms


def op_result_first(genome: CreativeGenome, plan: EditPlan) -> tuple[list[EditOp], str, str, int, int] | None:
    result = _first(genome, BeatRole.PAYOFF) or _first(genome, BeatRole.PROOF)
    if result is None or plan.segment_index(result.id) == 0:
        return None
    return _move(result, None), f"Open with the result beat {_quote(result)} (was at {result.start_ms / 1000:.1f}s).", "which beat opens the video", result.start_ms, result.end_ms


def op_context_compression(genome: CreativeGenome, plan: EditPlan) -> tuple[list[EditOp], str, str, int, int] | None:
    anchor_beat = _first(genome, BeatRole.PROOF, BeatRole.PAYOFF)
    anchor = anchor_beat.start_ms if anchor_beat else genome.duration_ms
    candidates = [b for b in genome.beats if b.role in (BeatRole.CONTEXT, BeatRole.SETUP) and b.start_ms < anchor and b.index > 0]
    if not candidates or len(plan.segments) < 3:
        return None
    target = max(candidates, key=lambda b: b.duration_ms)
    return [RemoveSegment(segment_id=target.id)], f"Remove the {target.role.value} beat {_quote(target)} ({target.duration_ms / 1000:.1f}s) before the proof.", "presence of one context beat", target.start_ms, target.end_ms


def op_remove_redundant(genome: CreativeGenome, plan: EditPlan) -> tuple[list[EditOp], str, str, int, int] | None:
    red = next((b for b in genome.beats if b.redundant_with_beat_id and b.index > 0), None)
    if red is None or len(plan.segments) < 3:
        return None
    return [RemoveSegment(segment_id=red.id)], f"Remove {_quote(red)}, which repeats {red.redundant_with_beat_id}.", "presence of one repeated beat", red.start_ms, red.end_ms


def op_pattern_interrupt(genome: CreativeGenome, plan: EditPlan, manifest: AssetManifest) -> tuple[list[EditOp], str, str, int, int] | None:
    span_ms = genome.longest_static_span_ms.value
    start = genome.longest_static_span_start_ms.value
    if not isinstance(span_ms, int) or not isinstance(start, int) or span_ms < 2500:
        return None
    span_end = start + span_ms
    overlaps = [(min(b.end_ms, span_end) - max(b.start_ms, start), b) for b in genome.beats]
    overlap, beat = max(overlaps, key=lambda o: o[0])
    if overlap < 1400:
        return None
    mid = (max(beat.start_ms, start) + min(beat.end_ms, span_end)) // 2  # middle of the static part of this beat
    mid = min(max(mid, beat.start_ms + 700), beat.end_ms - 700)
    rec = manifest.get(VIDEO_ASSET_ID)
    if not rec.stream or not rec.stream.width or not rec.stream.height:
        return None
    w, h = rec.stream.width, rec.stream.height
    cw, ch = int(w / 1.25) // 2 * 2, int(h / 1.25) // 2 * 2
    crop = CropRect(x=(w - cw) // 2 // 2 * 2, y=(h - ch) // 2 // 2 * 2, w=cw, h=ch)
    tail_id = f"{beat.id}_punch"
    ops: list[EditOp] = [
        TrimSegment(segment_id=beat.id, source_in_ms=beat.start_ms, source_out_ms=mid),
        InsertSegment(after_segment_id=beat.id, segment=Segment(id=tail_id, asset_id=VIDEO_ASSET_ID, source_in_ms=mid, source_out_ms=beat.end_ms, fit="cover", audio_policy="keep", label="punch-in")),
        SetCrop(segment_id=tail_id, crop=crop),
    ]
    return ops, f"Punch in 1.25x at {mid / 1000:.1f}s inside the {span_ms / 1000:.1f}s static span (same footage and audio, new framing).", "framing during the longest static span", start, start + span_ms


def op_trim_dead_air(genome: CreativeGenome, plan: EditPlan, video_path: Path) -> tuple[list[EditOp], str, str, int, int] | None:
    rms = audio_rms(video_path)
    best: tuple[int, TimelineBeat, int, int] | None = None
    for b in genome.beats:
        for g0, g1 in silence_gaps(rms, b.start_ms, b.end_ms, min_gap_ms=450):
            if g0 <= b.start_ms + 60:
                new_in, new_out, saved = g1 - 80, b.end_ms, g1 - 80 - b.start_ms
            elif g1 >= b.end_ms - 60:
                new_in, new_out, saved = b.start_ms, g0 + 80, b.end_ms - (g0 + 80)
            else:
                continue
            if saved >= 300 and new_out - new_in >= 600 and (best is None or saved > best[0]):
                best = (saved, b, new_in, new_out)
    if best is None:
        return None
    saved, b, new_in, new_out = best
    return [TrimSegment(segment_id=b.id, source_in_ms=new_in, source_out_ms=new_out)], f"Trim {saved / 1000:.2f}s of silence at the edge of {_quote(b)}.", "silence at one beat edge", b.start_ms, b.end_ms


def build_mutation(
    mtype: MutationType,
    hypothesis: CreativeHypothesis,
    genome: CreativeGenome,
    plan: EditPlan,
    manifest: AssetManifest,
    brief: CreativeBrief,
    parent_version_id: str,
    video_path: Path,
) -> CreativeMutation | None:
    builders: dict[MutationType, Callable[[], tuple[list[EditOp], str, str, int, int] | None]] = {
        MutationType.PROOF_EARLIER: lambda: op_proof_earlier(genome, plan),
        MutationType.PAYOFF_EARLIER: lambda: op_payoff_earlier(genome, plan),
        MutationType.RESULT_FIRST: lambda: op_result_first(genome, plan),
        MutationType.CONTEXT_COMPRESSION: lambda: op_context_compression(genome, plan),
        MutationType.REMOVE_REDUNDANT_BEAT: lambda: op_remove_redundant(genome, plan),
        MutationType.PATTERN_INTERRUPT: lambda: op_pattern_interrupt(genome, plan, manifest),
        MutationType.SHORTEN_SHOT: lambda: op_trim_dead_air(genome, plan, video_path),
    }
    builder = builders.get(mtype)
    if builder is None:
        return None
    built = builder()
    if built is None:
        return None
    ops, description, variable, t0, t1 = built
    if validate_ops_scope(ops, brief):
        return None
    new_plan = apply_ops(plan, ops)
    vr = validate_plan(new_plan, manifest, brief, baseline=plan)
    if not vr.ok:
        return None
    protected = ["spoken wording", "music and voice inside each beat", "burned-in captions", "every other beat's order and length"]
    return CreativeMutation(
        id=new_id("mut"),
        type=mtype,
        hypothesis_id=hypothesis.id,
        parent_version_id=parent_version_id,
        target_start_ms=t0,
        target_end_ms=t1,
        changed_variable=variable,
        ops=ops,
        protected_variables=protected,
        description=description,
        expected_benefit=f"prediction: addresses {hypothesis.family.value} ({hypothesis.statement[:90]})",
        risk={"RESULT_FIRST": "medium", "PAYOFF_EARLIER": "medium", "CONTEXT_COMPRESSION": "medium"}.get(mtype.value, "low"),
        cost_usd=0.0,
        latency_estimate_ms=1500,
        evidence_refs=[e.ref for e in hypothesis.evidence if e.ref],
        diff_lines=[description],
    )
