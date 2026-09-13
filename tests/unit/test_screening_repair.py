"""One-pass repair contracts with a local provider stub and immutable input evidence."""

from __future__ import annotations

import copy
from dataclasses import replace

import pytest

from directorloop.audit.models import InspectedWindow
from directorloop.audit.review import ViewerPayload
from directorloop.domain.ids import sha256_bytes, sha256_json
from directorloop.media.frames import SampledFrame
from directorloop.providers.base import CompletionResult, ProbeMedia, ProviderError
from directorloop.screening.models import ScreenFrame, ScreenJudgment, ScreenWindow
from directorloop.screening.repair import focused_repair_screen_window, repair_needed, repair_screen_window


def judgment():
    return {
        "observations": [
            {"kind": "visible_fact", "text": "A logo is shown in the opening.", "frame_timestamps_ms": [500], "asr_quote": None},
            {"kind": "visible_fact", "text": "The current screen displays a file picker.", "frame_timestamps_ms": [2500, 3500], "asr_quote": None},
            {"kind": "caption_claim", "text": "The caption reads Choose a file.", "frame_timestamps_ms": [2500], "asr_quote": None},
            {"kind": "asr_claim", "text": "The narrator asks for a file.", "frame_timestamps_ms": [], "asr_quote": "Choose a file."},
        ],
        "understanding": "The tutorial shows how to choose a file.",
        "attention_risk": "low", "cause_observation_indices": [1, 3],
        "suggestion": "The file picker makes the next step clear.", "moment_kind": "development",
        "uncertainties": ["Native vocal delivery is unavailable."],
        "review_checks": [
            {"aspect": "pacing", "status": "clear", "reason": "The current interval adds a concrete upload step.", "observation_indices": [1]},
            {"aspect": "visual_clarity", "status": "clear", "reason": "The file picker is visible.", "observation_indices": [1]},
            {"aspect": "caption_readability", "status": "clear", "reason": "The caption is legible in the supplied frame.", "observation_indices": [2]},
            {"aspect": "caption_alignment", "status": "clear", "reason": "The caption repeats the supplied spoken words.", "observation_indices": [2, 3]},
            {"aspect": "hook_and_payoff", "status": "clear", "reason": "The tutorial begins its promised upload step.", "observation_indices": [1]},
            {"aspect": "tone_from_words", "status": "clear", "reason": "The words give a direct instruction.", "observation_indices": [3]},
            {"aspect": "voice_delivery", "status": "unknown", "reason": "Native audio is unavailable.", "observation_indices": []},
            {"aspect": "share_motivation", "status": "clear", "reason": "The upload step may help someone using the same interface.", "observation_indices": [1, 3]},
        ],
        "potential_scores": [
            {"dimension": "creative", "rating": 3, "reason": "The visible file picker communicates the next step clearly.", "observation_indices": [1]},
            {"dimension": "retention", "rating": 3, "reason": "The file picker adds progress toward the tutorial goal.", "observation_indices": [1]},
            {"dimension": "virality", "rating": 2, "reason": "The demonstrated upload step offers a specific practical use.", "observation_indices": [1, 3]},
        ],
    }


def inputs():
    frames = [SampledFrame(t, f"original-{t}".encode(), 448, 796) for t in [500, 2500, 3500]]
    window = ScreenWindow(
        start_ms=2000, end_ms=4000, status="needs_review", prefix_asr_text="Choose a file.",
        frame_timestamps_ms=[frame.timestamp_ms for frame in frames],
        evidence_frames=[ScreenFrame(t_ms=f.timestamp_ms, width=f.width, height=f.height,
                                     sha256=sha256_bytes(f.jpeg), path=f"/fixture/{f.timestamp_ms}.jpg") for f in frames],
        judgment=ScreenJudgment.model_validate(judgment()),
        validation_issues=["Review check caption_alignment: readable caption evidence missing"],
        raw_output={"prior_attempt": "must remain immutable"}, input_tokens=111, output_tokens=22,
    )
    window.judgment.review_checks[3].observation_indices = [0, 3]
    payload = ViewerPayload(
        media=ProbeMedia(kind="frames", duration_ms=4000, frames=frames),
        instruction="Original past-only evidence: >> 2.5s Choose a file.",
        window=InspectedWindow(kind="prefix", start_ms=2000, end_ms=4000,
                               frame_timestamps_ms=[2500, 3500], frame_width=448,
                               context_frame_timestamps_ms=[500], context_frames=1,
                               latest_word_end_ms=3500, boundary_ms=4000),
    )
    return window, payload


