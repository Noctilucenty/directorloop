"""Repair planner: deterministic candidate edits, optionally chosen by an LLM.

The planner never emits shell or filter strings. It chooses among typed candidate
operations that this module derives from the finding, the coverage map and the
timeline, and every candidate is validated against the brief before it is offered.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..domain.assets import AssetCoverageMap, AssetManifest
from ..domain.brief import CreativeBrief, RepairAction
from ..domain.edit_plan import (
    EditOp,
    EditPlan,
    InsertSegment,
    MoveSegment,
    Segment,
    SetCaption,
    SetCrop,
    TrimSegment,
    apply_ops,
    describe_ops,
)
from ..domain.findings import FailureFinding
from ..domain.ids import new_id
from ..domain.repair import (
    ActionEstimate,
    GenerationRequest,
    LatencyEstimate,
    RejectedAlternative,
    RepairProposal,
)
from ..domain.truth import SourceTruth
from ..media.frames import locate_motion_region
from ..media.validate import validate_ops_scope, validate_plan
from ..observability.weave_ops import traced
from ..providers.base import ProviderError, TextPlannerProvider
from .routing import rank_actions


@dataclass
class CandidateEdit:
    action: RepairAction
    ops: list[EditOp]
    note: str
    diff_lines: list[str] = field(default_factory=list)
    generation_request: GenerationRequest | None = None
    invalid_reason: str | None = None


def _action_window(finding: FailureFinding, plan: EditPlan, coverage: AssetCoverageMap) -> tuple[str, int, int] | None:
    """(segment_id, source_start_ms, source_end_ms) of the moment the claim happens in the current footage."""
    if not finding.affected_segment_ids or not finding.evidence.claim_ids:
        return None
    seg = plan.segment(finding.affected_segment_ids[0])
    claim_id = finding.evidence.claim_ids[0]
    for e in coverage.for_claim(claim_id):
        if e.asset_id == seg.asset_id and e.start_ms is not None and e.end_ms is not None and e.end_ms > e.start_ms:
            return seg.id, max(seg.source_in_ms, e.start_ms), min(seg.source_out_ms, e.end_ms)
    # Unknown moment: assume the middle third of the segment.
    span = seg.source_out_ms - seg.source_in_ms
    return seg.id, seg.source_in_ms + span // 3, seg.source_in_ms + (2 * span) // 3


def _replace_candidate(finding: FailureFinding, plan: EditPlan, manifest: AssetManifest, coverage: AssetCoverageMap, brief: CreativeBrief) -> CandidateEdit | None:
    if not finding.evidence.claim_ids or not finding.affected_segment_ids:
        return None
    claim_id = finding.evidence.claim_ids[0]
    seg = plan.segment(finding.affected_segment_ids[0])
    best = coverage.best_for_claim(claim_id, exclude_asset_ids={seg.asset_id})
    if best is None:
        return None
    rec = manifest.get(best.asset_id)
    asset_dur = rec.duration_ms or 0
    if best.start_ms is not None and best.end_ms is not None and best.end_ms > best.start_ms:
        src_in = max(0, best.start_ms - 400)
        src_out = min(asset_dur, best.end_ms + 400)
    else:
        src_in, src_out = 0, asset_dur
    if src_out - src_in < 600:
        src_in, src_out = 0, asset_dur
    window = _action_window(finding, plan, coverage)
    ops: list[EditOp] = []
    new_id_ = f"{seg.id}_closer"
    if window is not None:
        _, act_start, act_end = window
        head = act_start - seg.source_in_ms
        tail = seg.source_out_ms - act_end
        if head >= 600:
            ops.append(TrimSegment(segment_id=seg.id, source_in_ms=seg.source_in_ms, source_out_ms=act_start))
            ops.append(InsertSegment(after_segment_id=seg.id, segment=Segment(id=new_id_, asset_id=best.asset_id, source_in_ms=src_in, source_out_ms=src_out, label=rec.label)))
            if tail >= 800:
                ops.append(InsertSegment(after_segment_id=new_id_, segment=Segment(id=f"{seg.id}_tail", asset_id=seg.asset_id, source_in_ms=act_end, source_out_ms=seg.source_out_ms, label=manifest.get(seg.asset_id).label)))
        else:
            ops.append(InsertSegment(after_segment_id=None, segment=Segment(id=new_id_, asset_id=best.asset_id, source_in_ms=src_in, source_out_ms=src_out, label=rec.label)))
            ops.append(TrimSegment(segment_id=seg.id, source_in_ms=act_end, source_out_ms=seg.source_out_ms))
    else:
        ops.append(InsertSegment(after_segment_id=seg.id, segment=Segment(id=new_id_, asset_id=best.asset_id, source_in_ms=src_in, source_out_ms=src_out, label=rec.label)))
    note = f"Show the existing {rec.label} at the moment of the action ({best.status}, visibility {best.visibility})."
    return _fit_duration(CandidateEdit(RepairAction.REPLACE_WITH_EXISTING_ASSET, ops, note), plan, manifest, brief)


def _fit_duration(cand: CandidateEdit, plan: EditPlan, manifest: AssetManifest, brief: CreativeBrief) -> CandidateEdit:
    """If the edit overshoots the brief's maximum duration, shorten inserted footage, then drop tails."""
    try:
        new_plan = apply_ops(plan, cand.ops)
    except Exception as exc:  # noqa: BLE001
        cand.invalid_reason = f"ops do not apply: {exc}"
        return cand
    excess = new_plan.timeline_duration_ms() - brief.max_duration_ms()
    if excess > 0:
        for op in cand.ops:
            if isinstance(op, InsertSegment) and op.segment.id.endswith("_tail") and excess > 0:
                cut = min(excess, op.segment.duration_ms - 600)
                if cut > 0:
                    op.segment.source_out_ms -= cut
                    excess -= cut
        for op in cand.ops:
            if isinstance(op, InsertSegment) and not op.segment.id.endswith("_tail") and excess > 0:
                cut = min(excess, op.segment.duration_ms - 1200)
                if cut > 0:
                    op.segment.source_out_ms -= cut
                    excess -= cut
        if excess > 0:
            cand.ops = [op for op in cand.ops if not (isinstance(op, InsertSegment) and op.segment.id.endswith("_tail"))]
    return cand


