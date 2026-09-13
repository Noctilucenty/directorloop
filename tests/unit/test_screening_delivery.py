"""Deterministic delivery evidence uses synthetic transcripts and RMS only."""

from __future__ import annotations

import json

import pytest

from directorloop.media.transcribe import Transcript, TranscriptSegment, TranscriptToken
from directorloop.screening.delivery import measure_delivery


def test_completed_transcript_rate_union_coverage_and_gap_locations():
    transcript = Transcript("one two three four", [
        TranscriptSegment(100, 500, "one two"), TranscriptSegment(1500, 2000, "three four"),
    ])
    result = measure_delivery(transcript, [0.1] * 200, 0, 2000)
    signals = result["signals"]
    assert signals["transcript_word_rate"]["value"] == {
        "observed_word_count": 4, "words_per_minute": 120.0, "denominator_ms": 2000,
    }
    assert signals["transcript_coverage"]["value"] == {"timed_transcript_ms": 900, "fraction": 0.45}
    assert signals["transcript_gaps"]["value"]["longest_intervals"] == [
        {"start_ms": 500, "end_ms": 1500, "duration_ms": 1000},
    ]
    assert signals["caption_readability"]["status"] == "unknown"
    assert all(signal["evidence_level"] == "mechanical" for signal in signals.values())
    assert json.loads(json.dumps(result, allow_nan=False)) == result


@pytest.mark.parametrize("transcript", [
    None,
    Transcript("", has_speech=False, note="whisper.cpp unavailable; transcript missing"),
    Transcript("", has_speech=False, note="no audio stream"),
    Transcript("text without timing"),
])
def test_missing_asr_is_unknown_not_zero_speech(transcript):
    signals = measure_delivery(transcript, None, 0, 2000)["signals"]
    for name in ("transcript_word_rate", "transcript_coverage", "transcript_gaps", "quiet_audio"):
        assert signals[name]["status"] == "unknown"
        assert signals[name]["value"] is None


def test_explicit_unavailable_overrides_stale_transcript_and_silence_is_distinct():
    transcript = Transcript("stale words", [TranscriptSegment(0, 1000, "stale words")])
    missing = measure_delivery(transcript, None, 0, 1000, asr_available=False)
    assert missing["signals"]["transcript_coverage"]["value"] is None
    silence = measure_delivery(Transcript("", has_speech=False, note="no speech detected"), [0.0] * 100, 0, 1000)
    assert silence["signals"]["transcript_word_rate"]["value"]["words_per_minute"] == 0
    assert silence["signals"]["quiet_audio"]["value"]["fraction_of_measured_audio"] == 1
    assert silence["signals"]["transcript_gaps"]["value"]["longest_duration_ms"] == 1000


def test_subword_tokens_are_joined_and_future_word_is_not_counted():
    transcript = Transcript("Unfinished words arrive later", [TranscriptSegment(0, 3000, "Unfinished words arrive later", (
        TranscriptToken(0, 100, "Un"), TranscriptToken(100, 400, "finished"),
        TranscriptToken(450, 900, " words"), TranscriptToken(950, 1100, " arrive"),
        TranscriptToken(1600, 2500, " later"),
    ))])
    signals = measure_delivery(transcript, None, 0, 1000)["signals"]
    assert signals["transcript_word_rate"]["value"]["observed_word_count"] == 2
    assert signals["transcript_word_rate"]["value"]["words_per_minute"] == 120
    assert signals["transcript_coverage"]["value"]["timed_transcript_ms"] == 850
    assert "later" not in json.dumps(signals)


def test_completed_word_crossing_start_counts_once_and_clips_timed_coverage():
    transcript = Transcript("one two", [TranscriptSegment(0, 1500, "one two", (
        TranscriptToken(0, 1000, "one"), TranscriptToken(1000, 1500, " two"),
    ))])
    signals = measure_delivery(transcript, None, 1000, 2000)["signals"]
    assert signals["transcript_word_rate"]["value"]["observed_word_count"] == 1
    assert signals["transcript_coverage"]["value"]["timed_transcript_ms"] == 500


