"""Free, evidence-linked explanations and balanced result summaries."""

import copy

import pytest

from directorloop.screening.models import ScreenReviewCheck
from directorloop.screening.scoring import build_scorecard
from tests.unit.test_screening_scores import report, window


def check(aspect="pacing", status="clear", reason="The instruction advances clearly through three visible steps.", refs=None):
    return ScreenReviewCheck(aspect=aspect, status=status, reason=reason,
                             observation_indices=[0] if refs is None else refs)


def test_lower_score_explains_duration_weighted_lower_rated_content_without_rewriting_evidence():
    r = report([window(0, 1000, (0, 0, 0)), window(1000, 2000, (4, 4, 4)),
                window(2000, 10000, (2, 2, 2))], 10000)
    reasons = ["The opening instruction is obscured by the title.",
               "The second step clearly reveals the finished result.",
               "The repeated middle instruction adds little new information"]
    for w, reason in zip(r.windows, reasons, strict=True):
        for score in w.judgment.potential_scores:
            score.reason = reason
    before = copy.deepcopy(r.model_dump())
    card = build_scorecard(r)
    for metric in card["metrics"].values():
        assert metric["score"] == 50
        assert metric["explanation"] == reasons[2] + "."
        assert metric["drivers"][0] == {
            "window_index": 2, "start_ms": 2000, "end_ms": 10000,
            "reason": reasons[2] + ".", "observation_indices": [0],
        }
        assert metric["drivers"][1]["window_index"] == 0
    assert r.model_dump() == before


def test_high_score_explains_largest_supported_strength_and_deduplicates_identical_reasons():
    r = report([window(0, 1000, (4, 4, 4)), window(1000, 2000, (4, 4, 4)),
                window(2000, 10000, (3, 3, 3))], 10000)
    for score in r.windows[-1].judgment.potential_scores:
        score.reason = "The final comparison clearly shows how each step changes the result."
    metric = build_scorecard(r)["metrics"]["creative"]
    assert metric["score"] == 80
    assert metric["drivers"][0]["window_index"] == 2
    assert metric["explanation"] == r.windows[-1].judgment.potential_scores[0].reason
    assert len(metric["drivers"]) == 2


def test_insufficient_evidence_has_no_explanatory_driver_or_fabricated_number():
    r = report()
    r.windows[1].judgment.potential_scores = []
    r.windows[2].judgment.potential_scores = []
    metric = build_scorecard(r)["metrics"]["creative"]
    assert metric["score"] is None and metric["drivers"] == []
    assert "More reviewed evidence" in metric["explanation"]


def test_strengths_and_improvements_are_distinct_grounded_findings_without_forcing_balance():
    r = report()
    r.windows[0].judgment.review_checks = [check(), check("voice_delivery")]
    r.windows[1].judgment.review_checks = [check("visual_clarity", "unknown", "The main subject is hidden behind the overlay.")]
    r.windows[2].judgment.review_checks = [check("pacing", "concern", "The instruction repeats without adding any new information.")]
    card = build_scorecard(r)
    assert len(card["strengths"]) == 1 and card["strengths"][0]["window_index"] == 0
    assert card["strengths"][0]["observation_indices"] == [0]
    assert len(card["improvements"]) == 1 and card["improvements"][0]["window_index"] == 2
    assert card["evidence_level"] == "model_rubric" and card["predicts_audience_outcomes"] is False
    r.windows[2].judgment.review_checks = []
    assert build_scorecard(r)["improvements"] == []
    r.windows[0].judgment.review_checks = []
    assert build_scorecard(r)["strengths"] == []


@pytest.mark.parametrize("mutation", ["bad_ref", "inference", "failed_check", "bad_observation", "truncated", "question"])
def test_strengths_never_promote_unsupported_or_incomplete_checks(mutation):
    r = report()
    w = r.windows[0]
    w.judgment.review_checks = [check()]
    if mutation == "bad_ref":
        w.judgment.review_checks[0].observation_indices = [3]
    elif mutation == "inference":
        w.judgment.observations[0].kind = "inference"
    elif mutation == "failed_check":
        w.validation_issues = ["Review check pacing: supporting observation failed evidence validation"]
    elif mutation == "bad_observation":
        w.validation_issues = ["Observation 1: frame citation was not supplied"]
    elif mutation == "truncated":
        w.judgment.review_checks[0].reason = "The progression is clear because"
    else:
        w.judgment.review_checks[0].reason = "Does this progression explain the next step?"
    assert build_scorecard(r)["strengths"] == []


@pytest.mark.parametrize("aspect", ["pacing", "visual_clarity", "caption_readability", "caption_alignment",
                                    "hook_and_payoff", "tone_from_words", "share_motivation"])
@pytest.mark.parametrize("reason", ["The promised result has not yet been revealed.",
                                    "The explanation is incomplete at this prefix.",
                                    "The video sets up a claim about the oldest crust but has not yet revealed the specific location or significance.",
                                    "The video promises an amazing detail about the crust but cuts off before delivering the specific detail."])
def test_cutoff_only_criticism_is_not_published_as_a_weakness_in_any_aspect(aspect, reason):
    r = report()
    r.windows[0].judgment.review_checks = [check(aspect, "concern", reason)]
    assert build_scorecard(r)["improvements"] == []


def test_actual_final_missing_payoff_can_remain_a_concern():
    r = report()
    r.windows[-1].is_last_prefix = True
    r.windows[-1].judgment.review_checks = [check("hook_and_payoff", "concern", "The promised result is never shown in the finished video.")]
    assert len(build_scorecard(r)["improvements"]) == 1