class Provider:
    def __init__(self, response=None, failure=None):
        self.response = judgment() if response is None else response
        self.failure = failure
        self.calls = []

    def judge_json(self, media, instruction, schema):
        self.calls.append((media, instruction, copy.deepcopy(schema)))
        if self.failure:
            raise self.failure
        return CompletionResult(data=copy.deepcopy(self.response), latency_ms=12, model="offline-stub",
                                input_tokens=100, output_tokens=60)


def test_one_correction_pass_uses_original_evidence_and_preserves_all_original_fields():
    window, payload = inputs()
    before, media_before = window.model_dump_json(), copy.deepcopy(payload)
    provider = Provider()
    checkpoints = []

    def checkpoint(candidate):
        assert not provider.calls
        assert candidate.status == "pending" and candidate.raw_output == {}
        assert candidate.instruction_sha256 and candidate.payload_sha256 and candidate.response_schema_sha256
        checkpoints.append(candidate)

    fixed = repair_screen_window(provider, payload, window, checkpoint=checkpoint)
    assert len(provider.calls) == len(checkpoints) == 1
    media, instruction, schema = provider.calls[0]
    assert media is payload.media and payload == media_before
    assert fixed is not window and window.model_dump_json() == before
    assert fixed.status == "complete" and fixed.validation_issues == []
    assert fixed.input_tokens == 100 and fixed.output_tokens == 60 and fixed.latency_ms == 12
    assert fixed.response_schema_sha256 == sha256_json(schema)
    assert fixed.judgment.review_checks[6].status == "unknown"
    assert fixed.judgment.uncertainties == ["Native vocal delivery is unavailable."]
    assert fixed.semantic_grounding_verified is False
    assert "prior_attempt" not in instruction
    assert "readable caption evidence missing" in instruction
    assert "exact asr_quote" in instruction and "CURRENT interval" in instruction
    assert "Native audio is absent" in instruction and "Niche, religious or political" in instruction
    assert schema["$defs"]["ScreenObservation"]["properties"]["asr_quote"]["enum"] == [None, "Choose a file."]
    assert not repair_needed(fixed)


@pytest.mark.parametrize("mutation", ["frame", "words", "media_transcript", "bytes", "interval", "native_video"])
def test_changed_or_future_payload_is_rejected_before_any_request(mutation):
    window, payload = inputs()
    if mutation == "frame":
        payload.media.frames[-1] = replace(payload.media.frames[-1], timestamp_ms=4001)
    elif mutation == "words":
        payload.window.latest_word_end_ms = 4001
    elif mutation == "media_transcript":
        payload.media.transcript = "Choose a file. FUTURE REVEAL."
    elif mutation == "bytes":
        payload.media.frames[0] = replace(payload.media.frames[0], jpeg=b"replacement frame")
    elif mutation == "interval":
        payload.window.start_ms = 0
    else:
        payload.media.video_bytes = b"full video would leak future content"
    provider = Provider()
    with pytest.raises(ValueError):
        repair_screen_window(provider, payload, window)
    assert not provider.calls


def test_checkpoint_or_provider_failure_propagates_without_retry_or_original_mutation():
    window, payload = inputs()
    before = window.model_dump_json()
    provider = Provider()

    def checkpoint(_):
        raise RuntimeError("checkpoint unavailable")

    with pytest.raises(RuntimeError, match="checkpoint unavailable"):
        repair_screen_window(provider, payload, window, checkpoint=checkpoint)
    assert provider.calls == []
    provider = Provider(failure=ProviderError("insufficient_quota"))
    with pytest.raises(ProviderError):
        repair_screen_window(provider, payload, window)
    assert len(provider.calls) == 1
    assert window.model_dump_json() == before


def test_invalid_output_is_retained_as_a_separate_candidate_without_a_retry():
    window, payload = inputs()
    provider = Provider(response={"unsupported": "raw response"})
    fixed = repair_screen_window(provider, payload, window)
    assert fixed.status == "needs_review" and fixed.judgment is None
    assert fixed.raw_output == {"unsupported": "raw response"}
    assert len(provider.calls) == 1
    assert window.raw_output == {"prior_attempt": "must remain immutable"}


@pytest.mark.parametrize("failure", ["missing_aspect", "duplicate_aspect", "missing_score", "null_score",
                                     "inference_score", "old_visual_score", "old_visual_attention",
                                     "bad_quote", "future_frame", "native_voice", "pending_payoff"])
