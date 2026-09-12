"""Frame sampling and a deterministic motion locator.

Frames are what frame-based probe providers see. The motion locator finds where
in the picture the action happens from pixel change alone; it is an observable
property, not a semantic judgment.
"""

from __future__ import annotations

import io
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from ..domain.edit_plan import CropRect
from .probe import FFMPEG, MediaError


@dataclass(frozen=True)
class SampledFrame:
    timestamp_ms: int
    jpeg: bytes
    width: int
    height: int


def sample_timestamps(duration_ms: int, count: int) -> list[int]:
    """Evenly spaced timestamps that avoid the very first and very last frame."""
    if duration_ms <= 0 or count <= 0:
        return []
    if count == 1:
        return [duration_ms // 2]
    step = duration_ms / (count + 1)
    return [int(round(step * (i + 1))) for i in range(count)]


def extract_frame(path: str | Path, timestamp_ms: int, max_width: int = 512, quality: int = 85) -> SampledFrame:
    cmd = [
        FFMPEG,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{timestamp_ms / 1000:.3f}",
        "-i",
        str(path),
        "-frames:v",
        "1",
        "-vf",
        f"scale='min({max_width},iw)':-2",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "-q:v",
        str(max(2, min(31, int(round((100 - quality) / 3))))),
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
    if proc.returncode != 0 or not proc.stdout:
        raise MediaError(f"frame extraction failed at {timestamp_ms} ms: {proc.stderr.decode(errors='ignore')[:200]}")
    with Image.open(io.BytesIO(proc.stdout)) as im:
        w, h = im.size
    return SampledFrame(timestamp_ms=timestamp_ms, jpeg=proc.stdout, width=w, height=h)


def sample_frames(path: str | Path, duration_ms: int, count: int = 8, max_width: int = 512) -> list[SampledFrame]:
    return [extract_frame(path, ts, max_width=max_width) for ts in sample_timestamps(duration_ms, count)]


def _decode_gray_frames(path: str | Path, start_ms: int, end_ms: int, fps: int, width: int) -> np.ndarray:
    """Decode a low-resolution grayscale frame stack (N, H, W) for an interval."""
    duration = max(0.05, (end_ms - start_ms) / 1000)
    cmd = [
        FFMPEG,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{start_ms / 1000:.3f}",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(path),
        "-vf",
        f"fps={fps},scale={width}:-2",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=120, check=False)
    if proc.returncode != 0:
        raise MediaError(f"decode failed: {proc.stderr.decode(errors='ignore')[:200]}")
    # Recover height from ffprobe-free arithmetic: total bytes / width must divide evenly.
    total = len(proc.stdout)
    if total == 0:
        raise MediaError("decode produced no frames")
    # Height is unknown until we know frame count; probe with candidate heights (even numbers).
    for h in range(2, 4096, 2):
        if total % (width * h) == 0:
            n = total // (width * h)
            arr = np.frombuffer(proc.stdout, dtype=np.uint8).reshape(n, h, width)
            # Heuristic: prefer the aspect ratio closest to portrait/landscape norms with >= 2 frames
            if n >= 2 and 0.3 < width / h < 3.5:
                return arr
    raise MediaError("could not infer frame geometry")


@dataclass(frozen=True)
class MotionRegion:
    """Where pixel change concentrates within an interval, in source-pixel coordinates."""

    crop: CropRect
    energy_share: float  # fraction of total motion energy inside the crop
    grid_cell: tuple[int, int]
    analysed_frames: int
    source_width: int
    source_height: int


def locate_motion_region(
    path: str | Path,
    start_ms: int,
    end_ms: int,
    source_width: int,
    source_height: int,
    output_aspect: tuple[int, int] = (9, 16),
    zoom: float = 3.0,
    analysis_width: int = 180,
    fps: int = 10,
) -> MotionRegion:
    """Find the cell with the most temporal change and return a crop around it.

    The crop keeps the output aspect ratio and is `zoom` times smaller than the source
    on each axis, clamped to the frame. Deterministic; no model involved.
    """
    stack = _decode_gray_frames(path, start_ms, end_ms, fps=fps, width=analysis_width)
    diffs = np.abs(np.diff(stack.astype(np.int16), axis=0)).sum(axis=0).astype(np.float64)
    h, w = diffs.shape
    cells_x, cells_y = 6, 10
    cell_w, cell_h = w / cells_x, h / cells_y
    energy = np.zeros((cells_y, cells_x))
    for cy in range(cells_y):
        for cx in range(cells_x):
            y0, y1 = int(cy * cell_h), int((cy + 1) * cell_h)
            x0, x1 = int(cx * cell_w), int((cx + 1) * cell_w)
            energy[cy, cx] = diffs[y0:y1, x0:x1].sum()
    cy, cx = np.unravel_index(int(np.argmax(energy)), energy.shape)
    center_x = (cx + 0.5) * cell_w / w
    center_y = (cy + 0.5) * cell_h / h
    aw, ah = output_aspect
    crop_w = int(source_width / zoom)
    crop_h = int(crop_w * ah / aw)
    if crop_h > source_height:
        crop_h = source_height
        crop_w = int(crop_h * aw / ah)
    x = int(center_x * source_width - crop_w / 2)
    y = int(center_y * source_height - crop_h / 2)
    x = max(0, min(source_width - crop_w, x))
    y = max(0, min(source_height - crop_h, y))
    x -= x % 2
    y -= y % 2
    crop_w -= crop_w % 2
    crop_h -= crop_h % 2
    # energy share inside the crop, measured on the analysis grid
    sx, sy = w / source_width, h / source_height
    ax0, ay0 = int(x * sx), int(y * sy)
    ax1, ay1 = int((x + crop_w) * sx), int((y + crop_h) * sy)
    share = float(diffs[ay0:ay1, ax0:ax1].sum() / (diffs.sum() or 1.0))
    return MotionRegion(
        crop=CropRect(x=x, y=y, w=crop_w, h=crop_h),
        energy_share=share,
        grid_cell=(int(cx), int(cy)),
        analysed_frames=int(stack.shape[0]),
        source_width=source_width,
        source_height=source_height,
    )
