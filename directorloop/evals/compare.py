"""Compare a candidate evaluation with the baseline under the same frozen suite."""

from __future__ import annotations

import re

from ..domain.decision import Comparison, QuestionDelta
from ..domain.edit_plan import EditPlan
from ..domain.evaluation import EvaluationRun
from ..domain.truth import EvaluationSuite, EvidenceModality, SourceTruth

_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "into", "is", "it", "its", "that", "this", "with",
         "when", "after", "before", "then", "by", "for", "at", "as", "be", "was", "are", "from", "up", "down"}


def _content_tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9']+", text.lower()) if len(t) > 2 and t not in _STOP}


def text_assisted_fixes(
    suite: EvaluationSuite,
    truth: SourceTruth | None,
    baseline_plan: EditPlan,
    candidate_plan: EditPlan,
    fixed_question_ids: list[str],
) -> list[str]:
    """Fixed VISUAL questions whose answer the candidate now states in NEW on-screen text.

    Showing the answer as a caption is not visual evidence; a caption can pass a probe while the
    footage still does not show the thing (a Curio lesson in the spec). Such fixes are excluded.
    """
    before = {c.text for c in baseline_plan.captions}
    new_texts = [c.text for c in candidate_plan.captions if c.text not in before]
    if not new_texts:
        return []
    flagged: list[str] = []
    for qid in fixed_question_ids:
        q = suite.question(qid)
        if q.modality != EvidenceModality.VISUAL:
            continue
        answer = next(o.text for o in q.options if o.id == q.correct_option_id)
        tokens = _content_tokens(answer)
        if truth is not None:
            for cid in q.claim_ids:
                try:
                    tokens |= _content_tokens(truth.claim(cid).text)
                except KeyError:
                    pass
        if not tokens:
            continue
        for text in new_texts:
            if len(_content_tokens(text) & tokens) >= max(2, int(0.5 * len(_content_tokens(answer)))):
                flagged.append(qid)
                break
    return flagged


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
