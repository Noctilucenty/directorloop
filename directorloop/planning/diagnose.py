"""Aggregate evaluation evidence into structured failure findings.

Deterministic. Observations (what failed, where, how often) stay separate from the
inferred cause, and every finding lists alternative explanations.
"""

from __future__ import annotations

import re

from ..domain.assets import AssetCoverageMap, AssetManifest
from ..domain.brief import CreativeBrief, RepairAction
from ..domain.edit_plan import EditPlan
from ..domain.evaluation import EvaluationRun, MeasurementType
from ..domain.findings import FailureCategory, FailureFinding, FindingEvidence
from ..domain.ids import new_id
from ..domain.truth import NOT_SHOWN_OPTION_ID, ClaimKind, EvaluationSuite, EvidenceModality, SourceTruth
from ..observability.weave_ops import traced

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "into", "is", "it", "its", "that", "this", "with",
    "when", "after", "before", "then", "by", "for", "at", "as", "be", "was", "are", "from", "up", "down",
}

EDITABLE: dict[FailureCategory, list[RepairAction]] = {
    FailureCategory.MISSING_ACTION_VISIBILITY: [
        RepairAction.REPLACE_WITH_EXISTING_ASSET,
        RepairAction.CROP_EXISTING_SHOT,
        RepairAction.REORDER_SEGMENTS,
        RepairAction.GENERATE_MISSING_SHOT,
    ],
    FailureCategory.VISUAL_NARRATION_MISMATCH: [
        RepairAction.REPLACE_WITH_EXISTING_ASSET,
        RepairAction.REORDER_SEGMENTS,
        RepairAction.CROP_EXISTING_SHOT,
        RepairAction.GENERATE_MISSING_SHOT,
    ],
    FailureCategory.MISSING_REQUIRED_INFORMATION: [
        RepairAction.REPLACE_WITH_EXISTING_ASSET,
        RepairAction.REVISE_CAPTIONS,
        RepairAction.REVISE_NARRATION,
        RepairAction.GENERATE_MISSING_SHOT,
    ],
    FailureCategory.UNCLEAR_CAUSALITY: [RepairAction.REORDER_SEGMENTS, RepairAction.REVISE_CAPTIONS, RepairAction.REPLACE_WITH_EXISTING_ASSET],
    FailureCategory.EVENT_ORDER_CONFUSION: [RepairAction.REORDER_SEGMENTS, RepairAction.REVISE_CAPTIONS],
    FailureCategory.CAPTION_OVERFLOW: [RepairAction.REVISE_CAPTIONS],
    FailureCategory.CAPTION_TIMING_ERROR: [RepairAction.REVISE_CAPTIONS],
    FailureCategory.AUDIO_CUT_OR_CLIP: [RepairAction.TRIM_OR_RETIME],
    FailureCategory.RENDER_INVALID: [],
    FailureCategory.STYLE_CONSTRAINT_VIOLATION: [],
}


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9']+", text.lower()) if t not in STOPWORDS and len(t) > 2}


def transcript_mentions(claim_text: str, transcript: str | None) -> bool | None:
    if transcript is None:
        return None
    ct, tt = _tokens(claim_text), _tokens(transcript)
    if not ct:
        return None
    return len(ct & tt) >= max(2, int(0.5 * len(ct)))


def _segments_for_claim(plan: EditPlan, manifest: AssetManifest, coverage: AssetCoverageMap, claim_id: str) -> list[str]:
    ids: list[str] = []
    # every clip meant to carry the claim, including clips a pixel check found NOT showing it:
    # those are exactly the segments where the communication fails
    covering_assets = {e.asset_id for e in coverage.for_claim(claim_id)}
    for seg in plan.segments:
        if seg.asset_id in covering_assets:
            ids.append(seg.id)
    return ids


