"""Compare a candidate evaluation with the baseline under the same frozen suite."""

from __future__ import annotations

from ..domain.decision import Comparison, QuestionDelta
from ..domain.evaluation import EvaluationRun
from ..domain.truth import EvaluationSuite


def compare_runs(baseline: EvaluationRun, candidate: EvaluationRun, suite: EvaluationSuite, duration_before_ms: int, duration_after_ms: int) -> Comparison:
    matched = (
        baseline.suite_hash == candidate.suite_hash
        and baseline.provider == candidate.provider
        and baseline.model == candidate.model
        and baseline.trials == candidate.trials
        and baseline.probe_modality == candidate.probe_modality
    )
    notes = []
    if baseline.suite_hash != candidate.suite_hash:
        notes.append("different suite versions")
    if (baseline.provider, baseline.model) != (candidate.provider, candidate.model):
        notes.append(f"provider/model differ ({baseline.provider}:{baseline.model} vs {candidate.provider}:{candidate.model})")
    if baseline.trials != candidate.trials:
        notes.append("trial counts differ")
    if baseline.probe_modality != candidate.probe_modality:
        notes.append("probe modality differs")
    deltas: list[QuestionDelta] = []
    for q in suite.questions:
        b = baseline.summary_for(q.id)
        c = candidate.summary_for(q.id)
        deltas.append(
            QuestionDelta(
                question_id=q.id,
                before_passed=bool(b and b.passed),
                after_passed=bool(c and c.passed),
                before_rate=b.pass_rate if b else 0.0,
                after_rate=c.pass_rate if c else 0.0,
                regression_guard=q.regression_guard,
            )
        )
    return Comparison(
        baseline_version_id=baseline.version_id,
        candidate_version_id=candidate.version_id,
        suite_hash=candidate.suite_hash,
        matched_config=matched,
        config_note="; ".join(notes) or "matched provider, model, trials, modality and suite",
        deltas=deltas,
        fixed=[d.question_id for d in deltas if d.status == "fixed"],
        regressed=[d.question_id for d in deltas if d.status == "regressed"],
        kept=[d.question_id for d in deltas if d.status == "kept"],
        still_failing=[d.question_id for d in deltas if d.status == "still_failing"],
        score_before=baseline.score,
        score_after=candidate.score,
        questions_passed_before=baseline.questions_passed,
        questions_passed_after=candidate.questions_passed,
        questions_total=len(suite.questions),
        duration_before_ms=duration_before_ms,
        duration_after_ms=duration_after_ms,
        mechanical_before=baseline.mechanical_passed,
        mechanical_after=candidate.mechanical_passed,
        constraints_before=baseline.constraints_passed,
        constraints_after=candidate.constraints_passed,
    )
