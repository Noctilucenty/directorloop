"""Phrase tripwire regressions, not validation of model semantics or attention."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from directorloop.screening import runner
from directorloop.screening.boundary import BOUNDARY_VALIDATION_VERSION, boundary_validation_issues
from tests.unit.test_screening import Frames, Provider, reply

FRESH_V6_PENDING_REASONS = [
    "The speaker asks to read a verse, but the specific verse is not shown, creating a pending promise.",
    "The video sets up a reading of a specific verse but has not yet delivered the full text or a conclusion.",
    "The specific mention of 'struggling with lust' sets up a promise of advice that is not yet delivered.",
    "The video sets up a solution for 'lust' but the payoff is cut off at 'stop', leaving the specific benefit unexplained.",
]
FRESH_LIVE_CUTOFF_REASONS = [
    "The video sets up a Bible verse reading but does not deliver it yet.",
    "The video sets up a promise to help with lust but cuts off before delivering the specific solution or app details.",
]
LANDSCAPE_OPEN_LOOP_REASONS = [
    "The video sets up a claim about the oldest crust but has not yet revealed the specific location or significance.",
    "The video sets up a promise about an 'amazing thing' regarding the crust but cuts off before delivering the specific detail.",
    "The specific location has not yet been revealed.",
    "The video cuts off before explaining the geological significance.",
]


# Real phrases from the saved 31.637s full review. Its raw report remains untouched.
@pytest.mark.parametrize("reason", [
    "The video sets up a problem (running out of credits) but has not yet delivered a solution or reveal.",
    "The video sets up a promise about what the tool gives the user, but the payoff is not yet delivered by the 4.0s mark.",
    "The video promises 'unlimited' and 'free' generation but hasn't shown the actual result of a prompt yet.",
    "The video builds anticipation with 'here's the crazy part' but the interval ends before the specific feature is revealed.",
    "The video promises 'unlimited' and 'free' generation but ends mid-sentence without showing the final result or link.",
    "The video is a list of features that has not yet delivered a final summary or call to action.",
    "The payoff for the second board attempt is still pending.",
    "The promised result remains pending at this checkpoint.",
    "The reading has only reached the middle of the sentence by the cutoff.",
    "The video is mid-verse at the cutoff, leaving the thought unresolved.",
])
def test_live_intermediate_ending_or_pending_payoff_phrases_need_review(reason):
    judgment = reply(review_checks=[{
        "aspect": "hook_and_payoff", "status": "concern", "reason": reason, "observation_indices": [0],
    }])
    assert boundary_validation_issues(judgment, is_last_prefix=False)


@pytest.mark.parametrize("statement", [
    "The video ends before the feature is revealed.",
    "The clip ends mid-sentence.",
    "Does the video cut off too early before delivering the final call to action or link?",
    "It stops before the payoff.",
])
def test_explicit_source_ending_criticism_in_own_summary_or_question_is_flagged(statement):
    judgment = reply(suggestion=statement)
    issues = boundary_validation_issues(judgment, is_last_prefix=False)
    assert len(issues) == 1 and issues[0].startswith("Intermediate checkpoint:")


def test_observed_repetition_is_not_rejected_as_merely_pending_payoff():
    judgment = reply(observations=[{
        "kind": "caption_claim", "text": "The same slogan repeats across the two sampled moments.",
        "frame_timestamps_ms": [3000, 8000], "asr_quote": None,
    }], review_checks=[{
        "aspect": "hook_and_payoff", "status": "concern",
        "reason": "The same slogan repeats at 3s and 8s without new information, while the payoff is not yet delivered.",
        "observation_indices": [0],
    }])
    assert boundary_validation_issues(judgment, is_last_prefix=False) == []
    # A lone point cannot establish elapsed repeated material; this is only a tripwire exemption.
    judgment["observations"][0]["frame_timestamps_ms"] = [3000]
    assert boundary_validation_issues(judgment, is_last_prefix=False)


@pytest.mark.parametrize("reason", [
    "The same slogan repeats while the payoff is still pending.",
    "The same claim repeats and remains mid-sentence at the cutoff.",
])
def test_new_cutoff_phrases_preserve_cited_observed_repetition(reason):
    judgment = reply(observations=[{
        "kind": "caption_claim", "text": "The same slogan repeats in the sampled frames.",
        "frame_timestamps_ms": [3000, 8000], "asr_quote": None,
    }], review_checks=[{
        "aspect": "hook_and_payoff", "status": "concern", "reason": reason,
        "observation_indices": [0],
    }])
    assert boundary_validation_issues(judgment, is_last_prefix=False) == []


@pytest.mark.parametrize("reason", FRESH_V6_PENDING_REASONS + [
    "The advice has not yet been explained by this checkpoint.",
    "The promised content remains unresolved at this moment.",
    "The presenter leaves an undelivered promise.",
    "The full conclusion has not yet been read.",
])
def test_pending_word_families_require_review_only_for_intermediate_hook_concerns(reason):
    judgment = reply(review_checks=[{
        "aspect": "hook_and_payoff", "status": "concern", "reason": reason,
        "observation_indices": [0],
    }])
    before = copy.deepcopy(judgment)
    issues = boundary_validation_issues(judgment, is_last_prefix=False)
    assert len(issues) == 1 and issues[0].startswith("Review check hook_and_payoff:")
    assert boundary_validation_issues(judgment, is_last_prefix=True) == []
    assert boundary_validation_issues(judgment, is_last_prefix=False, coverage="quick") == []
    assert judgment == before
    for status in ["clear", "unknown"]:
        judgment["review_checks"][0]["status"] = status
        assert boundary_validation_issues(judgment, is_last_prefix=False) == []
    judgment["review_checks"][0].update(status="concern", aspect="visual_clarity")
    assert boundary_validation_issues(judgment, is_last_prefix=False) == []


@pytest.mark.parametrize("reason", FRESH_V6_PENDING_REASONS)
def test_pending_paraphrases_preserve_repeated_evidence_and_quoted_language(reason):
    judgment = reply(observations=[{
        "kind": "caption_claim", "text": "The same slogan repeats across the two sampled frames.",
        "frame_timestamps_ms": [3000, 8000], "asr_quote": None,
    }], review_checks=[{
        "aspect": "hook_and_payoff", "status": "concern", "reason": reason,
        "observation_indices": [0],
    }])
    assert boundary_validation_issues(judgment, is_last_prefix=False) == []
    judgment["observations"][0]["text"] = "A phrase appears in a caption."
    judgment["review_checks"][0]["reason"] = f'The displayed caption reads "{reason}".'
    assert boundary_validation_issues(judgment, is_last_prefix=False) == []


@pytest.mark.parametrize("reason", FRESH_LIVE_CUTOFF_REASONS + [
    "The video promises advice but doesn't deliver it yet.",
    "The promised answer hasn't been delivered yet.",
    "The presenter does not yet explain the advice.",
    "The full verse isn't being read yet.",
    "The conclusion has not been fully read yet.",
    "The source stops before showing the specific solution.",
    "The reading ends before the promised conclusion appears.",
])
def test_undelivered_yet_and_cutoff_before_future_payoff_paraphrases(reason):
    judgment = reply(review_checks=[{
        "aspect": "hook_and_payoff", "status": "concern", "reason": reason,
        "observation_indices": [0],
    }])
    before = copy.deepcopy(judgment)
    assert any(issue.startswith("Review check hook_and_payoff:")
               for issue in boundary_validation_issues(judgment, is_last_prefix=False))
    assert boundary_validation_issues(judgment, is_last_prefix=True) == []
    assert boundary_validation_issues(judgment, is_last_prefix=False, coverage="quick") == []
    assert judgment == before


@pytest.mark.parametrize("reason", FRESH_LIVE_CUTOFF_REASONS)
def test_new_cutoff_paraphrases_do_not_reject_true_repetition_or_quoted_content(reason):
    judgment = reply(observations=[{
        "kind": "caption_claim", "text": "The same slogan repeats in the sampled frames.",
        "frame_timestamps_ms": [3000, 8000], "asr_quote": None,
    }], review_checks=[{
        "aspect": "hook_and_payoff", "status": "concern", "reason": reason,
        "observation_indices": [0],
    }])
    assert boundary_validation_issues(judgment, is_last_prefix=False) == []
    judgment["observations"][0]["text"] = "A caption appears."
    judgment["review_checks"][0]["reason"] = f'The visible caption reads "{reason}".'
    assert boundary_validation_issues(judgment, is_last_prefix=False) == []


@pytest.mark.parametrize("reason", LANDSCAPE_OPEN_LOOP_REASONS)
def test_landscape_future_details_are_not_missing_at_an_intermediate_prefix(reason):
    judgment = reply(review_checks=[{
        "aspect": "hook_and_payoff", "status": "concern", "reason": reason,
        "observation_indices": [0],
    }])
    before = copy.deepcopy(judgment)
    assert any(issue.startswith("Review check hook_and_payoff:")
               for issue in boundary_validation_issues(judgment, is_last_prefix=False))
    assert boundary_validation_issues(judgment, is_last_prefix=True) == []
    assert boundary_validation_issues(judgment, is_last_prefix=False, coverage="quick") == []
    assert judgment == before


@pytest.mark.parametrize("reason", LANDSCAPE_OPEN_LOOP_REASONS[:2])
def test_landscape_open_loop_words_preserve_literal_quotes_and_cited_repetition(reason):
    judgment = reply(observations=[{
        "kind": "caption_claim", "text": "The same claim repeats across the two sampled frames.",
        "frame_timestamps_ms": [3000, 8000], "asr_quote": None,
    }], review_checks=[{
        "aspect": "hook_and_payoff", "status": "concern", "reason": reason,
        "observation_indices": [0],
    }])
    assert boundary_validation_issues(judgment, is_last_prefix=False) == []
    judgment["observations"][0]["text"] = "A literal quotation appears in the caption."
    judgment["review_checks"][0]["reason"] = f'The caption reads "{reason}".'
    assert boundary_validation_issues(judgment, is_last_prefix=False) == []


def test_saved_pool_scene_cut_to_black_is_not_a_prefix_cutoff_claim():
    # Minimal immutable text fixture from screen_1a09c147f41_83bb2a07, 2-4s.
    # These anchors do not prove a missing landing; they preserve this distinct
    # observed transition concern for the separate evidence/semantic checks.
    judgment = reply(
        understanding="A person performs a high flip from a pergola into a pool. The video ends on a black frame.",
        suggestion="The video captures a high-energy flip from a pergola into a pool, ending with a black frame.",
        observations=[{
            "kind": "visible_fact", "text": "A person is in mid-air over the pool.",
            "frame_timestamps_ms": [2166, 2500, 3166, 3500], "asr_quote": None,
        }, {
            "kind": "visible_fact", "text": "The video ends with a completely black frame at the 3833ms mark.",
            "frame_timestamps_ms": [3833], "asr_quote": None,
        }],
        review_checks=[{
            "aspect": "hook_and_payoff", "status": "concern",
            "reason": "The video cuts to black before the flip's landing is shown.",
            "observation_indices": [1],
        }],
    )
    assert boundary_validation_issues(judgment, is_last_prefix=False) == []


@pytest.mark.parametrize("reason", [
    "The video sets up a transformation promise but the final result is not yet visible at this cutoff.",
    "The promised result isn't yet fully visible by the current checkpoint.",
    "The final result is not yet shown at this cutoff.",
])
def test_unseen_future_result_at_explicit_cutoff_is_not_an_observed_failure(reason):
    judgment = reply(review_checks=[{
        "aspect": "hook_and_payoff", "status": "concern", "reason": reason,
        "observation_indices": [0],
    }])
    before = copy.deepcopy(judgment)
    assert boundary_validation_issues(judgment, is_last_prefix=False)
    assert boundary_validation_issues(judgment, is_last_prefix=True) == []
    assert boundary_validation_issues(judgment, is_last_prefix=False, coverage="quick") == []
    assert judgment == before
    judgment["review_checks"][0]["status"] = "unknown"
    assert boundary_validation_issues(judgment, is_last_prefix=False) == []
    judgment["review_checks"][0].update(status="concern", aspect="visual_clarity")
    assert boundary_validation_issues(judgment, is_last_prefix=False) == []
    judgment["review_checks"][0].update(aspect="hook_and_payoff", reason=f'The caption reads "{reason}".')
    assert boundary_validation_issues(judgment, is_last_prefix=False) == []


def test_occluded_result_without_future_cutoff_claim_is_left_to_evidence_checks():
    judgment = reply(review_checks=[{
        "aspect": "hook_and_payoff", "status": "concern",
        "reason": "The final result is not visible because the host's hand covers it in the sampled frames.",
        "observation_indices": [0],
    }])
    assert boundary_validation_issues(judgment, is_last_prefix=False) == []


@pytest.mark.parametrize("reason", [
    "The video sets up a promise of 'beautiful animations' but the current sentence is cut off at 'creators'.",
    "The current utterance gets cut short before the explanation.",
    "The current phrase is cut off midway through the explanation.",
    "The sentence ends at this checkpoint without its next clause.",
    "The utterance stops at the evidence cutoff.",
    "The phrase is cut off at the current prefix boundary.",
])
def test_current_sentence_cutoff_is_not_an_editable_source_defect(reason):
    judgment = reply(review_checks=[{
        "aspect": "hook_and_payoff", "status": "concern", "reason": reason,
        "observation_indices": [0],
    }])
    before = copy.deepcopy(judgment)
    assert boundary_validation_issues(judgment, is_last_prefix=False)
    assert boundary_validation_issues(judgment, is_last_prefix=True) == []
    assert judgment == before
    judgment["review_checks"][0]["status"] = "unknown"
    assert boundary_validation_issues(judgment, is_last_prefix=False) == []
    judgment["review_checks"][0].update(status="concern", reason=f'The caption reads "{reason}".')
    assert boundary_validation_issues(judgment, is_last_prefix=False) == []


@pytest.mark.parametrize("reason", [
    "The current sentence ends with unfamiliar jargon that competes with the chart.",
    "The phrase ends with a vague pronoun instead of naming the product.",
    "The video cuts to black before the flip's landing is shown.",
])
def test_sentence_or_scene_ending_without_a_prefix_claim_is_not_rejected(reason):
    judgment = reply(review_checks=[{
        "aspect": "hook_and_payoff", "status": "concern", "reason": reason,
        "observation_indices": [0],
    }])
    assert boundary_validation_issues(judgment, is_last_prefix=False) == []


@pytest.mark.parametrize("reason", [
    'The caption says "The payoff is still pending" beside the invoice.',
    "The reader says ‘middle of the sentence by the cutoff’ as an example.",
    "The source's first chapter ends mid-verse, and the next chapter starts immediately.",
    "The requested payment is still pending. The actual payoff is shown.",
    "The reader has reached the middle of the sentence and continues at a steady pace.",
    "The unfamiliar middle of the verse introduces several new terms together.",
    "There is no pending promise because the answer was given.",
    "The advice is no longer pending; the speaker names three concrete steps.",
    "The title is not a pending promise, but an already answered question.",
    "The conclusion is visible but its white text blends into a white background.",
    "The text is cut off at the right edge of the sampled frame.",
    "The advice contradicts the earlier caption.",
])
def test_pending_or_sentence_words_without_a_cutoff_defect_are_not_flagged(reason):
    judgment = reply(review_checks=[{
        "aspect": "hook_and_payoff", "status": "concern", "reason": reason,
        "observation_indices": [0],
    }])
    assert boundary_validation_issues(judgment, is_last_prefix=False) == []


@pytest.mark.parametrize("status", ["clear", "unknown"])
def test_neutral_checkpoint_context_is_not_promoted_to_a_concern(status):
    judgment = reply(review_checks=[{
        "aspect": "hook_and_payoff", "status": status,
        "reason": "The video is mid-verse at the cutoff, so the later payoff remains unknown.",
        "observation_indices": [],
    }])
    assert boundary_validation_issues(judgment, is_last_prefix=False) == []


@pytest.mark.parametrize("reason", [
    "The payoff is still pending.",
    "The reading has only reached the middle of the sentence by the cutoff.",
    "The video is mid-verse at the cutoff, leaving the thought unresolved.",
])
def test_new_phrases_leave_final_ending_judgments_and_raw_records_unchanged(reason):
    judgment = reply(review_checks=[{
        "aspect": "hook_and_payoff", "status": "concern", "reason": reason,
        "observation_indices": [0],
    }])
    before = copy.deepcopy(judgment)
    assert boundary_validation_issues(judgment, is_last_prefix=True) == []
    assert boundary_validation_issues(judgment, is_last_prefix=False, coverage="quick") == []
    assert boundary_validation_issues(judgment, is_last_prefix=False)
    assert judgment == before


def test_quoted_words_are_not_treated_as_the_reviewers_own_ending_claim():
    judgment = reply(
        understanding='A caption reads "The video ends before the payoff."',
        observations=[{
            "kind": "caption_claim", "text": "On-screen words read: the video ends mid-sentence.",
            "frame_timestamps_ms": [166], "asr_quote": None,
        }],
    )
    assert boundary_validation_issues(judgment, is_last_prefix=False) == []


def test_future_expectation_is_not_an_observed_defect():
    judgment = reply(suggestion="What could be revealed after this cutoff?", review_checks=[{
        "aspect": "hook_and_payoff", "status": "unknown",
        "reason": "The payoff is not yet delivered; its later content remains unknown.", "observation_indices": [],
    }])
    assert boundary_validation_issues(judgment, is_last_prefix=False) == []


def test_final_and_quick_reviews_unchanged_and_input_never_mutated():
    judgment = reply(understanding="The video ends mid-sentence.")
    before = copy.deepcopy(judgment)
    assert boundary_validation_issues(judgment, is_last_prefix=True) == []
    assert boundary_validation_issues(judgment, is_last_prefix=False, coverage="quick") == []
    assert boundary_validation_issues(judgment, is_last_prefix=False)
    assert judgment == before
    assert BOUNDARY_VALIDATION_VERSION == "intermediate-boundary-phrases-v7"


def test_full_runner_marks_risk_provisional_without_rewriting_raw_output(tmp_path, monkeypatch):
    source, data_dir = tmp_path / "source.mp4", tmp_path / "data"
    source.write_bytes(b"immutable synthetic media")
    monkeypatch.setattr(runner, "inspect_media", lambda _: SimpleNamespace(
        duration_ms=6000, fps=30, width=448, height=796, has_audio=False))
    monkeypatch.setattr(runner, "FrameCache", Frames)
    provider = Provider(lambda _index, media: reply(
        media.frames[-1].timestamp_ms,
        attention_risk="medium", cause_observation_indices=[0],
        suggestion="Does the video cut off too early before the final call to action?",
        review_checks=[{
            "aspect": aspect, "status": "unknown", "reason": "Evidence is insufficient.", "observation_indices": [],
        } for aspect in sorted(runner.FULL_REVIEW_ASPECTS)],
    ))
    report = runner.run_screening("video", source, provider, data_dir, coverage="full")
    assert report.status == "needs_review"
    assert all(window.status == "needs_review" for window in report.windows[:-1])
    assert report.windows[-1].status == "complete"
    assert all(window.judgment.attention_risk == "medium" for window in report.windows)
    assert all(window.raw_output["attention_risk"] == "medium" for window in report.windows)
    assert report.protocol["boundary_validation"]["semantic_grounding_verified"] is False
    assert report.protocol["boundary_code_sha256"]