def _crop_candidate(finding: FailureFinding, plan: EditPlan, manifest: AssetManifest, coverage: AssetCoverageMap, asset_paths: dict[str, Path]) -> CandidateEdit | None:
    if not finding.affected_segment_ids:
        return None
    seg = plan.segment(finding.affected_segment_ids[0])
    rec = manifest.get(seg.asset_id)
    if not rec.stream or not rec.stream.width or not rec.stream.height:
        return None
    window = _action_window(finding, plan, coverage)
    start, end = (window[1], window[2]) if window else (seg.source_in_ms, seg.source_out_ms)
    zoom = min(2.5, min(rec.stream.width / plan.output.width, rec.stream.height / plan.output.height) * 2.9)
    try:
        region = locate_motion_region(asset_paths[seg.asset_id], start, end, rec.stream.width, rec.stream.height, zoom=zoom)
    except Exception as exc:  # noqa: BLE001
        return CandidateEdit(RepairAction.CROP_EXISTING_SHOT, [], "motion locator failed", invalid_reason=str(exc)[:120])
    ops: list[EditOp] = [SetCrop(segment_id=seg.id, crop=region.crop)]
    note = f"Crop {rec.label} {zoom:.1f}x into the region where pixels change most ({region.energy_share:.0%} of motion energy)."
    return CandidateEdit(RepairAction.CROP_EXISTING_SHOT, ops, note)


def _reorder_candidate(finding: FailureFinding, plan: EditPlan, coverage: AssetCoverageMap) -> CandidateEdit | None:
    if not finding.evidence.claim_ids:
        return None
    claim_id = finding.evidence.claim_ids[0]
    covering = [s.id for s in plan.segments if any(e.asset_id == s.asset_id and e.status != "absent" for e in coverage.for_claim(claim_id))]
    others = [s for s in covering if s not in finding.affected_segment_ids]
    if not others or not finding.affected_segment_ids:
        return None
    target = others[0]
    idx_target = plan.segment_index(target)
    idx_aff = plan.segment_index(finding.affected_segment_ids[0])
    if idx_target < idx_aff:
        return None
    after = plan.segments[idx_aff - 1].id if idx_aff > 0 else None
    return CandidateEdit(RepairAction.REORDER_SEGMENTS, [MoveSegment(segment_id=target, after_segment_id=after)], f"Move {target}, which shows the claim, before the shot that hides it.")


