"""Real ffmpeg round trip on tiny synthetic fixtures."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from directorloop.domain import (
    AssetKind,
    AssetManifest,
    AssetOrigin,
    AssetRecord,
    Caption,
    CreativeBrief,
    EditOpList,
    EditPlan,
    GoalProfile,
    NarrationTrack,
    OutputProfile,
    RepairAction,
    Segment,
    apply_ops,
    sha256_file,
)
from directorloop.media import (
    FFMPEG_PATH,
    RenderError,
    has_faststart,
    inspect_media,
    locate_motion_region,
    render_plan,
    sample_frames,
    validate_plan,
)


def _make_clip(path: Path, seconds: float, color: str, size: str = "320x568", moving_box: bool = False) -> None:
    if moving_box:
        # static colour background with a small box moving in the lower-right quadrant
        src = f"color=c={color}:s={size}:d={seconds}:r=30"
        vf = "drawbox=x='iw*0.7+10*sin(t*6)':y=ih*0.7:w=20:h=20:color=orange:t=fill"
        cmd = [FFMPEG_PATH, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", src, "-vf", vf,
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(path)]
    else:
        src = f"color=c={color}:s={size}:d={seconds}:r=30"
        cmd = [FFMPEG_PATH, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", src,
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(path)]
    subprocess.run(cmd, check=True, timeout=60)


def _make_tone(path: Path, seconds: float) -> None:
    cmd = [FFMPEG_PATH, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
           f"sine=frequency=440:duration={seconds}", "-ar", "44100", "-ac", "1", str(path)]
    subprocess.run(cmd, check=True, timeout=60)


@pytest.fixture
def fixture_pack(tmp_path: Path):
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    n = tmp_path / "n.wav"
    _make_clip(a, 3.0, "steelblue", moving_box=True)
    _make_clip(b, 2.0, "darkseagreen")
    _make_tone(n, 2.5)
    records = []
    for asset_id, p, kind in (("wide", a, AssetKind.VIDEO), ("result", b, AssetKind.VIDEO), ("nar", n, AssetKind.AUDIO)):
        info = inspect_media(p)
        records.append(
            AssetRecord(
                id=asset_id, project_id="p", kind=kind, origin=AssetOrigin.RENDERED_FIXTURE, content_hash=sha256_file(p),
                storage_key=p.name, label=asset_id, duration_ms=info.duration_ms, stream=info,
            )
        )
    manifest = AssetManifest(project_id="p", assets=records)
    paths = {"wide": a, "result": b, "nar": n}
    brief = CreativeBrief(
        id="b", project_id="p", profile=GoalProfile.PRODUCT_DEMO, objective="test", target_duration_ms_min=1000,
        target_duration_ms_max=8000, allowed_actions=[RepairAction.TRIM_OR_RETIME, RepairAction.CROP_EXISTING_SHOT,
        RepairAction.REVISE_CAPTIONS], review_status="approved",
    )
    plan = EditPlan(
        output=OutputProfile(width=360, height=640, fps_num=30),
        segments=[Segment(id="s1", asset_id="wide", source_in_ms=0, source_out_ms=2500),
                  Segment(id="s2", asset_id="result", source_in_ms=0, source_out_ms=1500)],
        captions=[Caption(id="c1", text="Hello there", start_ms=200, end_ms=1500)],
        narration=NarrationTrack(asset_id="nar"),
    )
    return manifest, paths, brief, plan, tmp_path


def test_validate_and_render_round_trip(fixture_pack):
    manifest, paths, brief, plan, tmp = fixture_pack
    vr = validate_plan(plan, manifest, brief)
    assert vr.ok, vr.errors
    res = render_plan(plan, manifest, paths, publish_dir=tmp / "renders")
    assert res.path.exists() and res.path.name == f"{res.artifact_hash}.mp4"
    assert abs(res.duration_ms - 4000) <= 150
    assert (res.width, res.height) == (360, 640)
    assert res.has_audio
    assert has_faststart(res.path)
    info = inspect_media(res.path)
    assert info.has_audio and info.video_codec == "h264"
    frames = sample_frames(res.path, res.duration_ms, count=4)
    assert len(frames) == 4 and all(f.jpeg[:2] == b"\xff\xd8" for f in frames)
    # rendering the identical plan again publishes the same hash (idempotent by content)
    res2 = render_plan(plan, manifest, paths, publish_dir=tmp / "renders")
    assert res2.artifact_hash == res.artifact_hash


def test_validation_rejects_bad_plans(fixture_pack):
    manifest, paths, brief, plan, tmp = fixture_pack
    bad = plan.model_copy(deep=True)
    bad.segments[0].source_out_ms = 99000
    assert not validate_plan(bad, manifest, brief).ok
    unauthorized = plan.model_copy(deep=True)
    unauthorized.segments[0].asset_id = "not_mine"
    vr = validate_plan(unauthorized, manifest, brief)
    assert any("unauthorized" in e for e in vr.errors)
    overflow = plan.model_copy(deep=True)
    overflow.captions[0].text = "x" * 190
    assert not validate_plan(overflow, manifest, brief).ok
    with pytest.raises(RenderError):
        render_plan(unauthorized, manifest, paths, publish_dir=tmp / "renders")


def test_ops_apply_then_render(fixture_pack):
    manifest, paths, brief, plan, tmp = fixture_pack
    ops = EditOpList.model_validate({"ops": [
        {"type": "trim_segment", "segment_id": "s1", "source_in_ms": 500, "source_out_ms": 2000},
        {"type": "set_caption", "caption_id": "c1", "text": "Retimed", "start_ms": 100, "end_ms": 900, "position": "top"},
    ]}).ops
    new_plan = apply_ops(plan, ops)
    assert validate_plan(new_plan, manifest, brief, baseline=plan).ok
    res = render_plan(new_plan, manifest, paths, publish_dir=tmp / "renders")
    assert abs(res.duration_ms - 3000) <= 150


def test_motion_locator_finds_moving_box(fixture_pack):
    manifest, paths, brief, plan, tmp = fixture_pack
    region = locate_motion_region(paths["wide"], 0, 2500, 320, 568, zoom=2.5)
    # the box moves in the lower-right quadrant
    cx = region.crop.x + region.crop.w / 2
    cy = region.crop.y + region.crop.h / 2
    assert cx > 160 and cy > 284, region
    assert region.energy_share > 0.5
