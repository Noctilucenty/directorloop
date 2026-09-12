"""Edit plan validation: the gate between a planner proposal and the renderer.

Checks asset ownership, trim bounds, durations, geometry, caption fit, allowed
action scope, protected intervals, and generation authorization.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.assets import AssetKind, AssetManifest, AssetOrigin
from ..domain.brief import ConstraintKind, CreativeBrief, RepairAction
from ..domain.edit_plan import (
    AdjustCaptionTiming,
    EditOp,
    EditPlan,
    InsertGeneratedSegment,
    InsertSegment,
    MoveSegment,
    RemoveCaption,
    RemoveSegment,
    ReplaceSegment,
    SetCaption,
    SetCrop,
    TrimSegment,
)
from .captions import layout_caption


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


OP_TO_ACTION: dict[type, RepairAction] = {
    TrimSegment: RepairAction.TRIM_OR_RETIME,
    MoveSegment: RepairAction.REORDER_SEGMENTS,
    ReplaceSegment: RepairAction.REPLACE_WITH_EXISTING_ASSET,
    InsertSegment: RepairAction.REPLACE_WITH_EXISTING_ASSET,
    RemoveSegment: RepairAction.TRIM_OR_RETIME,
    SetCrop: RepairAction.CROP_EXISTING_SHOT,
    SetCaption: RepairAction.REVISE_CAPTIONS,
    AdjustCaptionTiming: RepairAction.REVISE_CAPTIONS,
    RemoveCaption: RepairAction.REVISE_CAPTIONS,
    InsertGeneratedSegment: RepairAction.GENERATE_MISSING_SHOT,
}


def actions_for_ops(ops: list[EditOp]) -> set[RepairAction]:
    return {OP_TO_ACTION[type(op)] for op in ops}


def validate_ops_scope(ops: list[EditOp], brief: CreativeBrief) -> list[str]:
    errors: list[str] = []
    for action in actions_for_ops(ops):
        if not brief.action_allowed(action):
            errors.append(f"action {action} is not permitted by the brief")
    return errors


def validate_plan(
    plan: EditPlan,
    manifest: AssetManifest,
    brief: CreativeBrief,
    baseline: EditPlan | None = None,
    max_upscale: float = 3.0,
) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []

    if not plan.segments:
        errors.append("plan has no segments")

    seen: set[str] = set()
    for seg in plan.segments:
        if seg.id in seen:
            errors.append(f"duplicate segment id {seg.id}")
        seen.add(seg.id)
        if not manifest.has(seg.asset_id):
            errors.append(f"segment {seg.id} references unauthorized asset {seg.asset_id}")
            continue
        rec = manifest.get(seg.asset_id)
        if rec.kind not in (AssetKind.VIDEO, AssetKind.IMAGE, AssetKind.GRAPHIC):
            errors.append(f"segment {seg.id} uses non-visual asset {seg.asset_id}")
        if rec.duration_ms is not None and seg.source_out_ms > rec.duration_ms + 5:
            errors.append(f"segment {seg.id} trims past the end of {seg.asset_id} ({seg.source_out_ms} > {rec.duration_ms} ms)")
        if seg.speed != 1.0:
            errors.append(f"segment {seg.id}: speed changes are not supported in schema 1")
        if seg.duration_ms < 200:
            errors.append(f"segment {seg.id} is shorter than 200 ms")
        if rec.origin == AssetOrigin.GENERATED:
            if not brief.generation_permitted or brief.has_constraint(ConstraintKind.NO_GENERATION):
                errors.append(f"segment {seg.id} uses generated asset {seg.asset_id} but generation is not permitted")
            if rec.generation is None or rec.generation.status != "completed":
                errors.append(f"generated asset {seg.asset_id} has no completed provenance record")
        if seg.crop is not None and rec.stream and rec.stream.width and rec.stream.height:
            c = seg.crop
            if c.x + c.w > rec.stream.width or c.y + c.h > rec.stream.height:
                errors.append(f"segment {seg.id} crop exceeds the source frame")
            upscale = max(plan.output.width / c.w, plan.output.height / c.h)
            if upscale > max_upscale:
                errors.append(f"segment {seg.id} crop would upscale {upscale:.1f}x (limit {max_upscale}x)")
            elif upscale > 2.0:
                warnings.append(f"segment {seg.id} crop upscales {upscale:.1f}x; softness expected")

    total = plan.timeline_duration_ms()
    if total > brief.max_duration_ms():
        errors.append(f"timeline {total} ms exceeds the brief maximum {brief.max_duration_ms()} ms")
    if total < brief.min_duration_ms():
        errors.append(f"timeline {total} ms is under the brief minimum {brief.min_duration_ms()} ms")

    if plan.narration is not None:
        if not manifest.has(plan.narration.asset_id):
            errors.append("narration references an unauthorized asset")
        else:
            rec = manifest.get(plan.narration.asset_id)
            if rec.kind != AssetKind.AUDIO:
                errors.append("narration asset is not audio")
            elif rec.duration_ms and rec.duration_ms + plan.narration.offset_ms > total + 100:
                errors.append(
                    f"narration ({rec.duration_ms} ms from {plan.narration.offset_ms} ms) runs past the {total} ms timeline; words would be cut"
                )
    if plan.music is not None and not manifest.has(plan.music.asset_id):
        errors.append("music references an unauthorized asset")

    cap_ids: set[str] = set()
    for cap in plan.captions:
        if cap.id in cap_ids:
            errors.append(f"duplicate caption id {cap.id}")
        cap_ids.add(cap.id)
        if any(ch in cap.text for ch in "\x00\r"):
            errors.append(f"caption {cap.id} contains control characters")
        if cap.end_ms > total:
            errors.append(f"caption {cap.id} extends past the end of the video ({cap.end_ms} > {total} ms)")
        layout = layout_caption(cap, plan.output)
        if not layout.fits:
            errors.append(f"caption {cap.id} does not fit the frame ({len(layout.lines)} lines)")
        if cap.duration_ms < 700:
            warnings.append(f"caption {cap.id} shows for only {cap.duration_ms} ms")
        if layout.chars_per_second > 25:
            warnings.append(f"caption {cap.id} reads at {layout.chars_per_second:.0f} chars/s (heuristic threshold 25)")
    for a in plan.captions:
        for b in plan.captions:
            if a.id < b.id and a.position == b.position and a.start_ms < b.end_ms and b.start_ms < a.end_ms:
                errors.append(f"captions {a.id} and {b.id} overlap at the same position")

    if baseline is not None:
        errors += _check_constraints_against_baseline(plan, baseline, brief, manifest)

    return ValidationResult(ok=not errors, errors=errors, warnings=warnings)


def _check_constraints_against_baseline(
    plan: EditPlan, baseline: EditPlan, brief: CreativeBrief, manifest: AssetManifest
) -> list[str]:
    errors: list[str] = []
    for c in brief.protected_constraints:
        if c.kind == ConstraintKind.KEEP_NARRATION and plan.narration != baseline.narration:
            errors.append(f"constraint {c.id}: narration changed")
        if c.kind == ConstraintKind.KEEP_MUSIC and plan.music != baseline.music:
            errors.append(f"constraint {c.id}: music changed")
        if c.kind == ConstraintKind.NO_NEW_TEXT:
            before = {cap.text for cap in baseline.captions}
            for cap in plan.captions:
                if cap.text not in before:
                    errors.append(f"constraint {c.id}: new on-screen text '{cap.text}'")
        if c.kind == ConstraintKind.NO_GENERATION:
            for seg in plan.segments:
                if manifest.has(seg.asset_id) and manifest.get(seg.asset_id).origin == AssetOrigin.GENERATED:
                    errors.append(f"constraint {c.id}: generated asset used in {seg.id}")
        if c.kind in (ConstraintKind.PRESERVE_INTERVAL, ConstraintKind.KEEP_SILENCE_INTERVAL):
            if not _interval_preserved(plan, baseline, c.start_ms or 0, c.end_ms or 0):
                errors.append(f"constraint {c.id}: protected interval {c.start_ms}-{c.end_ms} ms was altered")
    for pi in baseline.protected_intervals:
        if not _interval_preserved(plan, baseline, pi.start_ms, pi.end_ms):
            errors.append(f"protected interval {pi.start_ms}-{pi.end_ms} ms ({pi.reason}) was altered")
    return errors


def _interval_preserved(plan: EditPlan, baseline: EditPlan, start_ms: int, end_ms: int) -> bool:
    """The same footage must occupy the same timeline positions inside the interval."""

    def footage_at(p: EditPlan, t: int) -> tuple[str, int] | None:
        for seg_id, s, e in p.segment_windows():
            if s <= t < e:
                seg = p.segment(seg_id)
                return (seg.asset_id, seg.source_in_ms + (t - s))
        return None

    for t in range(start_ms, end_ms, 100):
        if footage_at(plan, t) != footage_at(baseline, t):
            return False
    return True
