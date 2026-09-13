"""Offline admission regressions; supplied-anchor membership is not semantic truth."""

from __future__ import annotations

import copy

import pytest

from directorloop.screening.attention_admission import assess_attention
from directorloop.screening.models import ScreenWindow

CAPTION_ISSUES = [
    "Review check caption_readability: readable caption evidence missing",
    "Review check caption_alignment: readable caption evidence missing",
]
PENDING_ISSUE = "Review check hook_and_payoff: pending future payoff is not by itself an observed defect at an intermediate checkpoint"


def window(**updates):
    data = {
        "start_ms": 4000, "end_ms": 7856, "status": "needs_review",
        "frame_timestamps_ms": [4160, 6410, 7695],
        "prefix_asr_text": "First, open, click.",
        "validation_issues": list(CAPTION_ISSUES),
        "judgment": {
            "observations": [
                {"kind": "visible_fact", "text": "Animated logos appear above the speaker.",
                 "frame_timestamps_ms": [4160], "asr_quote": None},
                {"kind": "asr_claim", "text": "The narrator says to open and click.",
                 "frame_timestamps_ms": [], "asr_quote": "First, open, click."},
                {"kind": "visible_fact", "text": "A software interface displays an upload button.",
                 "frame_timestamps_ms": [6410, 7695], "asr_quote": None},
            ],
            "understanding": "The tutorial begins with an upload step.",
            "attention_risk": "low", "cause_observation_indices": [0, 1, 2],
            "suggestion": "The video moves from an example to its first tutorial step.",
            "moment_kind": "development", "uncertainties": ["The software name is not visible."],
        },
    }
    data.update(updates)
    return ScreenWindow.model_validate(data)


def test_unrelated_caption_failure_does_not_erase_an_anchored_attention_judgment():
    item = window()
    before = item.model_dump_json()
    assessment = assess_attention(item)
    assert assessment == {
        "version": "attention-evidence-v1", "status": "supported", "risk": "low",
        "reason": "AI attention estimate uses valid source references; subtitle checks remain unavailable.",
        "excluded_checks": ["caption_alignment", "caption_readability"],
        "semantic_grounding_verified": False,
    }
    assert item.model_dump_json() == before
    assert item.status == "needs_review"
    assert item.judgment.uncertainties == ["The software name is not visible."]


@pytest.mark.parametrize("index,causes,inference,pending,expected", [
    (2, [0, 1, 2], False, False, "supported"),
    (4, [0, 1, 2], False, True, "blocked"),
    (5, [1, 2], False, False, "supported"),
    (6, [0, 2], True, False, "blocked"),
])
def test_latest_four_window_failure_shapes(index, causes, inference, pending, expected):
    # Minimal shapes from screen_1a09c1900c1_8151ae1a; no private files needed.
    item = window()
    item.judgment.cause_observation_indices = causes
    if inference:
        item.judgment.observations[2].kind = "inference"
        item.judgment.attention_risk = "medium"
    if pending:
        item.validation_issues.append(PENDING_ISSUE)
    before = item.model_dump_json()
    assessment = assess_attention(item)
    assert assessment["status"] == expected, index
    assert assessment["risk"] == (item.judgment.attention_risk if expected == "supported" else "unknown")
    assert assessment["semantic_grounding_verified"] is False
    assert item.model_dump_json() == before


@pytest.mark.parametrize("issue", [
    "Observation 1: frame citation was not supplied",
    "Observation 3: quote is absent from the supplied ASR prefix",
    "Attention label has no supporting observation",
    "Attention explanation repeats an observation reference",
    "Intermediate checkpoint: wording may mistake the review cutoff for the video ending; inspect the evidence",
    PENDING_ISSUE,
    "Review check caption_alignment: supporting observation failed evidence validation",
    "Review check tone_from_words: quoted or readable wording missing",
    "The review checklist is incomplete",
    "Model output did not match the screening schema",
    "Unrecognized future validator failure",
])
def test_only_exact_unrelated_caption_modality_failures_can_be_excluded(issue):
    assessment = assess_attention(window(validation_issues=[*CAPTION_ISSUES, issue]))
    assert assessment["status"] == "blocked"
    assert assessment["risk"] == "unknown"
    assert assessment["excluded_checks"] == ["caption_alignment", "caption_readability"]


