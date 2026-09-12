"""Evaluation-integrity rules that must never regress (no network, no media)."""

from __future__ import annotations

from directorloop.domain import (
    Caption,
    Comparison,
    CreativeBrief,
    EditPlan,
    EvaluationRun,
    EvaluationSuite,
    EvidenceModality,
    GoalProfile,
    Outcome,
    ProbeAnswer,
    ProbeModality,
    ProbeOption,
    ProbeQuestion,
    QuestionDelta,
    RepairAction,
    Segment,
)
from directorloop.evals.compare import text_assisted_fixes
from directorloop.evals.probes import run_probe_trials
from directorloop.evals.scoring import score_answers
from directorloop.planning.acceptance import decide_acceptance
from directorloop.providers.base import ProbeMedia, ProviderCapability


def _suite() -> EvaluationSuite:
    ns = ProbeOption(id="not_shown", text="Not shown")
    return EvaluationSuite(
        id="s",
        story_family="f",
        questions=[
            ProbeQuestion(
                id="q_visual", text="Where is the orange tab after locking?", modality=EvidenceModality.VISUAL,
                options=[ProbeOption(id="o1", text="Inside a slot in the base"), ProbeOption(id="o2", text="Above the panel"), ns],
                correct_option_id="o1",
            ),
            ProbeQuestion(
                id="q_audio", text="What is it made from?", modality=EvidenceModality.AUDIO,
                options=[ProbeOption(id="o1", text="A cereal box"), ProbeOption(id="o2", text="Wood"), ns],
                correct_option_id="o1", regression_guard=True,
            ),
        ],
    )


def test_scoring_excludes_errors_and_uses_majority() -> None:
    suite = _suite()
    answers = [
        ProbeAnswer(question_id="q_visual", trial=0, chosen_option_id="o1"),
        ProbeAnswer(question_id="q_visual", trial=1, chosen_option_id=None, error="timeout"),
        ProbeAnswer(question_id="q_visual", trial=2, chosen_option_id="o2"),
        ProbeAnswer(question_id="q_audio", trial=0, chosen_option_id="o1"),
        ProbeAnswer(question_id="q_audio", trial=1, chosen_option_id="o1"),
        ProbeAnswer(question_id="q_audio", trial=2, chosen_option_id="not_shown"),
    ]
    _, summaries, totals = score_answers(answers, suite)
    visual = next(s for s in summaries if s.question_id == "q_visual")
    audio = next(s for s in summaries if s.question_id == "q_audio")
    assert visual.valid_trials == 2 and visual.errors == 1 and visual.correct == 1
    assert visual.passed is False  # 1 of 2 valid is not a majority; the timeout is not counted as correct
    assert audio.passed is True
    assert totals.trials_errored == 1 and totals.questions_passed == 1


class _RecordingProvider:
    def __init__(self) -> None:
        self.capability = ProviderCapability(name="fake", role="probe", model="fake", modalities={"text", "image"})
        self.calls: list[list[str]] = []

    def answer_questions(self, media, questions, *, seed):  # noqa: ANN001
        self.calls.append([q.id for q in questions])
        assert all(not hasattr(q, "correct_option_id") for q in questions)
        return [ProbeAnswer(question_id=q.id, trial=seed, chosen_option_id=q.options[0].id) for q in questions]

    def judge_json(self, media, instruction, schema):  # noqa: ANN001
        raise NotImplementedError


def test_probe_questions_are_asked_independently() -> None:
    provider = _RecordingProvider()
    answers = run_probe_trials(provider, ProbeMedia(kind="none"), _suite(), trials=3)
    assert len(answers) == 6
    assert all(len(call) == 1 for call in provider.calls), "one question per call prevents cross-question leakage"
    assert len(provider.calls) == 6


def _plan(captions: list[Caption]) -> EditPlan:
    return EditPlan(segments=[Segment(id="s1", asset_id="a", source_in_ms=0, source_out_ms=9000)], captions=captions)


def test_caption_that_states_a_visual_answer_is_flagged() -> None:
    suite = _suite()
    base = _plan([Caption(id="c1", text="Cereal-box phone stand", start_ms=0, end_ms=2000)])
    cand = _plan(base.captions + [Caption(id="c2", text="The tab sits inside the slot in the base", start_ms=2000, end_ms=4000)])
    assert text_assisted_fixes(suite, None, base, cand, ["q_visual"]) == ["q_visual"]
    # an unrelated new caption does not flag the fix
    cand2 = _plan(base.captions + [Caption(id="c2", text="Holds when you tap", start_ms=2000, end_ms=4000)])
    assert text_assisted_fixes(suite, None, base, cand2, ["q_visual"]) == []
    # audio questions may legitimately be fixed by words
    assert text_assisted_fixes(suite, None, base, cand, ["q_audio"]) == []


def _run(version: str, passed: int) -> EvaluationRun:
    return EvaluationRun(
        id=f"e_{version}", version_id=version, artifact_hash=version, suite_id="s", suite_hash="h",
        probe_modality=ProbeModality.FRAMES_AND_TRANSCRIPT, provider="p", model="m", trials=3,
        questions_passed=passed, questions_total=2, trials_valid=6, score=passed / 2,
    )


def test_text_substituted_fix_is_not_promoted() -> None:
    brief = CreativeBrief(
        id="b", project_id="p", profile=GoalProfile.PRODUCT_DEMO, objective="o", target_duration_ms_min=1000,
        target_duration_ms_max=12000, allowed_actions=[RepairAction.REVISE_CAPTIONS],
    )
    comparison = Comparison(
        baseline_version_id="v0", candidate_version_id="v1", suite_hash="h", matched_config=True,
        deltas=[QuestionDelta(question_id="q_visual", before_passed=False, after_passed=True, before_rate=0.3, after_rate=1.0)],
        fixed=["q_visual"], regressed=[], kept=["q_audio"], still_failing=[], score_before=0.5, score_after=1.0,
        questions_passed_before=1, questions_passed_after=2, questions_total=2, duration_before_ms=9000,
        duration_after_ms=9000, mechanical_before=True, mechanical_after=True, constraints_before=True, constraints_after=True,
    )
    decision = decide_acceptance(comparison, _run("v1", 2), brief, text_assisted=["q_visual"])
    assert decision.outcome == Outcome.NEEDS_REVIEW
    assert decision.blocking_gate_id == "visual_fix_not_text_substitution"
    assert decision.fixed == []
    promoted = decide_acceptance(comparison, _run("v1", 2), brief, text_assisted=[])
    assert promoted.outcome == Outcome.PROMOTED
