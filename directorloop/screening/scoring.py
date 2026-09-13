"""Source-linked subjective rubric indices, never calibrated audience forecasts."""

from __future__ import annotations

import math
import re
from typing import Any

from .boundary import _observed_repetition, boundary_validation_issues
from .models import ScreenReport, ScreenWindow

SCORE_PROTOCOL = {
    "version": "creative-potential-v1",
    "evidence_level": "model_rubric",
    "predicts_audience_outcomes": False,
    "scale": "0..4 ordinal ratings mapped to 0..100; duration-weighted across rated sections",
    "minimum_duration_coverage": 0.6,
    "missing_ratings": "excluded, never scored as zero; coverage and provisional state are shown",
}
SCORE_RUBRIC = """
Also return potential_scores: exactly one each for creative, retention, and virality, AFTER writing the observations and review checks. These are subjective CONTENT RUBRIC ratings from 0 to 4, never probabilities, views, completion percentages, predicted audience responses or a viral forecast. Use null when evidence is insufficient. Each rating needs a short, complete declarative reason and zero-based observation_indices; cite only valid, concrete observations available at this cutoff. Uncertainty is permitted and preferred to a made-up number.
creative: assess clear communication, purposeful progression and visual legibility. 0=severe observed communication barrier; 1=major observed weaknesses; 2=mixed or ordinary execution; 3=clear, purposeful execution with specific strengths; 4=exceptionally strong execution supported by multiple specific observations.
retention: assess reasons to continue watching THIS section. 0=severe observed confusion/redundancy; 1=weak progression with an observed reason to disengage; 2=mixed or ordinary continuation value; 3=specific progress, useful anticipation or a clear reveal; 4=unusually compelling progress and payoff supported by multiple observations. An unfinished promise at an intermediate cutoff is NOT a defect. Duration alone cannot lower the rating, and a natural ending is not retention failure.
virality: assess concrete reasons someone MIGHT share THIS content, not likelihood of platform distribution. 0=no specific share motive identifiable from supplied content; 1=weak or generic motive; 2=one plausible but ordinary motive; 3=specific usefulness, surprise or relatable detail; 4=distinctive, clearly evidenced usefulness/surprise that could give viewers a strong reason to share. Use null if you cannot assess the content. A religious, political or niche subject is not intrinsically better or worse. Never assume audience size, demand, identity, social proof or actual sharing.
Do not rate all three dimensions identically by default. A clip may be clear but offer little share value. Absence of a detected problem does NOT justify a 4. Use 4 sparingly with concrete evidence; do not penalize uncertainty with a zero. Do not normalize ratings against the other videos or earlier runs. Never infer vocal delivery from text.
For each review_checks concern, suggested_change may give one brief, feasible editing suggestion tied to the observed defect. For clear or unknown checks use suggested_change=null. Use an imperative complete sentence, not a question. This is a proposed idea for human review, not authorization to execute an edit. Never recommend removing an ending or adding sensationalism just to raise a score.
"""
_DIMENSIONS = ("creative", "retention", "virality")
_ACTIONS = {
    "pacing": "Tighten the repeated beat while preserving the information it adds.",
    "visual_clarity": "Make the main subject or action easier to identify.",
    "caption_readability": "Improve the caption’s readability.",
    "caption_alignment": "Check the caption wording against the spoken words.",
    "hook_and_payoff": "Make the connection between the opening promise and its payoff clearer.",
    "tone_from_words": "Make the wording match the intended message more clearly.",
    "share_motivation": "Make the specific takeaway easier to understand and share.",
}


def _sentence(text: str | None) -> bool:
    return bool(
        text
        and len(text) <= 320
        and not re.search(r"[?？؟]|\.\.\.|…", text)
        and re.search(r"[.!。！][\"”’']?$", text.strip())
        and not re.match(r"(?:does|do|did|is|are|can|could|would|should)\b", text, re.I)
    )


