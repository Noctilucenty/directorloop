"""One bounded evidence correction pass over the original past-only payload.

The caller owns scheduling, spend limits and immutable attempt history. This
module never loads additional media, fetches future context or saves a report.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from ..domain.ids import sha256_bytes, sha256_json
from ..observability.weave_ops import current_call_ref, traced
from .boundary import boundary_validation_issues
from .models import ScreenJudgment, ScreenWindow
from .scoring import SCORE_RUBRIC, _anchored, _pending, _score_statement, _sentence

if TYPE_CHECKING:
    from ..audit.review import ViewerPayload

REPAIR_VERSION = "screening-evidence-repair-v1"
FOCUSED_REPAIR_VERSION = "screening-focused-evidence-v1"
FOCUSED_INSTRUCTION = """
Independently judge ONLY the current video section from the supplied evidence. Earlier frames are context. Do not rehash the opening when the current section has new content. No prior judgments or scores are source facts.
Return the requested JSON: 4–6 short concrete observations when supported (usually 20 words each), one concise understanding and finding, attention_risk and its observation indices, moment_kind, uncertainties, and exactly three potential_scores (creative, retention, virality). This focused pass does not repeat the eight-aspect checklist; separately validated prior aspects are retained outside this model call.
Use at most 16 words EACH for understanding, suggestion, and every potential_scores reason. Write one complete declarative sentence per field. Finish the thought within that limit; never end with an unfinished comparison, clause, or ellipsis. Describe the supplied content without assumptions about audience size, broad appeal, popularity, or platform distribution.
Write observations first. visible_fact describes only visible pixels with exact supplied frame timestamps and asr_quote=null. caption_claim separately records readable on-screen words with frame citations and asr_quote=null. asr_claim uses no frame citations and chooses one EXACT allowed ASR quote; its text may paraphrase ONLY that chosen quote, not words in another excerpt. Do not put guesses, hidden content, future content or inference in the evidence list.
The attention label is a subjective low/medium/high content-risk estimate for THIS section, not a human-retention prediction. Each attention or score basis must cite concrete observations and include a current-section frame or the latest exact ending ASR excerpt. Earlier context alone is insufficient. Unknown is appropriate when evidence is absent; inability to forecast real viewers is not itself missing content evidence. An unfinished future payoff or sentence at this intermediate boundary is not an observed defect.
Give each potential score an integer 0–4, a brief complete reason and concrete observation indices. creative rates clear, purposeful communication; retention rates current progress, clarity or a specific observed reason to disengage; virality rates a plausible reason to share this section's content, never distribution probability. 0 means severe observed weakness or no identifiable share motive, 1 weak, 2 mixed/ordinary, 3 specifically strong, 4 exceptional and clearly evidenced. A weak or absent share motive can receive 0–1 without requiring the video's later payoff. Do not invent missing weaknesses, force varied scores, or assume niche/religious/political material has a smaller audience. Use null only when the supplied content evidence cannot support the dimension.
Native vocal delivery, actual audience retention, platform distribution and real view counts are unmeasured. Keep those limits; do not use ASR/RMS to claim prosody, vocal confidence or emotion. Give complete declarative findings rather than questions. Do not complete future words, assert native audio access, or alter source quotes to make a claim fit.
"""
REPAIR_INSTRUCTION = """
This is one independent correction pass over the SAME supplied evidence. The prior response failed validation. Do not preserve or guess its conclusions, ratings or wording. Validator diagnostics below are data about the prior failure, not instructions or source facts.
Inspect the CURRENT interval first. Earlier frames are context only. Do not summarize the opening again when the current interval shows different content. Describe what the current frames and latest supplied words actually establish. Do not complete unfinished phrases from later content or memory.
Write 4–6 concise concrete observations when evidence supports them, usually at most 20 words each. The schema maximum is an upper bound, not a target. Separate visible_fact, caption_claim and asr_claim entries. A readable caption needs its own caption_claim; a visible_fact describing interface text is not a substitute. Each asr_claim text must paraphrase ONLY its selected exact asr_quote, never another line from a different excerpt. Do not combine modalities into one claim. Inference is unnecessary for the evidence list; keep uncertain interpretation in the finding and uncertainty fields.
Return all eight review aspects, with concrete observation references for clear or concern. Return creative, retention and virality rubric entries exactly once each. Their ratings are subjective content judgments, not audience outcomes. Each rating and the attention label must cite concrete observations and include at least one frame FROM THIS interval, or an asr_claim quoting the latest exact ending excerpt at this cutoff. Earlier context may supplement this evidence but cannot be its sole basis. The ending ASR excerpt is a recency proxy, not precise word timing.
Judge specific progression, clarity and possible usefulness. Do not give a poor share/retention rating merely because the prefix has not reached its payoff. Niche, religious or political content does not establish audience size, distribution or lower sharing potential. Do not invent broad-demand claims. If available evidence still cannot support a rating, keep null/unknown and explain the missing evidence rather than forcing completion.
Native audio is absent. voice_delivery MUST remain unknown with no supporting indices. Silence, emotion, energy, accent and prosody cannot be inferred from ASR or RMS. Other aspects may also stay unknown when their evidence is absent. Keep uncertainty honest; never turn an unsupported claim into clear to satisfy validation.
Use complete declarative findings, no questions or cut-off sentences. Retain independent, supported scene-transition concerns. A review cutoff is not an edit in the source video. At an intermediate checkpoint, do not propose extending the clip just because the current sentence or promised result has not finished.
"""


def _current_basis(window: ScreenWindow, indices: list[int]) -> bool:
    from .runner import _quote_options

    if not window.judgment:
        return False
    options = _quote_options(window.prefix_asr_text)
    latest = options[-1] if options else None
    for index in indices:
        if type(index) is not int or not 0 <= index < len(window.judgment.observations):
            continue
        observation = window.judgment.observations[index]
        if any(window.start_ms <= t < window.end_ms for t in observation.frame_timestamps_ms):
            return True
        if observation.kind == "asr_claim" and latest and observation.asr_quote == latest:
            return True
    return False


def _quality_issues(window: ScreenWindow) -> list[str]:
    from .attention_admission import assess_attention
    from .runner import FULL_REVIEW_ASPECTS, validate_evidence

    judgment = window.judgment
    if not judgment:
        return ["Model output did not match the screening schema"]
    issues = validate_evidence(judgment, window.frame_timestamps_ms, window.prefix_asr_text)
    issues.extend(boundary_validation_issues(judgment.model_dump(), is_last_prefix=window.is_last_prefix))
    counts = Counter(check.aspect for check in judgment.review_checks)
    if set(counts) != FULL_REVIEW_ASPECTS or any(count != 1 for count in counts.values()):
        issues.append("Review repair: the eight-aspect checklist is incomplete or duplicated")
    checked = window.model_copy(deep=True)
    checked.validation_issues = list(dict.fromkeys(issues))
    checked.status = "needs_review" if checked.validation_issues else "complete"
    if assess_attention(checked)["status"] != "supported":
        issues.append("Review repair: attention lacks admissible concrete evidence")
    elif not _current_basis(checked, judgment.cause_observation_indices):
        issues.append("Review repair: attention needs evidence from the current interval")
    for check in judgment.review_checks:
        if not _sentence(check.reason):
            issues.append(f"Review check {check.aspect}: a complete declarative reason is required")
        if check.status != "unknown" and not _anchored(checked, check.observation_indices):
            issues.append(f"Review check {check.aspect}: concrete source evidence is required")
    dimensions = ("creative", "retention", "virality")
    for dimension in dimensions:
        scores = [score for score in judgment.potential_scores if score.dimension == dimension]
        if len(scores) != 1:
            issues.append(f"Review repair score {dimension}: exactly one rating is required")
            continue
        score = scores[0]
        if (score.rating is None or not _score_statement(score.reason)
                or not _anchored(checked, score.observation_indices) or _pending(score.reason, checked)):
            issues.append(f"Review repair score {dimension}: an admissible concrete rating is required")
        elif not _current_basis(checked, score.observation_indices):
            issues.append(f"Review repair score {dimension}: evidence from the current interval is required")
    return list(dict.fromkeys(issues))


def repair_needed(window: ScreenWindow) -> bool:
    """Only terminal model responses are candidates; provider failures are not retried."""
    return window.status in {"complete", "needs_review"} and bool(_quality_issues(window))


def _validate_original_payload(payload: ViewerPayload, window: ScreenWindow) -> None:
    media = payload.media
    if (media.kind != "frames" or media.video_bytes is not None
            or media.duration_ms != window.end_ms
            or payload.window.start_ms != window.start_ms or payload.window.end_ms != window.end_ms):
        raise ValueError("Repair payload does not match the original review interval")
    if ([frame.timestamp_ms for frame in media.frames] != window.frame_timestamps_ms
            or any(type(frame.timestamp_ms) is not int or not 0 <= frame.timestamp_ms < window.end_ms
                   for frame in media.frames)):
        raise ValueError("Repair frames do not match the original past-only evidence")
    if payload.window.latest_word_end_ms is not None and payload.window.latest_word_end_ms > window.end_ms:
        raise ValueError("Repair transcript crosses the original evidence boundary")
    if media.transcript is not None and media.transcript != window.prefix_asr_text:
        raise ValueError("Repair transcript does not match the original ASR prefix")
    if window.evidence_frames:
        recorded = [(frame.t_ms, frame.width, frame.sha256) for frame in window.evidence_frames]
        supplied = [(frame.timestamp_ms, frame.width, sha256_bytes(frame.jpeg)) for frame in media.frames]
        if recorded != supplied:
            raise ValueError("Repair frame bytes differ from the recorded evidence")


@traced("directorloop.screening_evidence_repair", kind="agent", display="Recheck evidence | one correction pass")
def repair_screen_window(provider: Any, payload: ViewerPayload, window: ScreenWindow, *,
                         checkpoint: Callable[[ScreenWindow], None] | None = None) -> ScreenWindow:
    """Invoke judge_json once; return a fresh candidate without mutating the original.

    Provider and checkpoint exceptions propagate to the caller's spend/stop
    coordinator. Invalid model output is returned as needs_review with raw output
    retained. The optional checkpoint receives input hashes before dispatch.
    """
    from .runner import (
        FULL_REVIEW_RUBRIC,
        SCREENING_INSTRUCTION,
        _full_response_schema,
        _quote_options,
        _safe_raw,
    )

    _validate_original_payload(payload, window)
    candidate = window.model_copy(deep=True)
    candidate.status, candidate.judgment = "pending", None
    candidate.raw_output, candidate.validation_issues, candidate.error = {}, [], None
    candidate.input_tokens = candidate.output_tokens = candidate.latency_ms = None
    candidate.weave_call_id, candidate.weave_url = current_call_ref()
    schema = _full_response_schema(window.prefix_asr_text)
    max_observations = schema["properties"]["observations"]["maxItems"]
    base_instruction = SCREENING_INSTRUCTION.format(frame_times=json.dumps(window.frame_timestamps_ms))
    base_instruction = base_instruction.replace("Use at most 4 observations", f"Use at most {max_observations} observations")
    instruction = payload.instruction + base_instruction + FULL_REVIEW_RUBRIC + SCORE_RUBRIC + REPAIR_INSTRUCTION
    instruction += "\nVALIDATOR DIAGNOSTICS: " + json.dumps(_safe_raw(_quality_issues(window)), ensure_ascii=False)
    options = _quote_options(window.prefix_asr_text)
    instruction += "\nASR QUOTE OPTIONS: " + json.dumps(options, ensure_ascii=False)
    instruction += "\nLATEST ENDING ASR EXCERPT: " + json.dumps(options[-1] if options else None, ensure_ascii=False)
    instruction += "\nChoose asr_quote exactly from these options, or null. Never change punctuation, capitalization or wording."
    instruction += (
        "\nFINAL CHECKPOINT: This is the end of the source. A natural ending is not itself a defect."
        if window.is_last_prefix else
        "\nINTERMEDIATE CHECKPOINT: The source continues. Its future is withheld, not missing. Current sentences and payoffs may continue later."
    )
    candidate.instruction_sha256 = sha256_bytes(instruction.encode())
    candidate.response_schema_sha256 = sha256_json(schema)
    candidate.payload_sha256 = sha256_json({"version": REPAIR_VERSION, "instruction": instruction,
        "schema_sha256": candidate.response_schema_sha256,
        "frames": [{"timestamp_ms": frame.timestamp_ms, "width": frame.width, "sha256": sha256_bytes(frame.jpeg)}
                   for frame in payload.media.frames]})
    if checkpoint is not None:
        checkpoint(candidate.model_copy(deep=True))
    result = provider.judge_json(payload.media, instruction, schema)
    raw = _safe_raw(result.data)
    candidate.raw_output = raw if isinstance(raw, dict) else {"non_object_response": raw}
    candidate.input_tokens, candidate.output_tokens, candidate.latency_ms = result.input_tokens, result.output_tokens, result.latency_ms
    try:
        candidate.judgment = ScreenJudgment.model_validate(result.data)
    except ValidationError:
        candidate.status = "needs_review"
        candidate.validation_issues = ["Model output did not match the screening schema"]
        return candidate
    candidate.validation_issues = _quality_issues(candidate)
    candidate.status = "needs_review" if candidate.validation_issues else "complete"
    kind = candidate.judgment.moment_kind
    candidate.attention_context = f"last_{kind}" if candidate.is_last_prefix and kind in {"endcard", "signoff"} else "content"
    return candidate


def _retain_valid_aspects(prior: ScreenWindow, focused: ScreenJudgment) -> ScreenJudgment:
    """Retain valid prior checks with remapped evidence; unavailable checks stay unknown."""
    from .models import ScreenReviewCheck
    from .runner import FULL_REVIEW_ASPECTS, validate_evidence

    merged = focused.model_copy(deep=True)
    prior_checked = prior.model_copy(deep=True)
    prior_checked.validation_issues = []
    if prior.judgment:
        prior_checked.validation_issues = validate_evidence(prior.judgment, prior.frame_timestamps_ms, prior.prefix_asr_text)
        prior_checked.validation_issues.extend(boundary_validation_issues(prior.judgment.model_dump(), is_last_prefix=prior.is_last_prefix))
    maximum = ScreenJudgment.model_json_schema()["properties"]["observations"]["maxItems"]
    remapped: dict[int, int] = {}
    checks = []
    for aspect in ["pacing", "visual_clarity", "caption_readability", "caption_alignment",
                   "hook_and_payoff", "tone_from_words", "voice_delivery", "share_motivation"]:
        assert aspect in FULL_REVIEW_ASPECTS
        matching = [c for c in prior.judgment.review_checks if c.aspect == aspect] if prior.judgment else []
        unknown = ScreenReviewCheck(aspect=aspect, status="unknown", observation_indices=[],
            reason="The earlier review did not establish valid evidence for this aspect.", suggested_change=None)
        if len(matching) != 1 or aspect == "voice_delivery":
            if aspect == "voice_delivery":
                unknown.reason = "Native audio was not supplied."
            checks.append(unknown)
            continue
        check = matching[0]
        if check.status == "concern" and aspect in {"hook_and_payoff", "share_motivation"}:
            unknown.reason = "This focused pass did not reassess the earlier concern for this aspect."
            checks.append(unknown)
            continue
        if check.status == "unknown":
            if _sentence(check.reason):
                unknown.reason = check.reason
            checks.append(unknown)
            continue
        invalid = (
            not _sentence(check.reason) or not _anchored(prior_checked, check.observation_indices)
            or not _current_basis(prior_checked, check.observation_indices)
            or any(issue.startswith(f"Review check {aspect}:") for issue in prior_checked.validation_issues)
            or (aspect == "hook_and_payoff" and (
                _pending(check.reason, prior_checked)
                or any(issue.startswith("Intermediate checkpoint:") for issue in prior_checked.validation_issues)))
        )
        needed = [i for i in check.observation_indices if i not in remapped]
        if invalid or len(merged.observations) + len(needed) > maximum:
            checks.append(unknown)
            continue
        for old_index in needed:
            remapped[old_index] = len(merged.observations)
            merged.observations.append(prior.judgment.observations[old_index].model_copy(deep=True))
        kept = check.model_copy(deep=True)
        kept.observation_indices = [remapped[i] for i in check.observation_indices]
        checks.append(kept)
    merged.review_checks = checks
    return merged


@traced("directorloop.screening_focused_repair", kind="agent", display="Recheck current evidence | focused assessment")
def focused_repair_screen_window(provider: Any, payload: ViewerPayload, window: ScreenWindow, *,
                                 checkpoint: Callable[[ScreenWindow], None] | None = None) -> ScreenWindow:
    """One focused model response plus deterministic retention of valid prior aspects.

    The caller records this attempt's phase and prior judgment. raw_output remains
    the untouched focused response; judgment includes remapped retained checks.
    Provider/checkpoint exceptions propagate, and no automatic retry occurs here.
    """
    from .runner import _full_response_schema, _quote_options, _safe_raw

    _validate_original_payload(payload, window)
    candidate = window.model_copy(deep=True)
    candidate.status, candidate.judgment = "pending", None
    candidate.raw_output, candidate.validation_issues, candidate.error = {}, [], None
    candidate.input_tokens = candidate.output_tokens = candidate.latency_ms = None
    candidate.weave_call_id, candidate.weave_url = current_call_ref()
    schema = _full_response_schema(window.prefix_asr_text)
    schema["properties"].pop("review_checks", None)
    schema["required"] = [key for key in schema["required"] if key != "review_checks"]
    schema["properties"]["observations"]["maxItems"] = min(6, schema["properties"]["observations"]["maxItems"])
    options = _quote_options(window.prefix_asr_text)
    instruction = payload.instruction + FOCUSED_INSTRUCTION
    instruction += "\nCURRENT SECTION FRAME TIMES: " + json.dumps([t for t in window.frame_timestamps_ms if window.start_ms <= t < window.end_ms])
    instruction += "\nASR QUOTE OPTIONS: " + json.dumps(options, ensure_ascii=False)
    instruction += "\nLATEST ENDING ASR EXCERPT: " + json.dumps(options[-1] if options else None, ensure_ascii=False)
    instruction += (
        "\nFINAL CHECKPOINT: A natural endcard or signoff is not automatically a problem."
        if window.is_last_prefix else
        "\nINTERMEDIATE CHECKPOINT: The source continues after this section. Its unfinished sentence or payoff is unknown, not missing."
    )
    candidate.instruction_sha256 = sha256_bytes(instruction.encode())
    candidate.response_schema_sha256 = sha256_json(schema)
    candidate.payload_sha256 = sha256_json({"version": FOCUSED_REPAIR_VERSION, "instruction": instruction,
        "schema_sha256": candidate.response_schema_sha256,
        "retained_aspect_source_sha256": sha256_json(window.judgment.model_dump()) if window.judgment else None,
        "frames": [{"timestamp_ms": frame.timestamp_ms, "width": frame.width, "sha256": sha256_bytes(frame.jpeg)}
                   for frame in payload.media.frames]})
    if checkpoint is not None:
        checkpoint(candidate.model_copy(deep=True))
    result = provider.judge_json(payload.media, instruction, schema)
    raw = _safe_raw(result.data)
    candidate.raw_output = raw if isinstance(raw, dict) else {"non_object_response": raw}
    candidate.input_tokens, candidate.output_tokens, candidate.latency_ms = result.input_tokens, result.output_tokens, result.latency_ms
    try:
        if not isinstance(result.data, dict) or "review_checks" in result.data:
            raise ValueError("Focused response must not regenerate the aspect checklist")
        focused = ScreenJudgment.model_validate(result.data)
        if len(focused.observations) > schema["properties"]["observations"]["maxItems"]:
            raise ValueError("Focused observation limit exceeded")
    except (ValidationError, ValueError):
        candidate.status = "needs_review"
        candidate.validation_issues = ["Model output did not match the focused screening schema"]
        return candidate
    candidate.judgment = _retain_valid_aspects(window, focused)
    candidate.validation_issues = _quality_issues(candidate)
    candidate.status = "needs_review" if candidate.validation_issues else "complete"
    kind = candidate.judgment.moment_kind
    candidate.attention_context = f"last_{kind}" if candidate.is_last_prefix and kind in {"endcard", "signoff"} else "content"
    return candidate