def test_uncorrected_evidence_or_rubric_problems_stay_needs_review(failure):
    data = judgment()
    if failure == "missing_aspect":
        data["review_checks"].pop()
    elif failure == "duplicate_aspect":
        data["review_checks"][-1] = copy.deepcopy(data["review_checks"][0])
    elif failure == "missing_score":
        data["potential_scores"].pop()
    elif failure == "null_score":
        data["potential_scores"][2]["rating"] = None
    elif failure == "inference_score":
        data["observations"][0]["kind"] = "inference"
        data["potential_scores"][2]["observation_indices"] = [0]
    elif failure == "old_visual_score":
        data["potential_scores"][2]["observation_indices"] = [0]
    elif failure == "old_visual_attention":
        data["cause_observation_indices"] = [0]
    elif failure == "bad_quote":
        data["observations"][3]["asr_quote"] = "Choose a different file."
    elif failure == "future_frame":
        data["observations"][1]["frame_timestamps_ms"] = [4500]
    elif failure == "native_voice":
        data["review_checks"][6].update(status="clear", reason="The speaker sounds confident.", observation_indices=[3])
    else:
        data["review_checks"][4].update(status="concern", reason="The current sentence is cut off at the checkpoint.")
    window, payload = inputs()
    provider = Provider(data)
    fixed = repair_screen_window(provider, payload, window)
    assert fixed.status == "needs_review" and fixed.validation_issues, failure
    assert fixed.raw_output == data
    assert len(provider.calls) == 1
    assert repair_needed(fixed)


def test_current_ending_asr_can_support_ratings_without_a_current_visual_citation():
    window, payload = inputs()
    data = judgment()
    data["cause_observation_indices"] = [3]
    for score in data["potential_scores"]:
        score["observation_indices"] = [3]
    fixed = repair_screen_window(Provider(data), payload, window)
    assert fixed.status == "complete"


def test_earlier_exact_asr_excerpt_cannot_stand_in_for_the_current_interval():
    from directorloop.screening.runner import _quote_options

    window, payload = inputs()
    window.prefix_asr_text = " ".join(["earlier words"] * 15 + ["Choose a file."])
    options = _quote_options(window.prefix_asr_text)
    assert len(options) > 1
    data = judgment()
    data["observations"][3]["asr_quote"] = options[0]
    data["cause_observation_indices"] = [3]
    data["potential_scores"][2]["observation_indices"] = [3]
    fixed = repair_screen_window(Provider(data), payload, window)
    assert fixed.status == "needs_review"
    assert any("current interval" in issue for issue in fixed.validation_issues)


@pytest.mark.parametrize("status", ["failed", "pending", "not_attempted"])
def test_failure_or_unstarted_work_is_not_automatically_retried(status):
    window, _ = inputs()
    window.status = status
    assert not repair_needed(window)


def focused_response():
    data = judgment()
    data.pop("review_checks")
    return data


def test_focused_pass_retains_good_aspects_remaps_evidence_and_downgrades_only_invalid_checks():
    window, payload = inputs()
    before = window.model_dump_json()
    data = focused_response()
    data["potential_scores"][2]["rating"] = 0
    data["potential_scores"][2]["reason"] = "The ordinary upload step offers no distinctive sharing motive."
    provider = Provider(data)
    checkpoints = []
    candidate = focused_repair_screen_window(provider, payload, window, checkpoint=checkpoints.append)
    assert len(provider.calls) == len(checkpoints) == 1
    assert window.model_dump_json() == before
    assert candidate.status == "complete", candidate.validation_issues
    assert not repair_needed(candidate)
    assert candidate.raw_output == data and "review_checks" not in candidate.raw_output
    assert len(candidate.judgment.review_checks) == 8
    assert len(candidate.judgment.observations) <= 12
    checks = {check.aspect: check for check in candidate.judgment.review_checks}
    assert checks["caption_alignment"].status == "unknown"
    assert checks["caption_alignment"].observation_indices == []
    assert checks["pacing"].status == "clear"
    assert checks["pacing"].reason == window.judgment.review_checks[0].reason
    assert checks["pacing"].observation_indices != window.judgment.review_checks[0].observation_indices
    kept_observation = candidate.judgment.observations[checks["pacing"].observation_indices[0]]
    assert kept_observation == window.judgment.observations[1]
    assert checks["voice_delivery"].status == "unknown"
    assert candidate.judgment.potential_scores[2].rating == 0
    schema = provider.calls[0][2]
    assert "review_checks" not in schema["properties"] and "review_checks" not in schema["required"]
    assert schema["properties"]["observations"]["maxItems"] == 6
    assert "prior_attempt" not in provider.calls[0][1]
    assert "unfinished future payoff" in provider.calls[0][1]
    assert "at most 16 words EACH for understanding, suggestion, and every potential_scores reason" in provider.calls[0][1]
    assert "one complete declarative sentence per field" in provider.calls[0][1]
    assert "without assumptions about audience size, broad appeal, popularity, or platform distribution" in provider.calls[0][1]
    assert candidate.semantic_grounding_verified is False