def _score_statement(text: str | None) -> bool:
    """Accept a complete score reason without requiring decorative punctuation.

    This is a formatting/truncation filter, not a semantic or grammar verifier.
    Advice keeps the stricter sentence contract; evidence admission is separate.
    """
    if not text or len(text) > 320:
        return False
    value = text.strip()
    if re.search(r"[?？؟]|\.\.\.|…", value):
        return False
    if re.match(r"(?:does|do|did|is|are|can|could|would|should|why|how|what|when|where|whether)\b", value, re.I):
        return False
    body = re.sub(r"[.!。！\"”’']+$", "", value).strip()
    if not body or re.search(r"[,;:–—-]$", body):
        return False
    words = re.findall(r"[A-Za-z]+(?:['’][A-Za-z]+)?", body.lower())
    dangling = {
        "a", "an", "the", "and", "or", "but", "because", "although", "though", "while", "when",
        "if", "unless", "which", "who", "whose", "where", "whether", "than", "with", "without",
        "for", "from", "to", "of", "as", "by", "at", "in", "on", "into", "through", "toward",
        "towards", "until", "is", "are", "was", "were", "be", "being", "been", "can", "could",
        "would", "should", "will", "may", "might", "must", "has", "have", "had", "does", "do",
        "did", "not", "very", "more", "less", "only", "provides", "supports", "offers", "creates",
        "makes", "shows", "demonstrates", "maintains", "communicates", "establishes", "explains",
        "adds", "reveals", "lacks", "uses",
    }
    complete_more_phrase = bool(re.search(r"\b(?:learn|discover)\s+more$", body, re.I))
    if len(words) < 4 or (words[-1] in dangling and not complete_more_phrase):
        return False
    # An introduced clause ending at its subject is visibly unfinished, even if
    # the model supplied a period. Avoid treating a bare noun phrase as a reason.
    if re.search(r"\b(?:because|although|while|unless|which|who)\s+(?:(?:the|a|an|this|that)\s+)?\w+$", body, re.I):
        return False
    return True


def _anchored(window: ScreenWindow, indices: list[int]) -> bool:
    judgment = window.judgment
    if not judgment or not indices or len(indices) != len(set(indices)):
        return False
    if any(type(i) is not int or i < 0 or i >= len(judgment.observations) for i in indices):
        return False
    for i in indices:
        observation = judgment.observations[i]
        if observation.kind in {"inference", "unknown"}:
            return False
        if not observation.frame_timestamps_ms and not observation.asr_quote:
            return False
        if any(issue.startswith(f"Observation {i + 1}:") for issue in window.validation_issues):
            return False
    return True


def _pending(reason: str, window: ScreenWindow) -> bool:
    return bool(
        boundary_validation_issues(
            {
                "observations": window.judgment.model_dump()["observations"] if window.judgment else [],
                "review_checks": [
                    {
                        "aspect": "hook_and_payoff",
                        "status": "concern",
                        "reason": reason,
                        "observation_indices": [],
                    }
                ],
            },
            is_last_prefix=window.is_last_prefix,
        )
    )


def _summary_reason(reason: str, window: ScreenWindow, indices: list[int]) -> bool:
    """Conservative public-copy filtering, separate from numerical score gates.

    Source membership does not substantiate assumed audience size or a claim
    that niche subject matter inherently limits distribution. Keep the recorded
    rating, but do not promote such a reason into a confident public explanation.
    """
    if not _score_statement(reason):
        return False
    if boundary_validation_issues(
        {
            "observations": window.judgment.model_dump()["observations"] if window.judgment else [],
            "review_checks": [{"aspect": "hook_and_payoff", "status": "concern", "reason": reason,
                               "observation_indices": indices}],
        },
        is_last_prefix=window.is_last_prefix,
    ):
        return False
    own_words = re.sub(r'''"[^"\n]*"|“[^”\n]*”|‘[^’\n]*’|(?<!\w)'[^'\n]+'(?!\w)''', " ", reason)
    if not window.is_last_prefix and (
        re.search(r"\b(?:incomplete|unfinished|unresolved)\b[^.;!?]{0,100}\b(?:prefix|checkpoint|cutoff|review boundary)\b", own_words, re.I)
        or re.search(r"\b(?:prefix|checkpoint|cutoff|review boundary)\b[^.;!?]{0,100}\b(?:incomplete|unfinished|unresolved)\b", own_words, re.I)
        or re.search(r"\b(?:not|hasn['’]t|haven['’]t)\s+yet\s+(?:been\s+)?(?:reveal(?:ed)?|show(?:n)?|explain(?:ed)?|deliver(?:ed)?|provid(?:e|ed)|complet(?:e|ed))\b", own_words, re.I)
        or re.search(r"\b(?:cuts?|cutting|cut|stops?|ends?)\s+(?:off\s+)?before\b", own_words, re.I)
    ):
        observations = window.judgment.model_dump()["observations"] if window.judgment else []
        if not _observed_repetition({"observation_indices": indices}, observations):
            return False
    niche = re.search(r"\b(?:niche|religious|political|christian|faith[- ]based|specific audience)\b", own_words, re.I)
    limiting = re.search(
        r"\b(?:limit(?:s|ed|ing)?|restrict(?:s|ed|ing)?|reduc(?:e|es|ed|ing)|prevent(?:s|ed|ing)?|"
        r"narrow(?:s|ed|ing)?|unlikely|unshareable|smaller|less likely|not shareable|lacks? broad|"
        r"not\s+(?:have\s+)?(?:a\s+)?(?:broad|wide)\s+enough|rather\s+than\s+(?:a\s+)?broad)\b", own_words, re.I,
    )
    distribution = re.search(r"\b(?:viral(?:ity)?|sharing|shareable|shareability|shared|share|audience|reach|appeal|distribution|spread)\b", own_words, re.I)
    broad_comparison = re.search(r"\b(?:broad|broadly|general|wide|wider|widely|mass|compared)\b", own_words, re.I)
    niche_contrast = niche and re.search(r"\bbut\b", own_words, re.I)
    return not (distribution and (niche_contrast or (limiting and (niche or broad_comparison))))


