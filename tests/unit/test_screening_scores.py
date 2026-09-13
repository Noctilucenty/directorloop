"""Offline contracts for subjective scores; no audience calibration is asserted."""

import copy

import pytest
from pydantic import ValidationError

from directorloop.screening.models import (
    ScreenJudgment,
    ScreenPotentialScore,
    ScreenReport,
    ScreenReviewCheck,
    ScreenWindow,
)
from directorloop.screening.scoring import _score_statement, _sentence, build_scorecard


def window(start=0, end=2000, ratings=(3, 2, 1)):
    return ScreenWindow(
        start_ms=start,
        end_ms=end,
        status="complete",
        frame_timestamps_ms=[start + 100],
        judgment=ScreenJudgment(
            observations=[
                {
                    "kind": "visible_fact",
                    "text": "The same instruction remains visible across this section.",
                    "frame_timestamps_ms": [start + 100],
                    "asr_quote": None,
                }
            ],
            understanding="The instruction is shown.",
            attention_risk="medium",
            cause_observation_indices=[0],
            suggestion="The instruction remains visible.",
            moment_kind="development",
            uncertainties=[],
            potential_scores=[
                {
                    "dimension": d,
                    "rating": n,
                    "reason": "A specific strength is visible in this interval.",
                    "observation_indices": [0],
                }
                for d, n in zip(("creative", "retention", "virality"), ratings, strict=True)
            ],
        ),
    )


def report(windows=None, duration=6000, status="complete"):
    return ScreenReport(
        id="screen_scores",
        video_id="v",
        artifact_path="fixture.mp4",
        created_at="fixture",
        status=status,
        duration_ms=duration,
        windows=windows if windows is not None else [window(0, 2000), window(2000, 4000), window(4000, 6000)],
    )


def test_dimensions_remain_distinct_and_never_become_probabilities():
    card = build_scorecard(report())
    assert [card["metrics"][d]["score"] for d in ("creative", "retention", "virality")] == [75, 50, 25]
    assert card["evidence_level"] == "model_rubric" and card["predicts_audience_outcomes"] is False
    assert card["status"] == "complete"
    assert all(m["coverage"] == 1 for m in card["metrics"].values())


def test_duration_weights_do_not_penalize_length_or_average_windows_equally():
    short = report(
        [window(0, 1000, (4, 4, 4)), window(1000, 2000, (4, 4, 4)), window(2000, 10000, (0, 0, 0))], 10000
    )
    longer = report(
        [window(0, 2000, (4, 4, 4)), window(2000, 4000, (4, 4, 4)), window(4000, 20000, (0, 0, 0))], 20000
    )
    assert build_scorecard(short)["metrics"]["creative"]["score"] == 20
    assert build_scorecard(longer)["metrics"]["creative"]["score"] == 20


def test_missing_is_not_zero_and_insufficient_coverage_does_not_emit_a_score():
    r = report()
    r.windows[1].judgment.potential_scores = []
    r.windows[2].judgment.potential_scores = []
    m = build_scorecard(r)["metrics"]["creative"]
    assert m["score"] is None and m["rated_ms"] == 2000
    r = report([window(0, 6000, (0, 0, 0))])
    m = build_scorecard(r)["metrics"]["creative"]
    assert m["score"] == 0 and m["coverage"] == 1


def test_partial_score_shows_coverage_and_provisional_state():
    r = report([window(0, 2000), window(2000, 4000), window(4000, 6000), window(6000, 8000)], 8000)
    r.windows[-1].judgment.potential_scores = []
    card = build_scorecard(r)
    m = card["metrics"]["creative"]
    assert card["status"] == "partial" and m["score"] == 75 and m["coverage"] == 0.75 and m["provisional"]


@pytest.mark.parametrize("status", ["running", "failed", "canceled"])
def test_unfinished_or_failed_sessions_do_not_emit_final_scores(status):
    assert all(m["score"] is None for m in build_scorecard(report(status=status))["metrics"].values())


def test_legacy_report_does_not_invent_ratings_from_risk_or_clear_checks():
    r = report()
    for w in r.windows:
        w.judgment.potential_scores = []
    card = build_scorecard(r)
    assert card["status"] == "unavailable" and all(m["score"] is None for m in card["metrics"].values())


