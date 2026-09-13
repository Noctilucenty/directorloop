"""Local timing measurements, not a judgment of voice, captions or the audience.

The transcript is ASR output: arithmetic on its timestamps is mechanical, while
the transcript itself can be wrong. Nothing here decodes media or calls a model.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any, Literal, TypedDict

from ..creative.signals import RMS_HOP_MS
from ..media.transcribe import Transcript


class DeliverySignal(TypedDict):
    status: Literal["measured", "partial", "unknown"]
    evidence_level: Literal["mechanical"]
    value: dict[str, Any] | None
    limitation: str


class DeliveryMeasurements(TypedDict):
    version: str
    evidence_level: Literal["mechanical"]
    start_ms: int
    end_ms: int
    signals: dict[str, DeliverySignal]


def _signal(value: dict[str, Any] | None, limitation: str, *, partial: bool = False) -> DeliverySignal:
    return {"status": "unknown" if value is None else "partial" if partial else "measured",
            "evidence_level": "mechanical", "value": value, "limitation": limitation}


def _integer(value: Any) -> bool:
    return type(value) is int


def _valid_span(start: Any, end: Any) -> bool:
    return _integer(start) and _integer(end) and 0 <= start < end


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = merged[-1][0], max(merged[-1][1], end)
        else:
            merged.append((start, end))
    return merged


def _interval_summary(spans: list[tuple[int, int]], min_gap_ms: int) -> dict[str, Any]:
    long = [(a, b) for a, b in spans if b - a >= min_gap_ms]
    longest = sorted(long, key=lambda pair: (-(pair[1] - pair[0]), pair[0]))[:5]
    return {"minimum_duration_ms": min_gap_ms, "count": len(long),
            "total_duration_ms": sum(b - a for a, b in long),
            "longest_duration_ms": max((b - a for a, b in long), default=0),
            "longest_intervals": [{"start_ms": a, "end_ms": b, "duration_ms": b - a}
                                  for a, b in sorted(longest)],
            "intervals_truncated": len(long) > len(longest)}


def _word_count(text: str) -> int:
    # Preserve the existing transcript's whitespace boundaries; punctuation is not a word.
    return sum(any(char.isalnum() for char in word) for word in text.split())


def _transcript_units(transcript: Transcript, start_ms: int, end_ms: int) -> tuple[list[tuple[int, int, int]], bool]:
    """Completed token words or whole segments; never interpolate word timing."""
    units: list[tuple[int, int, int]] = []
    incomplete = False
    for segment in transcript.segments:
        if _integer(segment.start_ms) and segment.start_ms >= end_ms:
            continue
        if _integer(segment.end_ms) and segment.end_ms <= start_ms:
            continue
        if not _valid_span(segment.start_ms, segment.end_ms):
            incomplete = True
            continue
        if not segment.tokens:
            # A segment crossing either boundary has no observed within-window word count.
            if segment.start_ms < start_ms or segment.end_ms > end_ms:
                incomplete = True
                continue
            count = _word_count(segment.text)
            if count:
                units.append((segment.start_ms, segment.end_ms, count))
            continue
        words: list[tuple[int, int, str]] = []
        for token in segment.tokens:
            if not _valid_span(token.start_ms, token.end_ms) or not (
                segment.start_ms <= token.start_ms < token.end_ms <= segment.end_ms
            ):
                if not _integer(token.start_ms) or token.start_ms < end_ms:
                    incomplete = True
                continue
            if not token.text.strip() or token.text.startswith("[_"):
                continue
            if words and not token.text[0].isspace():
                a, b, text = words[-1]
                words[-1] = min(a, token.start_ms), max(b, token.end_ms), text + token.text
            else:
                words.append((token.start_ms, token.end_ms, token.text))
        for a, b, text in words:
            # A word ending at start belongs to the previous window. A word crossing
            # end is not yet fully heard; its text or partial count must not leak.
            if start_ms < b <= end_ms and (count := _word_count(text)):
                units.append((max(a, start_ms), b, count))
    return units, incomplete


def _quantile90(values: list[float]) -> float:
    ordered = sorted(values)
    index = (len(ordered) - 1) * 0.9
    lo, hi = math.floor(index), math.ceil(index)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (index - lo)


def measure_delivery(
    transcript: Transcript | None,
    rms: Sequence[float] | None,
    start_ms: int,
    end_ms: int,
    *,
    asr_available: bool | None = None,
    min_gap_ms: int = 600,
    rms_hop_ms: int = RMS_HOP_MS,
) -> DeliveryMeasurements:
    """Return five serializable records for the half-open review window.

    Pass ``asr_available=False`` for failed/missing ASR or a missing audio stream.
    ``None`` conservatively recognizes the current Transcript missing-data notes.
    RMS bins are anchored at video zero; only bins completed by end_ms contribute.
    No fresh transcription, interpolation, VAD, OCR or provider calls are made.
    """
    if not _valid_span(start_ms, end_ms):
        raise ValueError("Review boundaries must be nonnegative integer milliseconds with end > start")
    if not _integer(min_gap_ms) or min_gap_ms <= 0 or not _integer(rms_hop_ms) or rms_hop_ms <= 0:
        raise ValueError("Gap and RMS hop must be positive integer milliseconds")
    if asr_available is not None and type(asr_available) is not bool:
        raise ValueError("asr_available must be bool or None")
    duration = end_ms - start_ms
    signals: dict[str, DeliverySignal] = {}
    note = transcript.note.casefold() if transcript else "missing"
    available = transcript is not None and asr_available is not False
    if asr_available is None and any(word in note for word in ("unavailable", "missing", "failed", "no audio")):
        available = False
    if transcript is not None and transcript.text.strip() and not transcript.segments:
        available = False
    unknown_asr = "Timed ASR is unavailable; absence of transcription is not evidence of silence."
    if not available or transcript is None:
        for name in ("transcript_word_rate", "transcript_coverage", "transcript_gaps"):
            signals[name] = _signal(None, unknown_asr)
    else:
        units, incomplete = _transcript_units(transcript, start_ms, end_ms)
        spans = _merge([(a, b) for a, b, _ in units])
        count = sum(n for _, _, n in units)
        timed_ms = sum(b - a for a, b in spans)
        # Duplicated/overlapping ASR can inflate word counts, even though union
        # coverage never double-counts time. Keep such rates unknown.
        overlap = sum(b - a for a, b, _ in units) > timed_ms
        rate = None if incomplete or overlap else round(count * 60000 / duration, 2)
        signals["transcript_word_rate"] = _signal(
            {"observed_word_count": count, "words_per_minute": rate, "denominator_ms": duration},
            "Completed whitespace-delimited ASR words per whole window, not syllable speed, subtitle reading rate, tone or pacing quality. Boundary-crossing untimed segments or overlapping ASR make the rate unknown.",
            partial=incomplete or overlap,
        )
        signals["transcript_coverage"] = _signal(
            {"timed_transcript_ms": timed_ms, "fraction": None if incomplete else round(timed_ms / duration, 4)},
            "Union of completed ASR word/segment spans, not a speech detector. Segment spans can include pauses; ASR can miss or invent speech.",
            partial=incomplete,
        )
        gaps: list[tuple[int, int]] = []
        cursor = start_ms
        for a, b in spans:
            if a > cursor:
                gaps.append((cursor, a))
            cursor = b
        if cursor < end_ms:
            gaps.append((cursor, end_ms))
        signals["transcript_gaps"] = _signal(
            None if incomplete else _interval_summary(gaps, min_gap_ms),
            "Intervals without a completed ASR span, including leading/trailing gaps. These are not confirmed vocal pauses and can contain music or missed speech.",
        )

    bins: list[tuple[int, int, float]] = []
    reference: list[float] = []
    if rms is not None:
        for index in range(min(len(rms), end_ms // rms_hop_ms)):
            value = float(rms[index])
            if not math.isfinite(value) or value < 0:
                continue
            reference.append(value)
            a, b = max(start_ms, index * rms_hop_ms), (index + 1) * rms_hop_ms
            if a < b:
                bins.append((a, b, value))
    if not bins:
        signals["quiet_audio"] = _signal(None, "No valid RMS bins in this window; missing audio energy is not silence.")
    else:
        threshold = max(0.000001, _quantile90(reference) * 0.18)
        quiet = _merge([(a, b) for a, b, value in bins if value < threshold])
        covered_ms = sum(b - a for a, b, _ in bins)
        signals["quiet_audio"] = _signal(
            {"fraction_of_measured_audio": round(sum(b - a for a, b in quiet) / covered_ms, 4),
             "measured_audio_ms": covered_ms, "window_coverage_fraction": round(covered_ms / duration, 4),
             "threshold_rms": round(threshold, 9), "rms_hop_ms": rms_hop_ms,
             "long_quiet_intervals": _interval_summary(quiet, min_gap_ms)},
            "RMS below 18% of the prefix's 90th percentile (floor 0.000001), using completed bins only. Low mixed-track energy is not speech silence, vocal emotion or an editing recommendation.",
            partial=covered_ms < duration,
        )
    signals["caption_readability"] = _signal(
        None, "No caption pixels, OCR boxes, font sizes or display durations were supplied. ASR text cannot establish caption presence, accuracy, readability or sync."
    )
    return {"version": "delivery-timing-v1", "evidence_level": "mechanical",
            "start_ms": start_ms, "end_ms": end_ms, "signals": signals}