def _display_reason(reason: str) -> str:
    """Only restore terminal punctuation; never truncate or rewrite a judgment."""
    value = reason.strip()
    return value if re.search(r"[.!。！][\"”’']?$", value) else value + "."


def _score_drivers(rows: list[tuple[int, ScreenWindow, Any]], score: int | None) -> list[dict[str, Any]]:
    if score is None:
        return []
    candidates = [(index, window, rating) for index, window, rating in rows
                  if _summary_reason(rating.reason, window, rating.observation_indices)]
    # Explain lower scores using lower-rated content, weighted by time, rather
    # than a fleeting low point. Stronger scores show the largest rated strengths.
    lower = score < 75
    selected = [row for row in candidates if row[2].rating < 3] if lower else [row for row in candidates if row[2].rating >= 3]
    selected.sort(key=lambda row: (
        -((4 - row[2].rating if lower else row[2].rating) * (row[1].end_ms - row[1].start_ms)),
        row[1].start_ms,
    ))
    if lower and selected:
        # Among equally rated drivers, an explicit ordinary-motive explanation
        # explains a middling score better than an entirely positive statement.
        same_rating = [row for row in selected if row[2].rating == selected[0][2].rating]
        ordinary = [row for row in same_rating if re.search(r"\b(?:ordinary|common|standard|moderate)\b", row[2].reason, re.I)]
        if ordinary:
            preferred = max(ordinary, key=lambda row: (row[1].end_ms - row[1].start_ms, -row[1].start_ms))
            selected.remove(preferred)
            selected.insert(0, preferred)
    drivers = []
    for index, window, rating in selected:
        reason = _display_reason(rating.reason)
        if any(driver["reason"] == reason for driver in drivers):
            continue
        drivers.append({"window_index": index, "start_ms": window.start_ms, "end_ms": window.end_ms,
                        "reason": reason, "observation_indices": list(rating.observation_indices)})
        if len(drivers) == 3:
            break
    return drivers


