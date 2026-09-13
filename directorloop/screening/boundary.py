"""Conservative phrase tripwires for mistaking a prefix limit for a video ending.

These checks identify recognizable wording for review; they do not verify meaning,
measure attention, correct model claims, or replace the model's original output.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

BOUNDARY_VALIDATION_VERSION = "intermediate-boundary-phrases-v7"
BOUNDARY_VALIDATION_PROTOCOL = {
    "version": BOUNDARY_VALIDATION_VERSION,
    "method": "phrase tripwire; only intermediate full-review judgments",
    "semantic_grounding_verified": False,
    "raw_output_modified": False,
}

_QUOTED = re.compile(r'''"[^"\n]*"|“[^”\n]*”|‘[^’\n]*’|(?<!\w)'[^'\n]+'(?!\w)''')
_END_CRITICISM = re.compile(
    r"\b(?:video|clip|source|it)\s+(?:(?:abruptly|apparently)\s+)?"
    r"(?:ends?|stops?|cuts?\s*off|is\s+cut\s*off)\s+"
    r"(?:(?:too\s+early|abruptly)\s*)?(?:mid[-\s]sentence|before|without|prematurely)\b"
    r"|\b(?:video|clip|source)\s+cuts?\s*off\s+too\s+early\b"
    r"|\b(?:interval|prefix)\s+ends?\s+before\b"
    r"|\bends?\s+mid[-\s]sentence\b",
    re.IGNORECASE,
)
_DELIVERY_ACTION = (
    r"(?:deliver(?:s|ed|ing)?|show(?:s|n|ing)?|reveal(?:s|ed|ing)?|provid(?:e|es|ed|ing)|"
    r"answer(?:s|ed|ing)?|resolv(?:e|es|ed|ing)|read(?:s|ing)?|explain(?:s|ed|ing)?|"
    r"complet(?:e|es|ed|ing)|finish(?:es|ed|ing)?)"
)
_NEGATIVE_AUXILIARY = r"(?:not|(?:has|have|had|do|does|did|is|are|was|were)n['’]t)"
_DELIVERY_MODIFIERS = r"(?:(?:been|being)\s+)?(?:fully\s+)?"
_PENDING = re.compile(
    r"\b" + _NEGATIVE_AUXILIARY + r"\s+yet\s+" + _DELIVERY_MODIFIERS + _DELIVERY_ACTION + r"\b"
    r"|\b" + _NEGATIVE_AUXILIARY + r"\s+" + _DELIVERY_MODIFIERS + _DELIVERY_ACTION
    + r"\b[^.;!?]{0,160}\byet\b"
    r"|\b(?:interval|prefix)\s+ends?\s+before\b",
    re.IGNORECASE,
)
_PAYOFF_WORDS = (
    r"(?:payoffs?|solutions?|reveals?|features?|results?|answers?|summaries|summary|"
    r"promises?|claims?|details?|locations?|significance|advice|text|content|conclusions?|"
    r"verses?|benefits?|call\s+to\s+action|cta|links?)"
)
_PAYOFF = re.compile(r"\b" + _PAYOFF_WORDS + r"\b", re.IGNORECASE)
_PAYOFF_PENDING = re.compile(
    r"\b" + _PAYOFF_WORDS + r"\b[^.;!?]{0,100}"
    r"\b(?:is|are|remains?)\s+(?:still\s+)?(?:pending|unresolved|undelivered)\b",
    re.IGNORECASE,
)
_PENDING_PAYOFF = re.compile(
    r"\b(?:pending|unresolved|undelivered)\s+" + _PAYOFF_WORDS + r"\b",
    re.IGNORECASE,
)
_NEGATED_PENDING = re.compile(
    r"\b(?:no|not|never)\s+(?:longer\s+|a\s+|an\s+)?(?:pending|unresolved|undelivered)\b",
    re.IGNORECASE,
)
_PAYOFF_CUTOFF = re.compile(
    r"\b" + _PAYOFF_WORDS + r"\b[^.;!?]{0,80}"
    r"\b(?:is|gets?|was|has\s+been)\s+cut\s*off\b[^.;!?]{0,100}"
    r"\b(?:unexplained|unresolved|undelivered|unfinished|mid[-\s](?:sentence|verse))\b",
    re.IGNORECASE,
)
_CUTOFF_BEFORE_PAYOFF = re.compile(
    r"\b(?:cuts?|cutting|cut|stops?|stopping|ends?|ending)\s+(?:off\s+)?before\s+"
    r"[^.;!?]{0,100}\b" + _PAYOFF_WORDS + r"\b",
    re.IGNORECASE,
)
_NOT_VISIBLE_BY_CUTOFF = re.compile(
    r"\b" + _NEGATIVE_AUXILIARY + r"\s+yet\s+(?:(?:fully|clearly)\s+)?(?:visible|shown)\b"
    r"[^.;!?]{0,90}\b(?:at|by)\s+(?:(?:this|the|current|review|evidence)\s+){0,2}"
    r"(?:cutoff|checkpoint|prefix\s+(?:limit|boundary))\b",
    re.IGNORECASE,
)
_SENTENCE_CUTOFF = re.compile(
    r"\bcurrent\s+(?:sentence|utterance|phrase)\s+(?:(?:is|gets?)\s+)?(?:cut\s*off|cut\s+short)\b"
    r"|\b(?:sentence|utterance|phrase)\s+(?:(?:is|gets?)\s+)?(?:cut\s*off|ends?|stops?)\b"
    r"[^.;!?]{0,70}\b(?:at|by|before)\s+(?:(?:this|the|current|review|evidence)\s+){0,2}"
    r"(?:cutoff|checkpoint|prefix\s+(?:limit|boundary))\b",
    re.IGNORECASE,
)
_UNFINISHED_AT_CUTOFF = re.compile(
    r"\b(?:mid[-\s](?:verse|sentence)|middle\s+of\s+(?:the\s+)?(?:sentence|verse))\b"
    r"[^.;!?]{0,90}\b(?:at|by|before)\s+(?:the\s+)?(?:cutoff|checkpoint|prefix\s+boundary)\b",
    re.IGNORECASE,
)
_REPETITION = re.compile(r"\b(?:repeats?|repeated|repetition|unchanged|same\s+(?:claim|slogan|text|shot|image)|no\s+new\s+information)\b", re.IGNORECASE)


def _own_words(value: Any) -> str:
    """Quoted content is not itself the reviewer's assertion about the source."""
    return _QUOTED.sub(" ", value) if isinstance(value, str) else ""


