"""Mechanical signals from a rendered video: shot cuts, motion curve, audio energy.

Deterministic measurements with explicit thresholds. They describe the file, not
the audience.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..media.probe import FFMPEG, MediaError

SCENE_THRESHOLD = 0.30
MOTION_FPS = 10
MOTION_WIDTH = 96
AUDIO_SR = 16000
RMS_HOP_MS = 10


@dataclass
class VideoSignals:
    duration_ms: int
    width: int
    height: int
    cuts_ms: list[int]  # timestamps of detected shot cuts
    motion: list[tuple[int, float]]  # (t_ms, mean abs frame difference 0-1)
    rms: np.ndarray  # RMS per RMS_HOP_MS window, 0-1 float
    scene_threshold: float = SCENE_THRESHOLD

    def rms_at(self, t_ms: int) -> float:
        i = min(len(self.rms) - 1, max(0, t_ms // RMS_HOP_MS))
        return float(self.rms[i]) if len(self.rms) else 0.0


def detect_cuts(path: str | Path, threshold: float = SCENE_THRESHOLD, timeout: int = 120) -> list[int]:
    """Shot cut timestamps from ffmpeg's scene score on a downscaled copy."""
    cmd = [
        FFMPEG, "-nostdin", "-hide_banner", "-i", str(path),
        "-vf", f"scale=160:-2,select='gt(scene,{threshold})',showinfo",
        "-an", "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise MediaError(f"scene detection failed: {proc.stderr[-200:]}")
    cuts = [int(round(float(m) * 1000)) for m in re.findall(r"pts_time:([0-9.]+)", proc.stderr)]
    return sorted(set(c for c in cuts if c > 0))


def motion_curve(path: str | Path, width: int, height: int, fps: int = MOTION_FPS, analysis_width: int = MOTION_WIDTH) -> list[tuple[int, float]]:
    """Mean absolute grayscale difference between consecutive sampled frames, normalized to 0-1."""
    ah = int(round(height * analysis_width / width / 2)) * 2
    cmd = [
        FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path),
        "-vf", f"fps={fps},scale={analysis_width}:{ah}", "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=180, check=False)
    if proc.returncode != 0:
        raise MediaError(f"motion decode failed: {proc.stderr.decode(errors='ignore')[-200:]}")
    frame_px = analysis_width * ah
    n = len(proc.stdout) // frame_px
    if n < 2:
        return []
    stack = np.frombuffer(proc.stdout[: n * frame_px], dtype=np.uint8).reshape(n, ah, analysis_width).astype(np.int16)
    diffs = np.abs(np.diff(stack, axis=0)).mean(axis=(1, 2)) / 255.0
    step = 1000 / fps
    return [(int(round((i + 1) * step)), float(v)) for i, v in enumerate(diffs)]


def audio_rms(path: str | Path, hop_ms: int = RMS_HOP_MS) -> np.ndarray:
    cmd = [
        FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path),
        "-vn", "-ac", "1", "-ar", str(AUDIO_SR), "-f", "f32le", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=120, check=False)
    if proc.returncode != 0 or not proc.stdout:
        return np.zeros(0, dtype=np.float32)
    pcm = np.frombuffer(proc.stdout, dtype=np.float32)
    hop = int(AUDIO_SR * hop_ms / 1000)
    n = len(pcm) // hop
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    frames = pcm[: n * hop].reshape(n, hop)
    return np.sqrt((frames.astype(np.float64) ** 2).mean(axis=1)).astype(np.float32)


def extract_signals(path: str | Path, duration_ms: int, width: int, height: int) -> VideoSignals:
    return VideoSignals(
        duration_ms=duration_ms,
        width=width,
        height=height,
        cuts_ms=detect_cuts(path),
        motion=motion_curve(path, width, height),
        rms=audio_rms(path),
    )


def snap_to_quiet(rms: np.ndarray, t_ms: int, window_ms: int = 220, hop_ms: int = RMS_HOP_MS) -> int:
    """Move a cut to the quietest 10 ms window near t_ms (pauses between words, even under music)."""
    if len(rms) == 0:
        return t_ms
    c = t_ms // hop_ms
    lo = max(0, c - window_ms // hop_ms)
    hi = min(len(rms), c + window_ms // hop_ms + 1)
    if hi <= lo:
        return t_ms
    # smooth over 3 windows so one quiet sample inside a word does not win
    seg = rms[lo:hi]
    if len(seg) >= 3:
        seg = np.convolve(seg, np.ones(3) / 3, mode="same")
    return int((lo + int(np.argmin(seg))) * hop_ms)


def static_spans(cuts_ms: list[int], motion: list[tuple[int, float]], duration_ms: int, motion_threshold: float = 0.012) -> list[tuple[int, int]]:
    """Intervals with no shot cut and motion below the threshold (candidate visual stagnation)."""
    boundaries = [0] + [c for c in cuts_ms if 0 < c < duration_ms] + [duration_ms]
    spans: list[tuple[int, int]] = []
    for a, b in zip(boundaries, boundaries[1:], strict=False):
        start = None
        for t, v in motion:
            if t < a or t > b:
                continue
            if v < motion_threshold:
                start = t if start is None else start
            else:
                if start is not None and t - start >= 600:
                    spans.append((start, t))
                start = None
        if start is not None and b - start >= 600:
            spans.append((start, b))
    return sorted(spans, key=lambda s: s[1] - s[0], reverse=True)


def first_visual_change_ms(cuts_ms: list[int], motion: list[tuple[int, float]], min_ms: int = 150, motion_spike: float = 0.05) -> int | None:
    """First shot cut or large motion spike after min_ms."""
    candidates = [c for c in cuts_ms if c >= min_ms]
    spikes = [t for t, v in motion if t >= min_ms and v >= motion_spike]
    if spikes:
        candidates.append(spikes[0])
    return min(candidates) if candidates else None


def first_meaningful_motion_ms(motion: list[tuple[int, float]], threshold: float = 0.02) -> int | None:
    for t, v in motion:
        if v >= threshold:
            return t
    return None


def silence_gaps(rms: np.ndarray, start_ms: int, end_ms: int, rel_threshold: float = 0.18, min_gap_ms: int = 350, hop_ms: int = RMS_HOP_MS) -> list[tuple[int, int]]:
    """Quiet stretches inside an interval, relative to the interval's speech level."""
    if len(rms) == 0:
        return []
    a, b = start_ms // hop_ms, min(len(rms), end_ms // hop_ms)
    seg = rms[a:b]
    if len(seg) == 0:
        return []
    level = float(np.percentile(seg, 90)) or 1e-6
    quiet = seg < rel_threshold * level
    gaps: list[tuple[int, int]] = []
    run_start = None
    for i, q in enumerate(quiet):
        if q and run_start is None:
            run_start = i
        elif not q and run_start is not None:
            if (i - run_start) * hop_ms >= min_gap_ms:
                gaps.append(((a + run_start) * hop_ms, (a + i) * hop_ms))
            run_start = None
    if run_start is not None and (len(quiet) - run_start) * hop_ms >= min_gap_ms:
        gaps.append(((a + run_start) * hop_ms, (a + len(quiet)) * hop_ms))
    return gaps