def test_supported_repetition_is_not_discarded_just_for_mentioning_an_unfinished_payoff():
    r = report()
    w = r.windows[0]
    w.judgment.observations[0].text = "The same instruction repeats across two sampled frames."
    w.judgment.observations[0].frame_timestamps_ms = [100, 1500]
    w.frame_timestamps_ms = [100, 1500]
    w.judgment.review_checks = [check("pacing", "concern", "The same instruction repeats while the promised result is still pending.")]
    assert len(build_scorecard(r)["improvements"]) == 1


@pytest.mark.parametrize("reason", [
    "The religious niche limits broad viral spread.",
    "The political topic makes sharing unlikely for a broad audience.",
    "The content's niche appeal restricts its potential reach.",
    "The content is educational and specific to a geological location, which limits its broad shareability compared to more dramatic or relatable topics.",
    "The specific geological topic may limit its broad shareability compared to more general travel content.",
    "The specific geological fact may not have a broad enough appeal for high virality without more context.",
    "The content is interesting but niche, offering a moderate share motive for nature and science enthusiasts.",
    "The content is interesting but likely appeals to a specific interest in geology rather than a broad audience.",
])
def test_distribution_assumptions_do_not_become_public_reasons_but_recorded_ordinal_scores_survive(reason):
    r = report()
    for w in r.windows:
        w.judgment.potential_scores[-1].reason = reason
        w.judgment.review_checks = [check("share_motivation", "concern", reason)]
    before = copy.deepcopy(r.model_dump())
    card = build_scorecard(r)
    assert card["metrics"]["virality"]["score"] == 25
    assert card["metrics"]["virality"]["drivers"] == []
    assert card["metrics"]["virality"]["explanation"] == "Sharing appeal is not established by the available explanation."
    assert card["improvements"] == [] and card["strengths"] == []
    assert r.model_dump() == before


def test_specific_subject_matter_can_have_a_grounded_sharing_strength():
    r = report()
    reason = "The religious verse provides a specific reminder about helping another person."
    for w in r.windows:
        w.judgment.potential_scores[-1].reason = reason
        w.judgment.potential_scores[-1].rating = 3
    r.windows[0].judgment.review_checks = [check("share_motivation", "clear", reason)]
    card = build_scorecard(r)
    assert card["metrics"]["virality"]["score"] == 75
    assert card["metrics"]["virality"]["explanation"] == reason
    assert card["strengths"][0]["reason"] == reason


def test_equal_low_ratings_prefer_an_existing_ordinary_motive_explanation_without_changing_scores():
    r = report([window(0, 1000, (3, 2, 2)), window(1000, 2000, (3, 2, 2)),
                window(2000, 10000, (3, 2, 2))], 10000)
    ordinary = "The fact-based explanation is a common but standard format for sharing."
    r.windows[0].judgment.potential_scores[-1].reason = ordinary
    r.windows[2].judgment.potential_scores[-1].reason = "The striking landscape offers a plausible reason for sharing."
    before = copy.deepcopy(r.model_dump())
    metric = build_scorecard(r)["metrics"]["virality"]
    assert metric["score"] == 50
    assert metric["explanation"] == ordinary and metric["drivers"][0]["window_index"] == 0
    assert r.model_dump() == before


def test_timeline_shows_real_per_section_retention_ratings_without_smoothing_or_changing_total():
    r = report([window(0, 1000, (4, 3, 2)), window(1000, 2000, (3, 2, 1)),
                window(2000, 10000, (2, 0, 0))], 10000)
    before = copy.deepcopy(r.model_dump())
    card = build_scorecard(r)
    assert [row["score"] for row in card["timeline"]] == [75, 50, 0]
    assert card["metrics"]["retention"]["score"] == 13
    for index, row in enumerate(card["timeline"]):
        assert row == {"window_index": index, "start_ms": r.windows[index].start_ms,
                       "end_ms": r.windows[index].end_ms, "score": [75, 50, 0][index],
                       "reason": r.windows[index].judgment.potential_scores[1].reason,
                       "observation_indices": [0]}
    assert r.model_dump() == before


@pytest.mark.parametrize("missing", ["null_rating", "missing_rating", "bad_ref", "truncated", "pending", "unsupported_copy", "unfinished"])
def test_timeline_preserves_unknown_sections_instead_of_defaulting_to_zero_or_interpolating(missing):
    r = report()
    w = r.windows[1]
    s = w.judgment.potential_scores[1]
    if missing == "null_rating":
        s.rating = None
    elif missing == "missing_rating":
        w.judgment.potential_scores = [x for x in w.judgment.potential_scores if x.dimension != "retention"]
    elif missing == "bad_ref":
        s.observation_indices = [8]
    elif missing == "truncated":
        s.reason = "The walking shot becomes repetitive because"
    elif missing == "pending":
        s.reason = "The promised result is not yet revealed."
    elif missing == "unsupported_copy":
        s.reason = "The specific geological topic limits broad shareability compared to general travel content."
    else:
        w.status = "pending"
    card = build_scorecard(r)
    assert [row["score"] for row in card["timeline"]] == [50, None, 50]
    assert card["timeline"][1] == {"window_index": 1, "start_ms": 2000, "end_ms": 4000,
                                   "score": None, "reason": None, "observation_indices": []}


def test_timeline_can_truthfully_remain_flat_when_admitted_ratings_are_equal():
    card = build_scorecard(report())
    assert [row["score"] for row in card["timeline"]] == [50, 50, 50]
    assert card["predicts_audience_outcomes"] is False


def test_invalid_overlapping_windows_do_not_produce_a_misleading_timeline():
    assert build_scorecard(report([window(0, 4000), window(2000, 6000)]))["timeline"] == []