def _observed_repetition(check: Mapping[str, Any], observations: list[Any]) -> bool:
    """Do not mistake a cited repetition concern for a solely pending payoff.

    Frame/quote membership remains the runner's separate responsibility. This only
    recognizes a concrete repetition statement with more than one image anchor,
    or repeated words inside an attributed ASR quote; it cannot prove repetition.
    """
    for index in check.get("observation_indices", []):
        if type(index) is not int or not 0 <= index < len(observations):
            continue
        observation = observations[index]
        if not isinstance(observation, Mapping) or not _REPETITION.search(str(observation.get("text", ""))):
            continue
        kind = observation.get("kind")
        frames = observation.get("frame_timestamps_ms", [])
        if kind in {"visible_fact", "caption_claim"} and isinstance(frames, list):
            valid = {t for t in frames if type(t) is int and t >= 0}
            if len(valid) >= 2:
                return True
        if kind == "asr_claim" and isinstance(observation.get("asr_quote"), str):
            words = re.findall(r"\b\w+\b", observation["asr_quote"].lower())
            if len(words) >= 2 and len(set(words)) < len(words):
                return True
    return False


def boundary_validation_issues(judgment: Mapping[str, Any], *, is_last_prefix: bool,
                               coverage: str = "full") -> list[str]:
    """Return additional review flags, without changing claims or risk labels.

    Not comprehensive: a missing flag does not mean the judgment is semantically
    valid. Only full-review intermediate prefixes participate in this tripwire.
    """
    if coverage != "full" or is_last_prefix or not isinstance(judgment, Mapping):
        return []
    checks = judgment.get("review_checks", [])
    observations = judgment.get("observations", [])
    checks = checks if isinstance(checks, list) else []
    observations = observations if isinstance(observations, list) else []
    own_statements = [_own_words(judgment.get(key)) for key in ("understanding", "suggestion")]
    own_statements.extend(_own_words(check.get("reason")) for check in checks
                          if isinstance(check, Mapping) and check.get("status") == "concern")
    issues = []
    if any(_END_CRITICISM.search(text) for text in own_statements):
        issues.append("Intermediate checkpoint: wording may mistake the review cutoff for the video ending; inspect the evidence")
    for check in checks:
        if not isinstance(check, Mapping) or check.get("aspect") != "hook_and_payoff" or check.get("status") != "concern":
            continue
        reason = _own_words(check.get("reason"))
        # An explicit denial of a pending state must not create a pending-payoff flag.
        # This remains a wording tripwire, not a judgment of whether a promise exists.
        pending_reason = _NEGATED_PENDING.sub(" ", reason)
        checkpoint_only = (
            (_PENDING.search(reason) and _PAYOFF.search(reason))
            or _PAYOFF_PENDING.search(pending_reason)
            or _PENDING_PAYOFF.search(pending_reason)
            or _PAYOFF_CUTOFF.search(reason)
            or _CUTOFF_BEFORE_PAYOFF.search(reason)
            or (_NOT_VISIBLE_BY_CUTOFF.search(reason) and _PAYOFF.search(reason))
            or _SENTENCE_CUTOFF.search(reason)
            or _UNFINISHED_AT_CUTOFF.search(reason)
        )
        if checkpoint_only and not _observed_repetition(check, observations):
            issues.append("Review check hook_and_payoff: pending future payoff is not by itself an observed defect at an intermediate checkpoint")
            break
    return issues