def test_focused_pass_does_not_preserve_invalid_pending_payoff_or_force_missing_scores():
    window, payload = inputs()
    window.judgment.review_checks[4].status = "concern"
    window.judgment.review_checks[4].reason = "The current sentence is cut off at this checkpoint."
    data = focused_response()
    data["potential_scores"][2]["rating"] = None
    candidate = focused_repair_screen_window(Provider(data), payload, window)
    assert candidate.status == "needs_review"
    assert candidate.judgment.potential_scores[2].rating is None
    hook = next(check for check in candidate.judgment.review_checks if check.aspect == "hook_and_payoff")
    assert hook.status == "unknown" and hook.suggested_change is None
    assert any("virality" in issue for issue in candidate.validation_issues)


def test_focused_pass_rejects_inference_or_old_context_as_score_evidence():
    window, payload = inputs()
    data = focused_response()
    data["potential_scores"][2]["observation_indices"] = [0]
    candidate = focused_repair_screen_window(Provider(data), payload, window)
    assert candidate.status == "needs_review"
    assert any("current interval" in issue for issue in candidate.validation_issues)
    data["observations"][0]["kind"] = "inference"
    candidate = focused_repair_screen_window(Provider(data), payload, window)
    assert candidate.status == "needs_review"
    assert any("admissible concrete rating" in issue for issue in candidate.validation_issues)


def test_focused_failure_propagates_once_and_does_not_modify_original():
    window, payload = inputs()
    before = window.model_dump_json()
    provider = Provider(failure=ProviderError("offline failure"))
    with pytest.raises(ProviderError):
        focused_repair_screen_window(provider, payload, window)
    assert len(provider.calls) == 1 and window.model_dump_json() == before


def test_focused_output_cannot_silently_regenerate_the_aspect_checklist():
    window, payload = inputs()
    candidate = focused_repair_screen_window(Provider(judgment()), payload, window)
    assert candidate.status == "needs_review" and candidate.judgment is None
    assert candidate.raw_output == judgment()


def test_complete_score_reasons_without_terminal_period_are_accepted_without_rewriting_raw():
    window, payload = inputs()
    data = focused_response()
    reasons = [
        "The combination of ground-level walking shots and sweeping aerial views provides a strong visual contrast that supports the educational narrative about the land",
        "The clear progression from the specific rock fragments to the broader landscape helps maintain viewer interest in the geological explanation",
        "The content is educational and visually appealing, but the specific geological topic may limit its broad shareability compared to more general travel content",
    ]
    for score, reason in zip(data["potential_scores"], reasons, strict=True):
        score["reason"] = reason
    before = copy.deepcopy(data)
    candidate = focused_repair_screen_window(Provider(data), payload, window)
    # This verifies formatting only. Anchors and semantic checks remain separate;
    # the illustrative statements are not claimed to describe this fixture.
    assert candidate.status == "complete", candidate.validation_issues
    assert candidate.raw_output == data == before
    assert [s.reason for s in candidate.judgment.potential_scores] == reasons


@pytest.mark.parametrize("aspect", ["hook_and_payoff", "share_motivation"])
@pytest.mark.parametrize("reason", [
    "The specific shareable value of the content is not yet established as the verse reading is incomplete.",
    "The current section repeats the same invitation without a specific takeaway.",
])
def test_focused_pass_does_not_reaffirm_prior_hook_or_share_concerns(aspect, reason):
    window, payload = inputs()
    check = next(c for c in window.judgment.review_checks if c.aspect == aspect)
    check.status, check.reason = "concern", reason
    check.suggested_change = "Make the takeaway clearer."
    before = window.model_dump_json()
    candidate = focused_repair_screen_window(Provider(focused_response()), payload, window)
    retained = next(c for c in candidate.judgment.review_checks if c.aspect == aspect)
    assert retained.status == "unknown"
    assert retained.reason == "This focused pass did not reassess the earlier concern for this aspect."
    assert retained.observation_indices == [] and retained.suggested_change is None
    assert candidate.judgment.potential_scores[2].rating == 2
    assert window.model_dump_json() == before