@traced("aggregate_findings", kind="tool")
def aggregate_findings(
    run: EvaluationRun,
    suite: EvaluationSuite,
    truth: SourceTruth,
    plan: EditPlan,
    manifest: AssetManifest,
    coverage: AssetCoverageMap,
    brief: CreativeBrief,
) -> list[FailureFinding]:
    findings: list[FailureFinding] = []

    for m in run.critical_mechanical_failures():
        if m.id.startswith("caption_fit"):
            cat = FailureCategory.CAPTION_OVERFLOW
        elif m.id.startswith("caption_within_video"):
            cat = FailureCategory.CAPTION_TIMING_ERROR
        elif m.id == "narration_not_cut":
            cat = FailureCategory.AUDIO_CUT_OR_CLIP
        else:
            cat = FailureCategory.RENDER_INVALID
        findings.append(
            FailureFinding(
                id=new_id("find"),
                category=cat,
                severity=1.0,
                confidence=0.99,
                timestamp_precision="frame" if cat != FailureCategory.RENDER_INVALID else "unknown",
                observed=f"mechanical check {m.id} failed: {m.detail}",
                inferred_cause="deterministic check; no inference needed",
                evidence=FindingEvidence(mechanical_check_ids=[m.id]),
                measurement_type=MeasurementType.MECHANICAL,
                editable_dimensions=EDITABLE.get(cat, []),
            )
        )
    for c in run.constraints:
        if not c.passed:
            findings.append(
                FailureFinding(
                    id=new_id("find"),
                    category=FailureCategory.STYLE_CONSTRAINT_VIOLATION,
                    severity=1.0,
                    confidence=0.99,
                    observed=f"protected constraint {c.constraint_id} ({c.kind}) violated: {c.detail}",
                    inferred_cause="the plan changed something the brief protects",
                    evidence=FindingEvidence(constraint_ids=[c.constraint_id]),
                    measurement_type=MeasurementType.MECHANICAL,
                )
            )

    # Model-probe failures grouped by claim
    grouped: dict[tuple[FailureCategory, str], FailureFinding] = {}
    for qs in run.question_summaries:
        if qs.passed or qs.valid_trials == 0:
            continue
        q = suite.question(qs.question_id)
        claim_id = q.claim_ids[0] if q.claim_ids else ""
        claim = truth.claim(claim_id) if claim_id else None
        mentioned = transcript_mentions(claim.text, run.transcript_text) if claim else None
        chose_not_shown = qs.chosen.get(NOT_SHOWN_OPTION_ID, 0)
        wrong_content = sum(v for k, v in qs.chosen.items() if k != NOT_SHOWN_OPTION_ID and k != q.correct_option_id)
        kind = claim.kind if claim else ClaimKind.FACT

        if q.modality == EvidenceModality.VISUAL:
            if wrong_content > chose_not_shown and kind == ClaimKind.SEQUENCE:
                cat = FailureCategory.EVENT_ORDER_CONFUSION
            elif mentioned:
                cat = FailureCategory.MISSING_ACTION_VISIBILITY if kind in (ClaimKind.MECHANISM, ClaimKind.OUTCOME, ClaimKind.SEQUENCE) else FailureCategory.VISUAL_NARRATION_MISMATCH
            else:
                cat = FailureCategory.MISSING_ACTION_VISIBILITY if kind in (ClaimKind.MECHANISM, ClaimKind.OUTCOME) else FailureCategory.MISSING_REQUIRED_INFORMATION
        else:
            if mentioned is False:
                cat = FailureCategory.MISSING_REQUIRED_INFORMATION
            elif wrong_content > chose_not_shown and kind in (ClaimKind.MECHANISM, ClaimKind.MOTIVATION):
                cat = FailureCategory.UNCLEAR_CAUSALITY
            elif wrong_content > chose_not_shown and kind == ClaimKind.SEQUENCE:
                cat = FailureCategory.EVENT_ORDER_CONFUSION
            else:
                cat = FailureCategory.MISSING_REQUIRED_INFORMATION

        segs = _segments_for_claim(plan, manifest, coverage, claim_id) if claim_id else []
        key = (cat, claim_id)
        sev = min(1.0, (1.0 - qs.pass_rate) * (0.7 + 0.3 * q.weight))
        conf = min(0.95, 0.4 + 0.15 * qs.valid_trials)
        if key in grouped:
            f = grouped[key]
            f.evidence.failed_question_ids.append(q.id)
            f.evidence.probe_failures += qs.valid_trials - qs.correct
            f.evidence.probe_trials += qs.valid_trials
            f.severity = max(f.severity, sev)
            continue
        start = end = None
        precision = "unknown"
        if segs:
            start = plan.segment_start_ms(segs[0])
            end = start + plan.segment(segs[0]).duration_ms
            precision = "segment"
        observed = (
            f"Probe question {q.id} failed in {qs.valid_trials - qs.correct} of {qs.valid_trials} trials "
            f"(answers: {qs.chosen}). Claim: {claim.text if claim else 'n/a'}."
        )
        if cat == FailureCategory.MISSING_ACTION_VISIBILITY:
            cause = "The required action is not visibly identifiable in the footage that currently covers it" + (
                "; the narration mentions it but the pictures do not show it." if mentioned else "."
            )
            alts = ["A model recognition error on small details.", "Frame sampling may have skipped the exact moment."]
        elif cat == FailureCategory.VISUAL_NARRATION_MISMATCH:
            cause = "Narration states the information while the visible footage shows something else."
            alts = ["The viewer may weigh pictures over narration for this question."]
        elif cat == FailureCategory.MISSING_REQUIRED_INFORMATION:
            cause = "Neither the pictures nor the audio communicate this required information."
            alts = ["Transcript may have missed a phrase.", "The information may be implied but not stated."]
        elif cat == FailureCategory.EVENT_ORDER_CONFUSION:
            cause = "The order of shots does not make the sequence of events clear."
            alts = ["The model may have inferred order from narration rather than pictures."]
        else:
            cause = "Cause and effect are not connected clearly enough in the current cut."
            alts = ["A single ambiguous phrase may be responsible."]
        grouped[key] = FailureFinding(
            id=new_id("find"),
            category=cat,
            severity=sev,
            confidence=conf,
            start_ms=start,
            end_ms=end,
            timestamp_precision=precision,
            observed=observed,
            inferred_cause=cause,
            alternatives=alts,
            evidence=FindingEvidence(
                failed_question_ids=[q.id],
                probe_failures=qs.valid_trials - qs.correct,
                probe_trials=qs.valid_trials,
                claim_ids=[claim_id] if claim_id else [],
                transcript_mentions_claim=mentioned,
                coverage_note=_coverage_note(coverage, manifest, claim_id),
            ),
            measurement_type=MeasurementType.MODEL_PROBE,
            editable_dimensions=[a for a in EDITABLE.get(cat, []) if brief.action_allowed(a)],
            affected_segment_ids=segs,
        )
    findings.extend(grouped.values())
    findings.sort(key=lambda f: (f.measurement_type != MeasurementType.MECHANICAL, -f.severity))
    return findings


def _coverage_note(coverage: AssetCoverageMap, manifest: AssetManifest, claim_id: str) -> str:
    if not claim_id:
        return ""
    labels = {a.id: a.label for a in manifest.assets}
    parts = []
    for e in coverage.for_claim(claim_id):
        parts.append(f"{labels.get(e.asset_id, e.asset_id)}: {e.status}/{e.visibility}")
    return "; ".join(parts) or "no asset covers this claim"