def test_untimed_segment_straddling_review_boundary_is_not_interpolated():
    transcript = Transcript("begin secret ending", [TranscriptSegment(0, 3000, "begin secret ending")])
    signals = measure_delivery(transcript, None, 0, 2000)["signals"]
    assert signals["transcript_word_rate"]["status"] == "partial"
    assert signals["transcript_word_rate"]["value"]["words_per_minute"] is None
    assert signals["transcript_coverage"]["value"]["fraction"] is None
    assert signals["transcript_gaps"]["status"] == "unknown"


def test_overlapping_asr_does_not_double_count_time_or_claim_known_rate():
    transcript = Transcript("one two", [TranscriptSegment(0, 1500, "one"), TranscriptSegment(1000, 2000, "two")])
    signals = measure_delivery(transcript, None, 0, 2000)["signals"]
    assert signals["transcript_coverage"]["value"] == {"timed_transcript_ms": 2000, "fraction": 1.0}
    assert signals["transcript_word_rate"]["value"]["words_per_minute"] is None
    assert signals["transcript_word_rate"]["status"] == "partial"


def test_future_audio_and_future_transcript_cannot_change_prefix_measurements():
    prefix = Transcript("one", [TranscriptSegment(0, 500, "one")])
    later = Transcript("one future", [*prefix.segments, TranscriptSegment(2000, 9000, "future")])
    rms = [0.1] * 50 + [0.001] * 100 + [0.1] * 50
    expected = measure_delivery(prefix, rms, 0, 2000)
    actual = measure_delivery(later, [*rms, *([1000.0] * 1000)], 0, 2000)
    assert actual == expected
    quiet = actual["signals"]["quiet_audio"]["value"]
    assert quiet["fraction_of_measured_audio"] == 0.5
    assert quiet["threshold_rms"] == 0.018
    assert quiet["long_quiet_intervals"]["longest_intervals"] == [
        {"start_ms": 500, "end_ms": 1500, "duration_ms": 1000},
    ]


def test_partial_rms_bins_and_invalid_samples_are_explicit_missing_data():
    signals = measure_delivery(None, [float("nan"), 0.0, float("inf"), -1.0, 0.0], 15, 49)["signals"]
    quiet = signals["quiet_audio"]
    assert quiet["status"] == "partial"
    assert quiet["value"]["measured_audio_ms"] == 5
    assert quiet["value"]["fraction_of_measured_audio"] == 1
    assert quiet["value"]["window_coverage_fraction"] == round(5 / 34, 4)
    json.dumps(signals, allow_nan=False)


def test_invalid_timed_asr_marks_gaps_unknown_instead_of_inventing_silence():
    transcript = Transcript("invalid", [TranscriptSegment(True, 1000, "invalid")])
    signals = measure_delivery(transcript, None, 0, 2000)["signals"]
    assert signals["transcript_word_rate"]["value"]["words_per_minute"] is None
    assert signals["transcript_gaps"]["value"] is None


@pytest.mark.parametrize("start,end,kwargs", [
    (True, 1000, {}), (0, 1000.0, {}), (-1, 1000, {}), (1000, 1000, {}),
    (0, 1000, {"rms_hop_ms": False}), (0, 1000, {"min_gap_ms": 0}),
    (0, 1000, {"asr_available": "yes"}),
])
def test_strict_millisecond_and_availability_validation(start, end, kwargs):
    with pytest.raises(ValueError):
        measure_delivery(None, None, start, end, **kwargs)


def test_unbounded_gap_lists_are_compact_but_truncation_is_explicit():
    rms = ([0.0] * 60 + [0.1] * 10) * 9
    gaps = measure_delivery(None, rms, 0, 6300)["signals"]["quiet_audio"]["value"]["long_quiet_intervals"]
    assert gaps["count"] == 9 and gaps["intervals_truncated"] is True
    assert len(gaps["longest_intervals"]) == 5
    assert gaps["total_duration_ms"] == 5400
