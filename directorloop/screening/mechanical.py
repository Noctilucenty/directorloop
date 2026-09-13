"""Measure changes in already supplied JPEGs without additional decoding or inference."""

from __future__ import annotations

import io

import numpy as np
from PIL import Image, UnidentifiedImageError

from ..domain.ids import sha256_bytes
from ..media.frames import SampledFrame
from .models import ScreenFrameChange, ScreenMechanicalVisual

MECHANICAL_PROTOCOL = {
    "version": "sampled-luma-change-v1",
    "scope": "current window supplied frames only; requested timestamp, not decoded PTS",
    "resize": [64, 64], "resampling": "Pillow LANCZOS", "color": "Pillow L 0..255",
    "changed_pixel_threshold": 8, "comparison": "absolute luma difference > threshold",
    "duplicate_timestamp": "highest resolution then SHA256; one image per timestamp",
    "aggregation": "unweighted mean across adjacent sample pairs; not normalized by gap",
    "semantic_motion_verified": False, "attention_or_retention_measurement": False,
}


def measure_frame_changes(frames: list[SampledFrame], start_ms: int, end_ms: int) -> ScreenMechanicalVisual:
    result = ScreenMechanicalVisual(status="insufficient_frames", start_ms=start_ms, end_ms=end_ms)
    selected = {}
    for frame in frames:
        if start_ms <= frame.timestamp_ms < end_ms:
            old = selected.get(frame.timestamp_ms)
            if old is None or (frame.width * frame.height, sha256_bytes(frame.jpeg)) > (old.width * old.height, sha256_bytes(old.jpeg)):
                selected[frame.timestamp_ms] = frame
    ordered = [selected[t] for t in sorted(selected)]
    result.sample_timestamps_ms = [f.timestamp_ms for f in ordered]
    result.sample_sha256 = [sha256_bytes(f.jpeg) for f in ordered]
    if len(ordered) < 2:
        return result
    try:
        gray = []
        for frame in ordered:
            with Image.open(io.BytesIO(frame.jpeg)) as image:
                gray.append(np.asarray(image.convert("L").resize((64, 64), Image.Resampling.LANCZOS), dtype=np.int16))
    except (UnidentifiedImageError, OSError, ValueError):
        result.status, result.error = "unavailable", "A supplied frame could not be decoded for pixel comparison"
        return result
    for index in range(1, len(ordered)):
        delta = np.abs(gray[index] - gray[index - 1])
        result.pairs.append(ScreenFrameChange(
            from_ms=ordered[index - 1].timestamp_ms, to_ms=ordered[index].timestamp_ms,
            gap_ms=ordered[index].timestamp_ms - ordered[index - 1].timestamp_ms,
            mean_absolute_luma_delta=round(float(delta.mean()), 6),
            changed_pixel_fraction=round(float((delta > result.changed_pixel_threshold).mean()), 6),
        ))
    result.status = "measured"
    result.mean_absolute_luma_delta = round(float(np.mean([pair.mean_absolute_luma_delta for pair in result.pairs])), 6)
    result.mean_changed_pixel_fraction = round(float(np.mean([pair.changed_pixel_fraction for pair in result.pairs])), 6)
    return result
