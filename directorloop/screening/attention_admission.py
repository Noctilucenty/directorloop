"""Admit a source-linked attention label independently of unrelated subtitle checks.

This is a deterministic view of an existing model judgment. It does not establish
semantic truth, predict audience outcomes, modify the report or call a provider.
The caller must first apply current read-time boundary validation to the window.
"""

from __future__ import annotations

from typing import Any

from .models import ScreenWindow
from .runner import _asr_quote_matches

ATTENTION_ADMISSION_VERSION = "attention-evidence-v1"
_UNRELATED_CHECK_FAILURES = {
    "Review check caption_readability: readable caption evidence missing": "caption_readability",
    "Review check caption_alignment: readable caption evidence missing": "caption_alignment",
    "Review check caption_alignment: matching transcript evidence missing": "caption_alignment",
}


def assess_attention(window: ScreenWindow) -> dict[str, Any]:
    """Keep unknown unless every attention cause has valid concrete source anchors.

    A needs-review window may retain its existing attention label only when every
    issue is a specifically recognized subtitle-modality failure. All other
    failures, including pending payoffs and errors on unrelated observations,
    remain blocked. No missing legacy evidence is silently grandfathered in.
    """
    excluded = sorted({_UNRELATED_CHECK_FAILURES[issue] for issue in window.validation_issues
                       if issue in _UNRELATED_CHECK_FAILURES})

    def result(reason: str, *, supported: bool = False) -> dict[str, Any]:
        return {
            "version": ATTENTION_ADMISSION_VERSION,
            "status": "supported" if supported else "blocked",
            "risk": window.judgment.attention_risk if supported and window.judgment else "unknown",
            "reason": reason,
            "excluded_checks": excluded,
            "semantic_grounding_verified": False,
        }

    if window.status not in {"complete", "needs_review"} or window.error or not window.judgment:
        return result("This section has no completed attention judgment.")
    judgment = window.judgment
    if judgment.attention_risk not in {"low", "medium", "high"}:
        return result("The model did not provide an attention estimate for this section.")
    if window.status == "needs_review" and not window.validation_issues:
        return result("This section still needs review; its missing checks cannot be assumed to pass.")
    if window.status == "complete" and window.validation_issues:
        return result("The saved status and evidence checks disagree.")
    if any(issue not in _UNRELATED_CHECK_FAILURES for issue in window.validation_issues):
        return result("An unresolved evidence check prevents an attention estimate.")
    causes = judgment.cause_observation_indices
    if not causes or any(type(i) is not int or not 0 <= i < len(judgment.observations) for i in causes):
        return result("The attention judgment needs valid observation references.")
    if len(causes) != len(set(causes)):
        return result("The attention judgment repeats an observation reference.")
    if window.start_ms < 0 or window.end_ms <= window.start_ms:
        return result("The attention judgment has an invalid time interval.")
    allowed = {t for t in window.frame_timestamps_ms if type(t) is int and 0 <= t < window.end_ms}
    for index in causes:
        observation = judgment.observations[index]
        if not observation.text.strip() or observation.kind not in {"visible_fact", "caption_claim", "asr_claim"}:
            return result("The attention judgment relies on an inference or unavailable evidence.")
        frames = observation.frame_timestamps_ms
        if observation.kind in {"visible_fact", "caption_claim"}:
            if (not frames or len(frames) != len(set(frames)) or observation.asr_quote is not None
                    or any(type(t) is not int or t not in allowed for t in frames)):
                return result("An attention observation has an invalid image reference.")
        elif frames or not observation.asr_quote or not _asr_quote_matches(observation.asr_quote, window.prefix_asr_text):
            return result("An attention observation has an invalid transcript reference.")
    return result(
        "AI attention estimate uses valid source references; subtitle checks remain unavailable."
        if excluded else "AI attention estimate is linked to the supplied evidence.",
        supported=True,
    )
