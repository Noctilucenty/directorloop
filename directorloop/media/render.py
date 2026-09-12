"""Render a validated EditPlan to an MP4 with FFmpeg.

Argument arrays only; no shell. Output is written to a temporary path, verified with
ffprobe, then atomically published under its content hash.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..domain.assets import AssetManifest
from ..domain.edit_plan import EditPlan, Segment
from ..domain.ids import sha256_file
from .captions import CaptionLayout, render_caption_png
from .probe import FFMPEG, MediaError, has_faststart, inspect_media


@dataclass
class RenderResult:
    path: Path
    artifact_hash: str
    duration_ms: int
    width: int
    height: int
    has_audio: bool
    render_ms: int
    verify_ms: int
    encoder: str
    caption_layouts: list[CaptionLayout] = field(default_factory=list)
    ffmpeg_argv: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class RenderError(RuntimeError):
    pass


def _segment_filter(seg: Segment, plan: EditPlan, idx: int) -> str:
    out = plan.output
    parts = ["setpts=PTS-STARTPTS"]
    if seg.crop is not None:
        c = seg.crop
        parts.append(f"crop={c.w}:{c.h}:{c.x}:{c.y}")
    if seg.fit == "cover":
        parts.append(f"scale={out.width}:{out.height}:force_original_aspect_ratio=increase")
        parts.append(f"crop={out.width}:{out.height}")
    else:
        parts.append(f"scale={out.width}:{out.height}:force_original_aspect_ratio=decrease")
        parts.append(f"pad={out.width}:{out.height}:(ow-iw)/2:(oh-ih)/2:color=black")
    parts.append(f"fps={out.fps_num}/{out.fps_den}")
    parts.append("format=yuv420p")
    parts.append("setsar=1")
    return f"[{idx}:v]" + ",".join(parts) + f"[v{idx}]"


def build_ffmpeg_argv(
    plan: EditPlan,
    manifest: AssetManifest,
    asset_paths: dict[str, Path],
    caption_pngs: list[tuple[CaptionLayout, int, int]],
    out_path: Path,
    encoder: str = "libx264",
    preset: str = "veryfast",
    crf: int = 22,
) -> list[str]:
    argv: list[str] = [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    filters: list[str] = []
    n_inputs = 0
    # Video segment inputs (fast input-side seek, frame-accurate on re-encode).
    for seg in plan.segments:
        path = asset_paths[seg.asset_id]
        argv += ["-ss", f"{seg.source_in_ms / 1000:.3f}", "-t", f"{(seg.source_out_ms - seg.source_in_ms) / 1000:.3f}", "-i", str(path)]
        n_inputs += 1
    seg_indices = list(range(len(plan.segments)))
    for i, seg in zip(seg_indices, plan.segments, strict=True):
        filters.append(_segment_filter(seg, plan, i))
        rec = manifest.get(seg.asset_id)
        keep_audio = seg.audio_policy == "keep" and bool(rec.stream and rec.stream.has_audio)
        dur = (seg.source_out_ms - seg.source_in_ms) / 1000
        if keep_audio:
            filters.append(f"[{i}:a]asetpts=PTS-STARTPTS,aformat=sample_rates=48000:channel_layouts=stereo,atrim=0:{dur:.3f},apad=whole_dur={dur:.3f}[a{i}]")
        else:
            filters.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{dur:.3f}[a{i}]")
    concat_in = "".join(f"[v{i}][a{i}]" for i in seg_indices)
    filters.append(f"{concat_in}concat=n={len(seg_indices)}:v=1:a=1[vcat][acat]")
    # Caption overlays
    vlabel = "vcat"
    for j, (layout, start_ms, end_ms) in enumerate(caption_pngs):
        argv += ["-loop", "1", "-framerate", f"{plan.output.fps_num}/{plan.output.fps_den}", "-i", str(layout.png_path)]
        cin = n_inputs
        n_inputs += 1
        nxt = f"vcap{j}"
        filters.append(
            f"[{vlabel}][{cin}:v]overlay=0:0:enable='between(t,{start_ms / 1000:.3f},{end_ms / 1000:.3f})':eof_action=pass[{nxt}]"
        )
        vlabel = nxt
    # Narration and music
    total_ms = plan.timeline_duration_ms()
    total_s = total_ms / 1000
    mix_inputs = ["[acat]"]
    if plan.narration is not None:
        argv += ["-i", str(asset_paths[plan.narration.asset_id])]
        nin = n_inputs
        n_inputs += 1
        delay = max(0, plan.narration.offset_ms)
        filters.append(
            f"[{nin}:a]aformat=sample_rates=48000:channel_layouts=stereo,adelay={delay}|{delay},"
            f"volume={plan.narration.gain_db}dB,apad=whole_dur={total_s:.3f},atrim=0:{total_s:.3f}[anar]"
        )
        mix_inputs.append("[anar]")
    if plan.music is not None:
        argv += ["-stream_loop", "-1", "-i", str(asset_paths[plan.music.asset_id])]
        min_ = n_inputs
        n_inputs += 1
        filters.append(
            f"[{min_}:a]aformat=sample_rates=48000:channel_layouts=stereo,volume={plan.music.gain_db}dB,"
            f"atrim=0:{total_s:.3f},apad=whole_dur={total_s:.3f}[amus]"
        )
        mix_inputs.append("[amus]")
    if len(mix_inputs) == 1:
        filters.append("[acat]anull[aout]")
    else:
        filters.append(
            "".join(mix_inputs) + f"amix=inputs={len(mix_inputs)}:duration=first:dropout_transition=0:normalize=0[aout]"
        )
    argv += ["-filter_complex", ";".join(filters), "-map", f"[{vlabel}]", "-map", "[aout]"]
    if encoder == "h264_videotoolbox":
        argv += ["-c:v", "h264_videotoolbox", "-b:v", "4M", "-allow_sw", "1"]
    else:
        argv += ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-profile:v", "high", "-level", "4.1"]
    argv += ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
    argv += ["-t", f"{total_s:.3f}", "-movflags", "+faststart", "-f", "mp4", str(out_path)]
    return argv


def render_plan(
    plan: EditPlan,
    manifest: AssetManifest,
    asset_paths: dict[str, Path],
    publish_dir: Path,
    work_dir: Path | None = None,
    encoder: str = "libx264",
    timeout_s: int = 180,
) -> RenderResult:
    """Render, verify, publish by content hash. Raises RenderError on any failure."""
    for asset_id in plan.asset_ids():
        if asset_id not in asset_paths:
            raise RenderError(f"no authorized path for asset {asset_id}")
        if not Path(asset_paths[asset_id]).exists():
            raise RenderError(f"asset file missing for {asset_id}")
    publish_dir.mkdir(parents=True, exist_ok=True)
    work_dir = work_dir or publish_dir / "_work"
    work_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix="dl_render_", dir=work_dir))
    try:
        layouts: list[tuple[CaptionLayout, int, int]] = []
        for cap in plan.captions:
            layout = render_caption_png(cap, plan.output, work_dir / "captions")
            layouts.append((layout, cap.start_ms, cap.end_ms))
        out_tmp = tmp_dir / "candidate.mp4"
        argv = build_ffmpeg_argv(plan, manifest, asset_paths, layouts, out_tmp, encoder=encoder)
        t0 = time.monotonic()
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s, check=False)
        except subprocess.TimeoutExpired as exc:
            raise RenderError(f"ffmpeg timed out after {timeout_s}s") from exc
        render_ms = int((time.monotonic() - t0) * 1000)
        if proc.returncode != 0 or not out_tmp.exists() or out_tmp.stat().st_size == 0:
            raise RenderError(f"ffmpeg failed: {proc.stderr.strip()[-600:]}")
        t1 = time.monotonic()
        try:
            info = inspect_media(out_tmp)
        except MediaError as exc:
            raise RenderError(f"rendered file does not decode: {exc}") from exc
        warnings: list[str] = []
        expected = plan.timeline_duration_ms()
        if info.duration_ms is None or abs(info.duration_ms - expected) > 150:
            raise RenderError(f"duration mismatch: expected {expected} ms, got {info.duration_ms}")
        if info.width != plan.output.width or info.height != plan.output.height:
            raise RenderError(f"geometry mismatch: {info.width}x{info.height}")
        if not info.has_audio:
            raise RenderError("rendered file has no audio stream")
        if not has_faststart(out_tmp):
            warnings.append("moov atom not at start; progressive playback may stall")
        digest = sha256_file(out_tmp)
        final = publish_dir / f"{digest}.mp4"
        if not final.exists():
            os.replace(out_tmp, final)
        verify_ms = int((time.monotonic() - t1) * 1000)
        return RenderResult(
            path=final,
            artifact_hash=digest,
            duration_ms=int(info.duration_ms),
            width=int(info.width or 0),
            height=int(info.height or 0),
            has_audio=info.has_audio,
            render_ms=render_ms,
            verify_ms=verify_ms,
            encoder=encoder,
            caption_layouts=[layout for layout, _, _ in layouts],
            ffmpeg_argv=argv,
            warnings=warnings,
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
