"""Deterministic scoring of probe answers against the frozen suite.

Exact option-id matching. Errors and abstentions are counted separately and never
inflate accuracy. A question passes when a majority of its valid trials are correct.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from ..domain.evaluation import ProbeAnswer, ProbeResult, QuestionSummary
from ..domain.truth import EvaluationSuite


@dataclass
class ScoreTotals:
    questions_passed: int
    questions_total: int
    trials_correct: int
    trials_valid: int
    trials_errored: int
    score: float  # weighted fraction of questions passed


def score_answers(answers: list[ProbeAnswer], suite: EvaluationSuite) -> tuple[list[ProbeResult], list[QuestionSummary], ScoreTotals]:
    results: list[ProbeResult] = []
    by_question: dict[str, list[ProbeResult]] = {q.id: [] for q in suite.questions}
    for a in answers:
        if a.question_id not in by_question:
            continue
        q = suite.question(a.question_id)
        if a.error or a.chosen_option_id is None:
            r = ProbeResult(question_id=a.question_id, trial=a.trial, chosen_option_id=a.chosen_option_id, correct=None, error=a.error or "no answer", latency_ms=a.latency_ms)
        else:
            r = ProbeResult(question_id=a.question_id, trial=a.trial, chosen_option_id=a.chosen_option_id, correct=(a.chosen_option_id == q.correct_option_id), latency_ms=a.latency_ms)
        results.append(r)
        by_question[a.question_id].append(r)

    summaries: list[QuestionSummary] = []
    passed_weight = 0.0
    questions_passed = 0
    trials_correct = trials_valid = trials_errored = 0
    for q in suite.questions:
        rs = by_question[q.id]
        valid = [r for r in rs if r.correct is not None]
        errors = len(rs) - len(valid)
        correct = sum(1 for r in valid if r.correct)
        rate = correct / len(valid) if valid else 0.0
        passed = bool(valid) and correct * 2 > len(valid)
        chosen = Counter(r.chosen_option_id for r in rs if r.chosen_option_id)
        summaries.append(
            QuestionSummary(
                question_id=q.id,
                modality=str(q.modality),
                weight=q.weight,
                regression_guard=q.regression_guard,
                valid_trials=len(valid),
                errors=errors,
                correct=correct,
                pass_rate=rate,
                passed=passed,
                chosen=dict(chosen),
            )
        )
        if passed:
            passed_weight += q.weight
            questions_passed += 1
        trials_correct += correct
        trials_valid += len(valid)
        trials_errored += errors
    totals = ScoreTotals(
        questions_passed=questions_passed,
        questions_total=len(suite.questions),
        trials_correct=trials_correct,
        trials_valid=trials_valid,
        trials_errored=trials_errored,
        score=passed_weight / suite.total_weight(),
    )
    return results, summaries, totals