def _caption_candidate(finding: FailureFinding, plan: EditPlan, truth: SourceTruth, coverage: AssetCoverageMap) -> CandidateEdit | None:
    if not finding.evidence.claim_ids:
        return None
    claim = truth.claim(finding.evidence.claim_ids[0])
    window = _action_window(finding, plan, coverage)
    if window is not None and finding.affected_segment_ids:
        seg_start = plan.segment_start_ms(finding.affected_segment_ids[0])
        seg = plan.segment(finding.affected_segment_ids[0])
        start = seg_start + (window[1] - seg.source_in_ms)
    else:
        start = finding.start_ms or 0
    end = min(plan.timeline_duration_ms(), start + 2500)
    if end - start < 800:
        start = max(0, end - 2500)
    text = claim.text[:90]
    return CandidateEdit(RepairAction.REVISE_CAPTIONS, [SetCaption(caption_id=None, text=text, start_ms=start, end_ms=end, position="top")], "State the approved claim as an on-screen caption at the moment it happens.")


def _generation_candidate(finding: FailureFinding, truth: SourceTruth, plan: EditPlan) -> CandidateEdit | None:
    if not finding.evidence.claim_ids:
        return None
    claim = truth.claim(finding.evidence.claim_ids[0])
    req = GenerationRequest(
        prompt=f"Close-up shot showing: {claim.text}. Match the look of the existing footage.",
        duration_ms=1500,
        width=512,
        height=896,
        conditioning_asset_id=plan.segment(finding.affected_segment_ids[0]).asset_id if finding.affected_segment_ids else None,
        must_show=[claim.id],
        identity_notes="Keep the object's shape, colors and materials identical to the existing footage.",
    )
    return CandidateEdit(RepairAction.GENERATE_MISSING_SHOT, [], "Generate only the missing 1.5 s shot and insert it at the point of the action.", generation_request=req)


def build_candidates(
    finding: FailureFinding,
    ranked: list[ActionEstimate],
    brief: CreativeBrief,
    plan: EditPlan,
    manifest: AssetManifest,
    coverage: AssetCoverageMap,
    truth: SourceTruth,
    asset_paths: dict[str, Path],
    baseline_plan: EditPlan,
) -> list[CandidateEdit]:
    labels = {a.id: a.label for a in manifest.assets}
    out: list[CandidateEdit] = []
    for row in ranked:
        cand: CandidateEdit | None = None
        if row.action == RepairAction.REPLACE_WITH_EXISTING_ASSET:
            cand = _replace_candidate(finding, plan, manifest, coverage, brief)
        elif row.action == RepairAction.CROP_EXISTING_SHOT:
            cand = _crop_candidate(finding, plan, manifest, coverage, asset_paths)
        elif row.action == RepairAction.REORDER_SEGMENTS:
            cand = _reorder_candidate(finding, plan, coverage)
        elif row.action == RepairAction.REVISE_CAPTIONS:
            cand = _caption_candidate(finding, plan, truth, coverage)
        elif row.action == RepairAction.GENERATE_MISSING_SHOT:
            cand = _generation_candidate(finding, truth, plan)
        if cand is None:
            continue
        if cand.ops and cand.invalid_reason is None:
            scope_errors = validate_ops_scope(cand.ops, brief)
            if scope_errors:
                cand.invalid_reason = "; ".join(scope_errors)
            else:
                try:
                    new_plan = apply_ops(plan, cand.ops)
                    vr = validate_plan(new_plan, manifest, brief, baseline=baseline_plan)
                    if not vr.ok:
                        cand.invalid_reason = "; ".join(vr.errors)[:300]
                except Exception as exc:  # noqa: BLE001
                    cand.invalid_reason = str(exc)[:200]
            cand.diff_lines = describe_ops(cand.ops, plan, labels)
        out.append(cand)
    return out