def test_caption_alignment_transcript_missing_can_be_excluded_for_visual_only_attention():
    item = window(validation_issues=["Review check caption_alignment: matching transcript evidence missing"])
    item.judgment.cause_observation_indices = [0, 2]
    item.prefix_asr_text = ""
    assessment = assess_attention(item)
    assert assessment["status"] == "supported"
    assert assessment["excluded_checks"] == ["caption_alignment"]


@pytest.mark.parametrize("causes", [[], [0, 0], [-1], [3], [0, 99]])
@pytest.mark.parametrize("status,issues", [("complete", []), ("needs_review", CAPTION_ISSUES)])
def test_legacy_status_does_not_grandfather_missing_or_invalid_causes(causes, status, issues):
    item = window(status=status, validation_issues=issues)
    item.judgment.cause_observation_indices = causes
    assert assess_attention(item)["status"] == "blocked"


@pytest.mark.parametrize("kind", ["unknown", "inference"])
@pytest.mark.parametrize("status,issues", [("complete", []), ("needs_review", CAPTION_ISSUES)])
def test_every_cause_must_be_concrete_even_if_another_cause_has_an_anchor(kind, status, issues):
    item = window(status=status, validation_issues=issues)
    item.judgment.observations[2].kind = kind
    assert assess_attention(item)["status"] == "blocked"


@pytest.mark.parametrize("frames", [[], [9999], [4160, 4160], [-1]])
def test_invalid_or_missing_image_anchors_block_even_without_saved_observation_errors(frames):
    item = window(status="complete", validation_issues=[])
    item.judgment.observations[0].frame_timestamps_ms = frames
    assert assess_attention(item)["status"] == "blocked"


@pytest.mark.parametrize("quote", [None, "", "Future words.", "first, open, click.", "First open click", "…"])
def test_invalid_or_changed_transcript_reference_blocks_even_without_saved_errors(quote):
    item = window(status="complete", validation_issues=[])
    item.judgment.observations[1].asr_quote = quote
    assert assess_attention(item)["status"] == "blocked"


def test_image_and_transcript_claims_cannot_borrow_each_others_anchors():
    item = window()
    item.judgment.observations[0].asr_quote = "First, open, click."
    assert assess_attention(item)["status"] == "blocked"
    item = window()
    item.judgment.observations[1].frame_timestamps_ms = [4160]
    assert assess_attention(item)["status"] == "blocked"


@pytest.mark.parametrize("risk", ["low", "medium", "high"])
def test_clean_completed_concrete_evidence_retains_the_original_model_label(risk):
    item = window(status="complete", validation_issues=[])
    item.judgment.attention_risk = risk
    assessment = assess_attention(item)
    assert assessment["status"] == "supported" and assessment["risk"] == risk
    assert assessment["excluded_checks"] == []
    assert assessment["semantic_grounding_verified"] is False


@pytest.mark.parametrize("updates", [
    {"status": "pending"}, {"status": "failed"}, {"status": "not_attempted"},
    {"judgment": None}, {"status": "needs_review", "validation_issues": []},
    {"status": "complete", "validation_issues": CAPTION_ISSUES},
    {"error": "Provider failed"}, {"start_ms": 7856},
])
def test_missing_inconsistent_or_unfinished_review_never_becomes_supported(updates):
    item = window(**copy.deepcopy(updates))
    assessment = assess_attention(item)
    assert assessment["status"] == "blocked" and assessment["risk"] == "unknown"


def test_unknown_model_label_is_never_filled_in():
    item = window()
    item.judgment.attention_risk = "unknown"
    assert assess_attention(item)["risk"] == "unknown"
    assert assess_attention(item)["status"] == "blocked"
