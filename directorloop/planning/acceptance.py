"""Acceptance rules: hard gates first, then the goal metric (spec section 21)."""

from __future__ import annotations

from ..domain.brief import CreativeBrief, GoalProfile
from ..domain.decision import Comparison, HardGate, Outcome, PromotionDecision
from ..domain.evaluation import EvaluationRun
from ..observability.weave_ops import traced

MIN_IMPROVEMENT_QUESTIONS: dict[GoalProfile, int] = {
    GoalProfile.EDUCATIONAL: 1,
    GoalProfile.PRODUCT_DEMO: 1,
    GoalProfile.NARRATIVE: 1,
    GoalProfile.COMEDY: 1,
    GoalProfile.CINEMATIC: 1,
}


@traced("decide_acceptance", kind="tool")
def decide_acceptance(
    comparison: Comparison, candidate: EvaluationRun, brief: CreativeBrief, text_assisted: list[str] | None = None
) -> PromotionDecision:
    gates: list[HardGate] = [
        HardGate(id="render_valid", passed=candidate.mechanical_passed, detail="all critical mechanical checks pass on the candidate file"),
        HardGate(id="protected_constraints", passed=candidate.constraints_passed, detail="every protected constraint still holds"),
        HardGate(id="matched_configuration", passed=comparison.matched_config, detail=comparison.config_note),
        HardGate(
            id="no_regression",
            passed=not comparison.regressed,
            detail="no previously passing question now fails" if not comparison.regressed else f"regressed: {', '.join(comparison.regressed)}",
        ),
        HardGate(
            id="duration_within_brief",
            passed=brief.min_duration_ms() <= comparison.duration_after_ms <= brief.max_duration_ms(),
            detail=f"{comparison.duration_after_ms} ms within {brief.min_duration_ms()}-{brief.max_duration_ms()} ms",
        ),
        HardGate(
            id="evidence_sufficient",
            passed=candidate.trials_valid >= max(1, candidate.trials_valid + candidate.trials_errored) * 0.5,
            detail=f"{candidate.trials_valid} valid trial answers, {candidate.trials_errored} missing",
        ),
    ]
    text_assisted = text_assisted or []
    genuine_fixes = [q for q in comparison.fixed if q not in text_assisted]
    gates.append(
        HardGate(
            id="visual_fix_not_text_substitution",
            passed=not comparison.fixed or bool(genuine_fixes),
            detail="fixes do not rely on new on-screen text stating the answer"
            if not text_assisted
            else f"answer stated in new on-screen text for visual question(s): {', '.join(text_assisted)}",
        )
    )
    min_q = MIN_IMPROVEMENT_QUESTIONS.get(brief.profile, 1)
    delta_q = comparison.delta_questions - len(text_assisted)
    delta_s = comparison.delta_score
    blocking = next((g for g in gates if not g.passed), None)
    if blocking is not None:
        if blocking.id == "evidence_sufficient":
            outcome, reason = Outcome.INSUFFICIENT_EVIDENCE, "too many probe answers were missing to judge the candidate"
        elif blocking.id == "visual_fix_not_text_substitution":
            outcome, reason = Outcome.NEEDS_REVIEW, f"the only fixes state the answer in new on-screen text instead of showing it: {blocking.detail}"
        elif blocking.id == "matched_configuration":
            outcome, reason = Outcome.NEEDS_REVIEW, f"baseline and candidate were not evaluated under matched settings: {comparison.config_note}"
        else:
            outcome, reason = Outcome.REJECTED, f"hard gate {blocking.id} failed: {blocking.detail}"
        return PromotionDecision(
            outcome=outcome,
            hard_gates=gates,
            min_improvement_questions=min_q,
            delta_questions=delta_q,
            delta_score=delta_s,
            regressions=comparison.regressed,
            fixed=genuine_fixes,
            reason=reason,
            blocking_gate_id=blocking.id,
        )
    if delta_q >= min_q:
        outcome, reason = Outcome.PROMOTED, f"{delta_q} more question(s) pass on visual or spoken evidence ({comparison.questions_passed_before} -> {comparison.questions_passed_after} of {comparison.questions_total}) with no regressions"
        if text_assisted:
            reason += f"; not counted (answer stated in new text): {', '.join(text_assisted)}"
    elif delta_q < 0:
        outcome, reason = Outcome.REJECTED, f"fewer questions pass than the baseline ({comparison.questions_passed_before} -> {comparison.questions_passed_after})"
    else:
        outcome, reason = Outcome.NO_GAIN, "the candidate passes the same questions as the baseline; keeping the baseline"
    return PromotionDecision(
        outcome=outcome,
        hard_gates=gates,
        min_improvement_questions=min_q,
        delta_questions=delta_q,
        delta_score=delta_s,
        regressions=comparison.regressed,
        fixed=genuine_fixes,
        reason=reason,
        blocking_gate_id=None,
    )
