"""No-media control: can the model answer the suite without seeing the video?

A question the model answers correctly with no media is weak evidence that the
video communicated it. Results are stored next to the suite, not inside it, so the
frozen suite hash is unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..domain.evaluation import LeakageControlResult
from ..domain.ids import utc_now_iso
from ..domain.truth import EvaluationSuite
from ..providers.base import MediaProbeProvider, ProbeMedia
from .probes import run_probe_trials
from .scoring import score_answers

WEAK_THRESHOLD = 0.5


def run_no_media_control(provider: MediaProbeProvider, suite: EvaluationSuite, trials: int = 3) -> LeakageControlResult:
    media = ProbeMedia(kind="none", duration_ms=0)
    answers = run_probe_trials(provider, media, suite, trials=trials, seed_base=5000)
    _, summaries, _ = score_answers(answers, suite)
    per_question = {s.question_id: s.pass_rate for s in summaries}
    chance = {q.id: 1.0 / max(1, len(q.options)) for q in suite.questions}
    weak = [qid for qid, rate in per_question.items() if rate >= WEAK_THRESHOLD]
    return LeakageControlResult(
        suite_hash=suite.content_hash(),
        provider=provider.capability.name,
        model=provider.capability.model,
        trials=trials,
        per_question=per_question,
        weak_question_ids=weak,
        chance_rate=chance,
        ran_at=utc_now_iso(),
    )


def apply_leakage_result(suite: EvaluationSuite, result: LeakageControlResult) -> EvaluationSuite:
    """Return a copy of the suite with leakage statuses annotated (hash of questions unchanged in spirit;
    the annotated copy is only used for reporting and question weighting decisions)."""
    annotated = suite.model_copy(deep=True)
    for q in annotated.questions:
        rate = result.per_question.get(q.id)
        if rate is None:
            continue
        q.leakage_status = "weak" if q.id in result.weak_question_ids else "strong"
        q.leakage_note = f"no-media control: {rate:.0%} correct over {result.trials} trials ({result.model})"
    return annotated


def save_leakage_result(result: LeakageControlResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.model_dump(mode="json"), indent=2), encoding="utf-8")


def load_leakage_result(path: Path) -> LeakageControlResult | None:
    if not path.exists():
        return None
    return LeakageControlResult.model_validate(json.loads(path.read_text(encoding="utf-8")))