PLANNER_SYSTEM = (
    "You are the repair planner of DirectorLoop, an AI creative director. You receive one diagnosed failure in a short "
    "video, the creator's brief with protected constraints, the current timeline, what each clip visibly shows, a routing "
    "table of possible repair actions with measured or default latency and prior outcomes, learned strategy hints with "
    "their evidence, and concrete candidate edits that are already validated. Choose the SMALLEST justified repair: prefer "
    "re-editing existing footage over generation, prefer showing over telling, never violate protected constraints, and "
    "choose do_nothing if no candidate is justified by the evidence. Return JSON only."
)


def _planner_schema(n: int) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "chosen_candidate_index": {"type": ["integer", "null"], "minimum": 0, "maximum": max(0, n - 1)},
            "hypothesis": {"type": "string"},
            "predicted_benefit": {"type": "string"},
            "decision_summary": {"type": "string"},
            "rejected_alternatives": {
                "type": "array",
                "items": {"type": "object", "properties": {"action": {"type": "string"}, "reason": {"type": "string"}}, "required": ["action", "reason"]},
            },
        },
        "required": ["chosen_candidate_index", "hypothesis", "predicted_benefit", "decision_summary", "rejected_alternatives"],
    }


@traced("propose_repair", kind="agent")
def propose_repair(
    *,
    finding: FailureFinding,
    brief: CreativeBrief,
    plan: EditPlan,
    baseline_plan: EditPlan,
    manifest: AssetManifest,
    coverage: AssetCoverageMap,
    truth: SourceTruth,
    asset_paths: dict[str, Path],
    routing: list[ActionEstimate],
    policy_hints: list[dict[str, Any]],
    planner: TextPlannerProvider | None,
    deadline_ms: int | None,
    transcript: str | None,
) -> RepairProposal:
    ranked = rank_actions(routing, deadline_ms=deadline_ms)
    candidates = build_candidates(finding, ranked, brief, plan, manifest, coverage, truth, asset_paths, baseline_plan)
    valid = [c for c in candidates if c.invalid_reason is None and (c.ops or c.generation_request)]
    invalid = [c for c in candidates if c.invalid_reason is not None]
    labels = {a.id: a.label for a in manifest.assets}
    row_for = {r.action: r for r in routing}

    chosen: CandidateEdit | None = valid[0] if valid else None
    planner_label = "rules"
    hypothesis = ""
    predicted = ""
    summary = ""
    rejected: list[RejectedAlternative] = []

    if planner is not None and valid:
        user = json.dumps(
            {
                "finding": {
                    "category": str(finding.category),
                    "observed": finding.observed,
                    "inferred_cause": finding.inferred_cause,
                    "alternatives": finding.alternatives,
                    "evidence": finding.evidence.model_dump(),
                    "affected_segments": finding.affected_segment_ids,
                },
                "brief": {
                    "profile": str(brief.profile),
                    "objective": brief.objective,
                    "protected_constraints": [f"{c.kind}: {c.reason}" for c in brief.protected_constraints],
                    "allowed_actions": [str(a) for a in brief.allowed_actions],
                    "max_duration_ms": brief.max_duration_ms(),
                },
                "timeline": [
                    {"segment_id": s.id, "clip": labels.get(s.asset_id, s.asset_id), "source_ms": [s.source_in_ms, s.source_out_ms], "starts_at_ms": plan.segment_start_ms(s.id)}
                    for s in plan.segments
                ],
                "captions": [{"text": c.text, "ms": [c.start_ms, c.end_ms]} for c in plan.captions],
                "rendered_audio_transcript": transcript,
                "clip_coverage": [
                    {"clip": labels.get(e.asset_id, e.asset_id), "claim_id": e.claim_id, "status": str(e.status), "visibility": e.visibility, "note": e.note}
                    for e in coverage.entries
                    if e.claim_id in finding.evidence.claim_ids
                ],
                "routing_table": [
                    {
                        "action": str(r.action),
                        "allowed": r.allowed,
                        "feasible": r.feasible,
                        "expected_improvement": r.expected_improvement,
                        "latency_ms": r.latency.expected_ms,
                        "latency_source": r.latency.source,
                        "cost_usd": r.cost_usd,
                        "creative_risk": r.creative_risk,
                        "regression_risk": r.regression_risk,
                        "prior_success_rate": r.prior_success_rate,
                        "prior_samples": r.prior_samples,
                        "note": r.note,
                    }
                    for r in routing
                ],
                "strategy_hints": policy_hints,
                "candidates": [
                    {"index": i, "action": str(c.action), "note": c.note, "edit": c.diff_lines or (["generate a missing shot"] if c.generation_request else [])}
                    for i, c in enumerate(valid)
                ],
                "invalid_candidates": [{"action": str(c.action), "reason": c.invalid_reason} for c in invalid],
                "deadline_ms": deadline_ms,
            },
            ensure_ascii=False,
        )
        try:
            res = planner.complete_json(PLANNER_SYSTEM, user, _planner_schema(len(valid)), temperature=0.2)
            data = res.data
            idx = data.get("chosen_candidate_index")
            if isinstance(idx, int) and 0 <= idx < len(valid):
                chosen = valid[idx]
            elif idx is None:
                chosen = None
            planner_label = f"{planner.capability.name}:{planner.capability.model}"
            hypothesis = str(data.get("hypothesis", ""))[:400]
            predicted = str(data.get("predicted_benefit", ""))[:300]
            summary = str(data.get("decision_summary", ""))[:600]
            for item in data.get("rejected_alternatives", []) or []:
                if isinstance(item, dict) and item.get("action"):
                    try:
                        rejected.append(RejectedAlternative(action=RepairAction(str(item["action"])), reason=str(item.get("reason", ""))[:200]))
                    except ValueError:
                        continue
        except ProviderError as exc:
            planner_label = f"rules (planner failed: {str(exc)[:80]})"

    if not rejected:
        for c in valid:
            if c is not chosen:
                rejected.append(RejectedAlternative(action=c.action, reason=f"ranked below the chosen action: {c.note}"))
        for c in invalid:
            rejected.append(RejectedAlternative(action=c.action, reason=f"invalid: {c.invalid_reason}"))
    if chosen is None:
        row = row_for.get(RepairAction.DO_NOTHING)
        return RepairProposal(
            id=new_id("prop"),
            finding_id=finding.id,
            hypothesis=hypothesis or "No candidate repair is justified by the evidence.",
            evidence_refs=finding.evidence.failed_question_ids,
            action=RepairAction.DO_NOTHING,
            ops=[],
            predicted_benefit=predicted or "none",
            expected_latency=LatencyEstimate(expected_ms=0, source="n/a", samples=0),
            estimated_cost_usd=0.0,
            rejected_alternatives=rejected,
            planner=planner_label,
            routing_table=routing,
            decision_summary=summary or "No valid candidate edit was available under the brief.",
        )
    row = row_for[chosen.action]
    if not hypothesis:
        hypothesis = f"If we {chosen.note[0].lower() + chosen.note[1:]} the failed questions ({', '.join(finding.evidence.failed_question_ids)}) should pass without regressions."
    return RepairProposal(
        id=new_id("prop"),
        finding_id=finding.id,
        hypothesis=hypothesis,
        evidence_refs=finding.evidence.failed_question_ids,
        action=chosen.action,
        ops=chosen.ops,
        predicted_benefit=predicted or f"expected improvement: {row.expected_improvement} (prediction)",
        expected_latency=row.latency,
        estimated_cost_usd=row.cost_usd or 0.0,
        creative_risk=row.creative_risk,
        regression_risk=row.regression_risk,
        rejected_alternatives=rejected,
        requires_generation=chosen.generation_request is not None,
        generation_request=chosen.generation_request,
        validation_requirements=["same frozen suite", "protected constraints", "no regression on guard questions"],
        policy_rule_ids_used=[str(h.get("rule_id")) for h in policy_hints],
        planner=planner_label,
        routing_table=routing,
        decision_summary=summary or chosen.note,
    )
