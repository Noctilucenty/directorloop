"""ffprobe inspection of media files."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from ..domain.assets import StreamInfo

FFPROBE = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"


class MediaError(RuntimeError):
    pass


def _parse_rate(value: str | None) -> float | None:
    if not value or value in ("0/0", "N/A"):
        return None
    if "/" in value:
        num, den = value.split("/", 1)
        try:
            return float(num) / float(den) if float(den) else None
        except ValueError:
            return None
    try:
        return float(value)
    except ValueError:
        return None


def ffprobe_json(path: str | Path, timeout: int = 30, *, untrusted: bool = False) -> dict:
    path = Path(path)
    if not path.exists():
        raise MediaError(f"missing file: {path}")
    cmd = [
        FFPROBE,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
    ]
    if untrusted:
        # Accept self-contained upload containers, never playlists or network demuxers.
        cmd.extend(["-protocol_whitelist", "file,pipe", "-format_whitelist", "mov,matroska,webm"])
    cmd.append(str(path.resolve()))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"ffprobe timed out on {path.name}") from exc
    if proc.returncode != 0:
        raise MediaError(f"ffprobe failed on {path.name}: {proc.stderr.strip()[:300]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise MediaError(f"ffprobe returned invalid JSON for {path.name}") from exc


def inspect_media(path: str | Path, *, untrusted: bool = False) -> StreamInfo:
    data = ffprobe_json(path, untrusted=untrusted)
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration_s = None
    for source in (fmt.get("duration"), (video or {}).get("duration"), (audio or {}).get("duration")):
        try:
            if source is not None:
                duration_s = float(source)
                break
        except (TypeError, ValueError):
            continue
    rotation = 0
    if video:
        for sd in video.get("side_data_list", []) or []:
            if "rotation" in sd:
                try:
                    rotation = int(float(sd["rotation"]))
                except (TypeError, ValueError):
                    pass
        tags = video.get("tags") or {}
        if "rotate" in tags:
            try:
                rotation = int(float(tags["rotate"]))
            except (TypeError, ValueError):
                pass
    nb_frames = None
    if video and video.get("nb_frames") not in (None, "N/A"):
        try:
            nb_frames = int(video["nb_frames"])
        except ValueError:
            nb_frames = None
    return StreamInfo(
        width=int(video["width"]) if video and video.get("width") else None,
        height=int(video["height"]) if video and video.get("height") else None,
        fps=_parse_rate((video or {}).get("avg_frame_rate")) or _parse_rate((video or {}).get("r_frame_rate")),
        duration_ms=int(round(duration_s * 1000)) if duration_s is not None else None,
        video_codec=(video or {}).get("codec_name"),
        has_audio=audio is not None,
        audio_codec=(audio or {}).get("codec_name"),
        audio_sample_rate=int(audio["sample_rate"]) if audio and audio.get("sample_rate") else None,
        audio_channels=int(audio["channels"]) if audio and audio.get("channels") else None,
        rotation=rotation,
        nb_frames=nb_frames,
        container=fmt.get("format_name"),
        size_bytes=int(fmt["size"]) if fmt.get("size") else os.path.getsize(path),
    )


def ffmpeg_version() -> str:
    try:
        out = subprocess.run([FFMPEG, "-version"], capture_output=True, text=True, timeout=10, check=False).stdout
        return out.splitlines()[0].split(" ")[2] if out else "unknown"
    except (OSError, IndexError, subprocess.TimeoutExpired):
        return "unknown"


def ffmpeg_capabilities() -> dict[str, bool]:
    """Which encoders and filters this ffmpeg build actually exposes."""
    caps: dict[str, bool] = {}
    try:
        enc = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=10, check=False).stdout
        filt = subprocess.run([FFMPEG, "-hide_banner", "-filters"], capture_output=True, text=True, timeout=10, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False}
    for name in ("libx264", "h264_videotoolbox", "h264_nvenc", "aac"):
        caps[f"encoder:{name}"] = f" {name} " in enc
    for name in ("drawtext", "overlay", "concat", "amix", "adelay", "apad", "subtitles"):
        caps[f"filter:{name}"] = f" {name} " in filt
    caps["available"] = True
    return caps


def has_faststart(path: str | Path) -> bool:
    """True when the moov atom precedes mdat (progressive playback in browsers)."""
    with open(path, "rb") as fh:
        head = fh.read(1 << 20)
    moov = head.find(b"moov")
    mdat = head.find(b"mdat")
    if moov == -1:
        return False
    return mdat == -1 or moov < mdat
