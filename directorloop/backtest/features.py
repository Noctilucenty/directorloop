"""Outcome-blind file and ASR features for historical backtests.

This module never reads Instagram metrics or calls a remote model. Mechanical
measurements and local ASR-derived text features remain separately attributed.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from pathlib import Path
from typing import Any

import numpy as np

from ..audit.review import words_from_transcript
from ..creative.signals import (
    RMS_HOP_MS,
    SCENE_THRESHOLD,
    audio_rms,
    detect_cuts,
    motion_curve,
    static_spans,
)
from ..domain.ids import sha256_file, utc_now_iso
from ..media.probe import ffmpeg_version, inspect_media
from ..media.transcribe import DEFAULT_MODEL, Transcript, transcribe

FEATURE_VERSION = "blind-mechanical-features-v1"
STATIC_MOTION_THRESHOLD = 0.012
QUIET_RELATIVE_THRESHOLD = 0.18
ASR_LOCK = threading.Lock()

MECHANICAL_FEATURE_NAMES = (
    "duration_seconds", "cut_rate_per_second", "speech_words_per_second",
    "silence_fraction", "longest_static_fraction", "mean_motion",
    "opening_motion", "mean_loudness",
)


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def overlap_ms(intervals: list[tuple[int, int]], start: int, end: int) -> int:
    """Union length: overlapping signals must not double-count video duration."""
    spans = sorted((max(start, a), min(end, b)) for a, b in intervals if a < end and b > start)
    total, current_start, current_end = 0, None, None
    for a, b in spans:
        if current_end is None or a > current_end:
            if current_end is not None:
                total += current_end - current_start
            current_start, current_end = a, b
        else:
            current_end = max(current_end, b)
    if current_end is not None:
        total += current_end - current_start
    return int(total)


def _mean(values: list[float]) -> float | None:
    return float(np.mean(values)) if values else None


def _quiet_fraction(rms: np.ndarray, start: int, end: int, reference_end: int) -> float | None:
    """Near-silence relative to the 90th percentile heard by this boundary."""
    prefix = rms[:min(len(rms), max(1, reference_end // RMS_HOP_MS))]
    sample = rms[start // RMS_HOP_MS:min(len(rms), end // RMS_HOP_MS)]
    if not len(prefix) or not len(sample):
        return None
    reference = float(np.percentile(prefix, 90)) or 1e-6
    return float(np.mean(sample < QUIET_RELATIVE_THRESHOLD * reference))


def summarize_measurements(*, duration_ms: int, cuts_ms: list[int] | None,
                           motion: list[tuple[int, float]] | None, rms: np.ndarray | None,
                           transcript: Transcript | None, window_ms: int = 2000) -> dict[str, Any]:
    """Pure aggregation, shared by the extractor and deterministic fixture tests."""
    if duration_ms <= 0 or window_ms <= 0:
        raise ValueError("Positive duration and window size are required")
    duration_s = duration_ms / 1000
    cuts = sorted(set(t for t in (cuts_ms or []) if 0 < t < duration_ms))
    available_motion = motion is not None and bool(motion)
    still = static_spans(cuts, motion or [], duration_ms, STATIC_MOTION_THRESHOLD) if available_motion and cuts_ms is not None else None
    words = words_from_transcript(transcript) if transcript is not None else None
    # An unavailable/failed ASR result is unknown, never zero spoken words.
    if transcript is not None and any(term in transcript.note.lower() for term in ("unavailable", "missing", "failed", "no audio:")):
        words = None
    rms = rms if rms is not None and len(rms) else None
    values: dict[str, float | None] = {
        "duration_seconds": duration_s,
        "cut_rate_per_second": len(cuts) / duration_s if cuts_ms is not None else None,
        "speech_words_per_second": len(words) / duration_s if words is not None else None,
        "silence_fraction": _quiet_fraction(rms, 0, duration_ms, duration_ms) if rms is not None else None,
        "longest_static_fraction": max((b - a for a, b in still), default=0) / duration_ms if still is not None else None,
        "mean_motion": _mean([v for _, v in motion or []]) if available_motion else None,
        "opening_motion": _mean([v for t, v in motion or [] if t <= 1000]) if available_motion else None,
        "mean_loudness": float(np.mean(rms)) if rms is not None else None,
    }
    windows = []
    for start in range(0, duration_ms, window_ms):
        end = min(duration_ms, start + window_ms)
        spoken = [w for w in words if start < w.end_ms <= end] if words is not None else None
        windows.append({
            "start_ms": start, "end_ms": end,
            "mean_motion": _mean([v for t, v in motion or [] if start < t <= end]) if available_motion else None,
            "static_fraction": overlap_ms(still, start, end) / (end - start) if still is not None else None,
            "silence_fraction": _quiet_fraction(rms, start, end, end) if rms is not None else None,
            "word_count": len(spoken) if spoken is not None else None,
            "speech_words_per_second": len(spoken) / ((end - start) / 1000) if spoken is not None else None,
            "cut_count": sum(start < t <= end for t in cuts) if cuts_ms is not None else None,
            "prefix_audio_reference_end_ms": end,
        })
    return {"features": values, "windows": windows,
            "measurements": {"cuts_ms": cuts if cuts_ms is not None else None,
                             "static_spans_ms": still, "transcribed_word_count": len(words) if words is not None else None,
                             "first_transcribed_word_start_ms": min((w.start_ms for w in words), default=None) if words is not None else None},
            "missingness": {k: "measurement unavailable" for k, v in values.items() if v is None}}


def extract_mechanical_features(media_path: str | Path, *, media_id: str, expected_sha256: str,
                                window_ms: int = 2000, asr_language: str = "auto",
                                cache_dir: str | Path | None = None) -> dict[str, Any]:
    """Extract from exact media bytes. Paths and external metadata are not returned."""
    if not re.fullmatch(r"[a-f0-9]{64}", expected_sha256):
        raise ValueError("Expected a full SHA-256")
    path = Path(media_path)
    if sha256_file(path) != expected_sha256:
        raise ValueError("Media hash mismatch; refusing features for an ambiguous artifact")
    configuration = {"version": FEATURE_VERSION, "module_sha256": sha256_file(Path(__file__)),
                     "window_ms": window_ms, "asr_language": asr_language,
                     "asr_model_sha256": sha256_file(DEFAULT_MODEL) if DEFAULT_MODEL.exists() else None,
                     "ffmpeg_version": ffmpeg_version(), "scene_threshold": SCENE_THRESHOLD,
                     "static_motion_threshold": STATIC_MOTION_THRESHOLD, "quiet_relative_threshold": QUIET_RELATIVE_THRESHOLD}
    fingerprint = canonical_hash(configuration)
    cache = Path(cache_dir) / expected_sha256 / (fingerprint + ".json") if cache_dir is not None else None
    if cache is not None and cache.exists():
        result = json.loads(cache.read_text())
        if result.get("artifact_sha256") != expected_sha256 or result.get("extractor_fingerprint") != fingerprint:
            raise ValueError("Cached feature identity mismatch")
        # Reuse is preprocessing only; the original extraction timestamp remains intact.
        return {**result, "media_id": media_id, "preprocessing_mode": "cached"}
    info = inspect_media(path)
    duration = int(info.duration_ms or 0)
    if duration <= 0 or not info.width or not info.height:
        raise ValueError("A video with measured duration and dimensions is required")
    errors: dict[str, str] = {}
    try:
        cuts = detect_cuts(path)
    except Exception as exc:  # preserve other independent measurements
        cuts = None
        errors["cuts"] = type(exc).__name__
    try:
        motion = motion_curve(path, info.width, info.height)
    except Exception as exc:
        motion = None
        errors["motion"] = type(exc).__name__
    try:
        rms = audio_rms(path) if info.has_audio else None
    except Exception as exc:
        rms = None
        errors["audio_energy"] = type(exc).__name__
    transcript = None
    if info.has_audio:
        try:
            with ASR_LOCK:
                transcript = transcribe(path, language=asr_language)
        except Exception as exc:
            errors["transcript"] = type(exc).__name__
    result = summarize_measurements(duration_ms=duration, cuts_ms=cuts, motion=motion, rms=rms,
                                    transcript=transcript, window_ms=window_ms)
    if sha256_file(path) != expected_sha256:
        raise ValueError("Media changed during extraction")
    result.update({"media_id": media_id, "artifact_sha256": expected_sha256, "feature_version": FEATURE_VERSION,
                   "extractor_fingerprint": fingerprint, "extractor_configuration": configuration,
                   "created_at": utc_now_iso(), "preprocessing_mode": "fresh", "duration_ms": duration,
                   "media": {"width": info.width, "height": info.height, "fps": info.fps, "has_audio": info.has_audio},
                   "asr": {"requested_language": asr_language,
                           "detected_language": transcript.detected_language if transcript else None,
                           "source": transcript.source if transcript else None,
                           "model": transcript.model if transcript else None,
                           "note": transcript.note if transcript else "transcript unavailable"},
                   "measurement_errors": errors,
                   "evidence_types": {"video_and_audio_energy": "MECHANICAL SIGNAL", "speech_features": "LOCAL ASR DERIVED SIGNAL"},
                   "limitations": ["Static frames and near-silence can be intentional; neither proves disengagement.",
                                   "Cuts and pixel difference do not measure information progress, semantic novelty, or clarity.",
                                   "ASR language detection and words are fallible, especially with mixed speech, singing, or names.",
                                   "No caption OCR, first meaningful information, product understanding, payoff, or native audio judgment is inferred mechanically."]})
    if any(v is not None and not math.isfinite(v) for v in result["features"].values()):
        raise ValueError("Non-finite mechanical feature")
    if cache is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".tmp")
        tmp.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
        tmp.replace(cache)
    return result
