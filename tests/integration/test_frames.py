"""Frame extraction at the end of a file whose audio outlasts its video. Found on a real YouTube short: the container ran
28 ms past the video stream, the audit asked for a frame 60 ms before the container end, ffmpeg decoded nothing, and the
whole audit crashed with a misleading encoder error. Real ffmpeg; no model calls."""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from directorloop.media.frames import extract_frame
from directorloop.media.probe import FFMPEG, inspect_media


@pytest.fixture(scope="module")
def short_video_long_audio(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("tail") / "tail.mp4"
    subprocess.run([FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=240x426:d=2:r=24000/1001",
                    "-f", "lavfi", "-i", "sine=frequency=330:duration=2.2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-color_range", "tv",
                    "-c:a", "aac", str(path)], check=True, timeout=60)
    return path


def gray(jpeg: bytes) -> np.ndarray:
    return np.asarray(Image.open(io.BytesIO(jpeg)).convert("L"), dtype=np.float32)


def test_a_time_after_the_last_video_frame_returns_the_last_frame(short_video_long_audio: Path) -> None:
    info = inspect_media(short_video_long_audio)
    assert info.duration_ms is not None and info.duration_ms >= 2150, "the container duration follows the longer audio"
    tail = extract_frame(short_video_long_audio, info.duration_ms - 60)
    last = extract_frame(short_video_long_audio, 1960)  # the final frame of a 2 s stream at 23.976 fps starts near 1.96 s
    earlier = extract_frame(short_video_long_audio, 1500)
    assert tail.timestamp_ms == info.duration_ms - 60 and tail.width == last.width
    assert float(np.abs(gray(tail.jpeg) - gray(last.jpeg)).mean()) < 1.0, "a player keeps showing the last frame, so that is the frame at this time"
    assert float(np.abs(gray(tail.jpeg) - gray(earlier.jpeg)).mean()) > 3.0, "testsrc2 frames change, so an earlier frame would be caught"
