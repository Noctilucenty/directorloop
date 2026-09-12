"""Compute-aware repair routing (spec section 15).

Builds the transparent routing table judges see: for each action, whether it is
allowed and feasible, expected improvement, a latency estimate with its source,
cost, risks, and prior success statistics from policy memory (with shrinkage).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..domain.assets import AssetCoverageMap, AssetManifest, CoverageStatus
from ..domain.brief import ACTION_COST_ORDER, ConstraintKind, CreativeBrief, GoalProfile, RepairAction
from ..domain.edit_plan import EditPlan
from ..domain.findings import FailureCategory, FailureFinding
from ..domain.policy import PolicyStore
from ..domain.repair import ActionEstimate, LatencyEstimate


@dataclass
class LatencyProfile:
    """Measured latencies collected at runtime; defaults are labeled as such."""

    render_ms: list[int] = field(default_factory=list)
    evaluate_ms: list[int] = field(default_factory=list)
    planner_ms: list[int] = field(default_factory=list)
    generation_ms: list[int] = field(default_factory=list)
    coverage_ms: list[int] = field(default_factory=list)

    def estimate(self, kind: str, default_ms: int) -> LatencyEstimate:
        samples = getattr(self, f"{kind}_ms", [])
        if samples:
            ordered = sorted(samples)
            p50 = ordered[len(ordered) // 2]
            return LatencyEstimate(expected_ms=p50, source=f"measured: p50 of {len(samples)} samples", samples=len(samples))
        return LatencyEstimate(expected_ms=default_ms, source="default (unmeasured)", samples=0)

    def record(self, kind: str, ms: int) -> None:
        getattr(self, f"{kind}_ms").append(int(ms))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.__dict__), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> LatencyProfile:
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(**{k: list(v) for k, v in data.items() if k.endswith("_ms")})


RISK: dict[RepairAction, tuple[str, str]] = {
    RepairAction.DO_NOTHING: ("low", "low"),
    RepairAction.TRIM_OR_RETIME: ("low", "medium"),
    RepairAction.REORDER_SEGMENTS: ("medium", "medium"),
    RepairAction.REPLACE_WITH_EXISTING_ASSET: ("low", "low"),
    RepairAction.CROP_EXISTING_SHOT: ("low", "low"),
    RepairAction.REVISE_CAPTIONS: ("low", "low"),
    RepairAction.REVISE_NARRATION: ("medium", "medium"),
    RepairAction.ADD_GRAPHIC: ("medium", "low"),
    RepairAction.GENERATE_MISSING_SHOT: ("high", "medium"),
    RepairAction.REGENERATE_SEGMENT: ("high", "high"),
    RepairAction.REGENERATE_FULL_DRAFT: ("high", "high"),
}

EXPECTED: dict[FailureCategory, dict[RepairAction, str]] = {
    FailureCategory.MISSING_ACTION_VISIBILITY: {
        RepairAction.REPLACE_WITH_EXISTING_ASSET: "high",
        RepairAction.CROP_EXISTING_SHOT: "medium",
        RepairAction.REORDER_SEGMENTS: "medium",
        RepairAction.GENERATE_MISSING_SHOT: "unknown",
        RepairAction.REVISE_CAPTIONS: "low",
        RepairAction.REVISE_NARRATION: "low",
    },
    FailureCategory.VISUAL_NARRATION_MISMATCH: {
        RepairAction.REPLACE_WITH_EXISTING_ASSET: "high",
        RepairAction.REORDER_SEGMENTS: "medium",
        RepairAction.CROP_EXISTING_SHOT: "low",
        RepairAction.GENERATE_MISSING_SHOT: "unknown",
    },
    FailureCategory.MISSING_REQUIRED_INFORMATION: {
        RepairAction.REPLACE_WITH_EXISTING_ASSET: "medium",
        RepairAction.REVISE_CAPTIONS: "medium",
        RepairAction.REVISE_NARRATION: "high",
        RepairAction.GENERATE_MISSING_SHOT: "unknown",
    },
    FailureCategory.EVENT_ORDER_CONFUSION: {RepairAction.REORDER_SEGMENTS: "high", RepairAction.REVISE_CAPTIONS: "low"},
    FailureCategory.UNCLEAR_CAUSALITY: {RepairAction.REORDER_SEGMENTS: "medium", RepairAction.REVISE_CAPTIONS: "medium"},
    FailureCategory.CAPTION_OVERFLOW: {RepairAction.REVISE_CAPTIONS: "high"},
    FailureCategory.CAPTION_TIMING_ERROR: {RepairAction.REVISE_CAPTIONS: "high"},
    FailureCategory.AUDIO_CUT_OR_CLIP: {RepairAction.TRIM_OR_RETIME: "high"},
}


@dataclass
class Feasibility:
    feasible: bool
    note: str
    data: dict[str, object] = field(default_factory=dict)


def feasibility(
    action: RepairAction,
    finding: FailureFinding,
    brief: CreativeBrief,
    plan: EditPlan,
    manifest: AssetManifest,
    coverage: AssetCoverageMap,
    generation_available: bool,
) -> Feasibility:
    claim_id = finding.evidence.claim_ids[0] if finding.evidence.claim_ids else None
    current_assets = {plan.segment(s).asset_id for s in finding.affected_segment_ids} if finding.affected_segment_ids else set()
    if action == RepairAction.DO_NOTHING:
        return Feasibility(True, "always available")
    if action == RepairAction.REPLACE_WITH_EXISTING_ASSET:
        if not claim_id:
            return Feasibility(False, "no claim to cover")
        best = coverage.best_for_claim(claim_id, exclude_asset_ids=current_assets)
        if best is None:
            return Feasibility(False, "no other asset covers this claim")
        if best.status == CoverageStatus.VERIFIED and best.visibility == "clear":
            return Feasibility(True, f"{best.asset_id} shows it clearly (verified from pixels)", {"asset_id": best.asset_id, "entry": best.model_dump()})
        if best.status == CoverageStatus.VERIFIED:
            return Feasibility(True, f"{best.asset_id} shows it, but small or partial", {"asset_id": best.asset_id, "entry": best.model_dump()})
        return Feasibility(True, f"{best.asset_id} declared by the creator but not verified from pixels", {"asset_id": best.asset_id, "entry": best.model_dump()})
    if action == RepairAction.CROP_EXISTING_SHOT:
        if not finding.affected_segment_ids:
            return Feasibility(False, "no segment to crop")
        seg = plan.segment(finding.affected_segment_ids[0])
        rec = manifest.get(seg.asset_id)
        if not rec.stream or not rec.stream.width or not rec.stream.height:
            return Feasibility(False, "source geometry unknown")
        max_zoom = min(rec.stream.width / plan.output.width, rec.stream.height / plan.output.height) * 3.0
        if max_zoom < 1.5:
            return Feasibility(False, f"source resolution only allows {max_zoom:.1f}x zoom")
        return Feasibility(True, f"source allows up to {max_zoom:.1f}x zoom before exceeding the 3x upscale limit", {"segment_id": seg.id, "max_zoom": max_zoom})
    if action == RepairAction.REORDER_SEGMENTS:
        if not claim_id:
            return Feasibility(False, "no claim")
        covering = [s.id for s in plan.segments if any(e.asset_id == s.asset_id and e.status != CoverageStatus.ABSENT for e in coverage.for_claim(claim_id))]
        others = [s for s in covering if s not in finding.affected_segment_ids]
        if not others:
            return Feasibility(False, "no other segment in the timeline covers this claim")
        return Feasibility(True, f"segment {others[0]} already covers the claim and could move earlier", {"segment_id": others[0]})
    if action == RepairAction.REVISE_CAPTIONS:
        if brief.has_constraint(ConstraintKind.NO_NEW_TEXT):
            return Feasibility(False, "brief forbids new on-screen text")
        if brief.has_constraint(ConstraintKind.NO_NEW_FACTS):
            return Feasibility(True, "allowed only with wording that restates an approved claim; needs review under no_new_facts", {"needs_review": True})
        return Feasibility(True, "caption text can change")
    if action == RepairAction.REVISE_NARRATION:
        if not brief.narration_change_permitted or brief.has_constraint(ConstraintKind.KEEP_NARRATION):
            return Feasibility(False, "narration is protected")
        return Feasibility(True, "narration may be re-synthesized")
    if action == RepairAction.GENERATE_MISSING_SHOT:
        if not brief.generation_permitted or brief.has_constraint(ConstraintKind.NO_GENERATION):
            return Feasibility(False, "generation not permitted by the brief")
        if brief.has_constraint(ConstraintKind.PRESERVE_PRODUCT_APPEARANCE):
            return Feasibility(generation_available, "permitted, but a generated shot risks altering the product's appearance (protected)" + ("" if generation_available else "; no generation provider configured"))
        return Feasibility(generation_available, "generation provider available" if generation_available else "no generation provider configured")
    if action in (RepairAction.TRIM_OR_RETIME,):
        return Feasibility(True, "timing can change within the brief's duration bounds")
    return Feasibility(False, "not implemented in this build")


DEFAULT_LATENCY = {
    RepairAction.DO_NOTHING: 0,
    RepairAction.TRIM_OR_RETIME: 1500,
    RepairAction.REORDER_SEGMENTS: 1500,
    RepairAction.REPLACE_WITH_EXISTING_ASSET: 1500,
    RepairAction.CROP_EXISTING_SHOT: 2500,
    RepairAction.REVISE_CAPTIONS: 1500,
    RepairAction.REVISE_NARRATION: 9000,
    RepairAction.ADD_GRAPHIC: 3000,
    RepairAction.GENERATE_MISSING_SHOT: 60000,
    RepairAction.REGENERATE_SEGMENT: 90000,
    RepairAction.REGENERATE_FULL_DRAFT: 180000,
}

DEFAULT_COST_USD = {
    RepairAction.GENERATE_MISSING_SHOT: 0.18,
    RepairAction.REGENERATE_SEGMENT: 0.25,
    RepairAction.REGENERATE_FULL_DRAFT: 0.80,
    RepairAction.REVISE_NARRATION: 0.02,
}


def build_routing_table(
    finding: FailureFinding,
    brief: CreativeBrief,
    plan: EditPlan,
    manifest: AssetManifest,
    coverage: AssetCoverageMap,
    store: PolicyStore,
    latency: LatencyProfile,
    generation_available: bool,
) -> list[ActionEstimate]:
    rows: list[ActionEstimate] = []
    profile: GoalProfile = brief.profile
    for action in ACTION_COST_ORDER:
        allowed = brief.action_allowed(action)
        feas = feasibility(action, finding, brief, plan, manifest, coverage, generation_available)
        expected = EXPECTED.get(finding.category, {}).get(action, "none" if action != RepairAction.DO_NOTHING else "none")
        if action == RepairAction.GENERATE_MISSING_SHOT:
            est = latency.estimate("generation", DEFAULT_LATENCY[action])
        elif action == RepairAction.DO_NOTHING:
            est = LatencyEstimate(expected_ms=0, source="n/a", samples=0)
        else:
            base = latency.estimate("render", DEFAULT_LATENCY[action])
            extra = 1000 if action == RepairAction.CROP_EXISTING_SHOT else 0
            est = LatencyEstimate(expected_ms=base.expected_ms + extra, source=base.source, samples=base.samples)
        stat = store.stat(profile, finding.category, action)
        creative, regression = RISK[action]
        rows.append(
            ActionEstimate(
                action=action,
                allowed=allowed,
                feasible=feas.feasible,
                expected_improvement=expected,
                latency=est,
                cost_usd=DEFAULT_COST_USD.get(action, 0.0),
                creative_risk=creative,
                regression_risk=regression,
                prior_success_rate=stat.shrunk_success_rate() if stat.attempts else None,
                prior_samples=stat.attempts,
                note=feas.note,
            )
        )
    return rows


IMPROVEMENT_RANK = {"high": 3, "medium": 2, "unknown": 1, "low": 1, "none": 0}
RISK_RANK = {"low": 0, "medium": 1, "high": 2}


def rank_actions(rows: list[ActionEstimate], deadline_ms: int | None = None, min_prior_samples: int = 2) -> list[ActionEstimate]:
    """Deterministic ordering used by the rules planner and offered to the LLM planner.

    Sort by expected improvement, then learned prior success (only with enough samples),
    then lower risk, then lower latency, then cost order. Actions that are not allowed,
    not feasible, or exceed the deadline are excluded.
    """
    usable = [r for r in rows if r.allowed and r.feasible and r.action != RepairAction.DO_NOTHING]
    if deadline_ms is not None:
        usable = [r for r in usable if r.latency.expected_ms <= deadline_ms]

    def key(r: ActionEstimate) -> tuple:
        prior = r.prior_success_rate if (r.prior_success_rate is not None and r.prior_samples >= min_prior_samples) else 0.5
        return (
            -IMPROVEMENT_RANK.get(r.expected_improvement, 0),
            -round(prior, 2),
            RISK_RANK[r.creative_risk] + RISK_RANK[r.regression_risk],
            r.latency.expected_ms,
            ACTION_COST_ORDER.index(r.action),
        )

    return sorted(usable, key=key)