@pytest.mark.parametrize("rating", [True, False, 1.5, "3", -1, 5])
def test_scores_have_strict_bounded_ordinal_type(rating):
    with pytest.raises(ValidationError):
        ScreenPotentialScore(dimension="creative", rating=rating, reason="Evidence.", observation_indices=[0])


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_ref",
        "bad_citation",
        "inference_only",
        "duplicate_dimension",
        "question",
        "truncated",
        "pending_payoff",
    ],
)
def test_unusable_rating_evidence_fails_closed(mutation):
    r = report([window(0, 6000)])
    w = r.windows[0]
    s = w.judgment.potential_scores[0]
    if mutation == "missing_ref":
        s.observation_indices = [9]
    elif mutation == "bad_citation":
        w.validation_issues = ["Observation 1: frame citation was not supplied"]
    elif mutation == "inference_only":
        w.judgment.observations[0].kind = "inference"
    elif mutation == "duplicate_dimension":
        w.judgment.potential_scores[1].dimension = "creative"
    elif mutation == "question":
        s.reason = "Does the cut work?"
    elif mutation == "truncated":
        s.reason = "The cut is abrupt because"
    elif mutation == "pending_payoff":
        s.reason = "The promised payoff is not yet delivered."
    assert build_scorecard(r)["metrics"]["creative"]["score"] is None


def test_disputed_other_check_keeps_score_explicitly_provisional():
    r = report()
    r.windows[1].validation_issues = ["Review check hook_and_payoff: pending future payoff"]
    card = build_scorecard(r)
    assert card["metrics"]["creative"]["score"] == 75 and card["metrics"]["creative"]["provisional"]


def test_bad_timing_cannot_double_count_coverage():
    assert (
        build_scorecard(report([window(0, 4000), window(2000, 6000)]))["metrics"]["creative"]["score"] is None
    )
    assert build_scorecard(report(duration=0))["metrics"]["creative"]["score"] is None


def test_top_improvements_need_evidence_and_do_not_mutate_records():
    r = report()
    w = r.windows[0]
    w.judgment.review_checks = [
        ScreenReviewCheck(
            aspect="pacing",
            status="concern",
            reason="The same instruction is repeated without new information.",
            observation_indices=[0],
            suggested_change="Shorten the repeated instruction.",
        )
    ]
    # Validate the complete typed record, including the nested assigned fixture.
    r = ScreenReport.model_validate(r.model_dump())
    before = copy.deepcopy(r.model_dump())
    card = build_scorecard(r)
    assert card["improvements"][0]["action"] == "Shorten the repeated instruction."
    assert card["improvements"][0]["start_ms"] == 0
    assert r.model_dump() == before
    r.windows[0].validation_issues = [
        "Review check pacing: supporting observation failed evidence validation"
    ]
    assert build_scorecard(r)["improvements"] == []


def test_schema_requires_new_scores_only_for_full_review():
    from directorloop.screening.runner import FULL_SCREENING_SCHEMA, SCREENING_SCHEMA

    assert "potential_scores" in FULL_SCREENING_SCHEMA["required"]
    assert "potential_scores" not in SCREENING_SCHEMA["required"]
    assert FULL_SCREENING_SCHEMA["properties"]["potential_scores"]["minItems"] == 3


@pytest.mark.parametrize("reason", [
    "The visible file picker communicates the next step clearly",
    "The clear progression helps maintain interest in the geological explanation",
    "The combination of ground-level shots and aerial views provides a strong visual contrast",
])
def test_score_punctuation_is_optional_but_advice_punctuation_is_not(reason):
    assert _score_statement(reason)
    assert not _sentence(reason)
    r = report([window(0, 6000)])
    r.windows[0].judgment.potential_scores[0].reason = reason
    assert build_scorecard(r)["metrics"]["creative"]["score"] == 75
    assert r.windows[0].judgment.potential_scores[0].reason == reason


@pytest.mark.parametrize("reason", [
    "Does the cut make the information clearer",
    "How the cut makes the information clearer",
    "The cut is clear?", "The cut is clear…", "The cut is clear...",
    "The cut is abrupt because", "The cut is abrupt because the viewer",
    "The wide shot is clear, although the",
    "The cut is abrupt, which", "The wide shot provides",
    "The cut improves the", "The cut adds clarity but", "The cut adds clarity but.",
    "The cut adds clarity:", "The cut adds clarity—", "", None,
])
def test_score_formatting_tolerance_does_not_accept_questions_or_obvious_truncation(reason):
    assert not _score_statement(reason)


@pytest.mark.parametrize("reason", [
    "The visible comparison gives viewers a reason to continue watching to learn more.",
    "The layered reveal gives viewers a reason to discover more",
])
def test_complete_learn_more_and_discover_more_endings_are_not_truncated(reason):
    assert _score_statement(reason)
    r = report([window(0, 6000)])
    r.windows[0].judgment.potential_scores[1].reason = reason
    assert build_scorecard(r)["metrics"]["retention"]["score"] == 50


@pytest.mark.parametrize("reason", [
    "The first shot adds context compared to more.",
    "The shot invites the viewer to discover more because",
])
def test_other_dangling_comparisons_or_incomplete_more_clauses_stay_rejected(reason):
    assert not _score_statement(reason)