def build_scorecard(report: ScreenReport) -> dict[str, Any]:
    """Deterministic aggregation. No provider calls, outcome reads or record mutation."""
    duration = report.duration_ms
    ordered = sorted(report.windows, key=lambda w: w.start_ms)
    valid_timing = duration > 0 and all(0 <= w.start_ms < w.end_ms <= duration for w in ordered)
    valid_timing = valid_timing and all(
        a.end_ms <= b.start_ms for a, b in zip(ordered, ordered[1:], strict=False)
    )
    terminal = report.status in {"complete", "needs_review"}
    metrics: dict[str, Any] = {}
    timeline_ratings: dict[int, Any] = {}
    for dimension in _DIMENSIONS:
        rows = []
        for index, window in enumerate(report.windows if valid_timing else []):
            if window.status not in {"complete", "needs_review"} or not window.judgment:
                continue
            candidates = [score for score in window.judgment.potential_scores if score.dimension == dimension]
            if len(candidates) != 1:
                continue
            score = candidates[0]
            if (
                score.rating is None
                or not _score_statement(score.reason)
                or not _anchored(window, score.observation_indices)
            ):
                continue
            if _pending(score.reason, window):
                continue
            rows.append((index, window, score))
            if dimension == "retention" and _summary_reason(score.reason, window, score.observation_indices):
                timeline_ratings[index] = score
        rated_ms = sum(w.end_ms - w.start_ms for _, w, _ in rows)
        coverage = rated_ms / duration if duration > 0 else 0.0
        sufficient = coverage >= SCORE_PROTOCOL["minimum_duration_coverage"] and len(rows) >= min(
            3, len(report.windows)
        )
        value = (
            (sum((w.end_ms - w.start_ms) * s.rating * 25 for _, w, s in rows) / rated_ms) if rated_ms else None
        )
        provisional = coverage < 1 or not terminal or any(w.validation_issues for _, w, _ in rows)
        metric_score = int(math.floor(value + 0.5)) if terminal and sufficient and value is not None else None
        drivers = _score_drivers(rows, metric_score)
        metrics[dimension] = {
            "score": metric_score,
            "coverage": round(coverage, 4),
            "rated_ms": rated_ms,
            "total_ms": max(0, duration),
            "rated_sections": len(rows),
            "total_sections": len(report.windows),
            "provisional": provisional,
            "explanation": drivers[0]["reason"] if drivers else (
                "More reviewed evidence is needed to explain a whole-video score."
                if metric_score is None else
                "Sharing appeal is not established by the available explanation."
                if dimension == "virality" else
                "The score needs a better-supported explanation."
            ),
            "drivers": drivers,
            "reason": (
                "More reviewed evidence is needed for a whole-video score."
                if metric_score is None
                else "Based on the rated sections; unscored or disputed evidence may change this estimate."
                if provisional
                else "Subjective content rating across the reviewed sections, not an audience forecast."
            ),
        }
    timeline = []
    for index, window in enumerate(report.windows if valid_timing else []):
        rating = timeline_ratings.get(index)
        timeline.append({
            "window_index": index, "start_ms": window.start_ms, "end_ms": window.end_ms,
            "score": rating.rating * 25 if rating is not None else None,
            "reason": _display_reason(rating.reason) if rating is not None else None,
            "observation_indices": list(rating.observation_indices) if rating is not None else [],
        })
    improvements = []
    strengths = []
    priority = {
        aspect: i
        for i, aspect in enumerate(
            (
                "hook_and_payoff",
                "pacing",
                "visual_clarity",
                "caption_readability",
                "caption_alignment",
                "tone_from_words",
                "share_motivation",
            )
        )
    }
    for index, window in enumerate(report.windows if valid_timing else []):
        if window.status not in {"complete", "needs_review"} or not window.judgment:
            continue
        for check in window.judgment.review_checks:
            if check.status not in {"clear", "concern"} or check.aspect not in priority:
                continue
            if not _summary_reason(check.reason, window, check.observation_indices):
                continue
            if not _anchored(window, check.observation_indices) or any(
                issue.startswith(f"Review check {check.aspect}:") for issue in window.validation_issues
            ):
                continue
            if any(issue.startswith("Intermediate checkpoint:") for issue in window.validation_issues):
                continue
            finding = {
                "window_index": index, "start_ms": window.start_ms, "end_ms": window.end_ms,
                "aspect": check.aspect, "reason": _display_reason(check.reason),
                "observation_indices": list(check.observation_indices),
            }
            if check.status == "clear":
                strengths.append(finding)
                continue
            action = check.suggested_change if _sentence(check.suggested_change) else _ACTIONS[check.aspect]
            if not _summary_reason(action, window, check.observation_indices):
                action = _ACTIONS[check.aspect]
            improvements.append({**finding, "action": action})
    improvements.sort(key=lambda item: (priority[item["aspect"]], item["start_ms"]))
    unique = []
    for item in improvements:
        if not any((x["aspect"], x["action"]) == (item["aspect"], item["action"]) for x in unique):
            unique.append(item)
    strengths.sort(key=lambda item: (priority[item["aspect"]], -(item["end_ms"] - item["start_ms"]), item["start_ms"]))
    distinct_strengths = []
    for item in strengths:
        if not any(x["aspect"] == item["aspect"] or x["reason"] == item["reason"] for x in distinct_strengths):
            distinct_strengths.append(item)
        if len(distinct_strengths) == 3:
            break
    ready = all(metric["score"] is not None and not metric["provisional"] for metric in metrics.values())
    return {
        "version": SCORE_PROTOCOL["version"],
        "status": "complete"
        if ready
        else "partial"
        if any(m["score"] is not None for m in metrics.values())
        else "unavailable",
        "evidence_level": "model_rubric",
        "predicts_audience_outcomes": False,
        "metrics": metrics,
        "timeline": timeline,
        "strengths": distinct_strengths,
        "improvements": unique[:3],
        "method": "Each cited 0–4 model rating is scaled to 0–100, then weighted by section duration. At least 60% duration coverage and three sections (or all sections for shorter clips) are required. Missing evidence is excluded, never treated as zero.",
        "limitations": [
            "These are subjective content indices, not retention percentages, predicted views or probabilities of going viral.",
            "No calibration against real audience outcomes is available for these scores.",
            "Scores summarize sampled frames and transcript evidence; native vocal delivery is not assessed.",
        ],
    }
