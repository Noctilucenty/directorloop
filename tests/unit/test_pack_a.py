"""Pack A generator: reduced-resolution end-to-end run into a temp dir."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from make_pack_a import SHOT_SECONDS, generate, render_frame  # noqa: E402

from directorloop.domain import GoalProfile, load_pack  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("say") is None, reason="ffmpeg and macOS say required"
)


@pytest.fixture(scope="module")
def small_pack(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("packa")
    return generate(out / "pack", width=180, height=320, fps=10, speech_pref="say", quiet=True)


def test_pack_loads_with_checksums(small_pack: Path) -> None:
    pack = load_pack(small_pack, verify_checksums=True)
    assert pack.brief.profile == GoalProfile.PRODUCT_DEMO
    assert pack.truth.is_fictional is True
    assert len(pack.suite.questions) == 6
    assert {a.id for a in pack.manifest.assets} == {
        "asset_wide", "asset_closeup", "asset_result", "asset_narration", "asset_music"
    }
    for asset in pack.manifest.assets:
        assert asset.original_filename is None  # names never leak into the manifest
        assert pack.asset_path(asset).exists()


def test_shot_durations_and_geometry(small_pack: Path) -> None:
    pack = load_pack(small_pack, verify_checksums=False)
    for shot, seconds in SHOT_SECONDS.items():
        rec = pack.manifest.get(f"asset_{shot}")
        assert rec.stream is not None
        assert abs((rec.duration_ms or 0) - seconds * 1000) <= 150, (shot, rec.duration_ms)
        assert (rec.stream.width, rec.stream.height) == (180, 320)
        assert rec.stream.has_audio is False
    narr = pack.manifest.get("asset_narration")
    assert narr.stream is not None and narr.stream.has_audio and (narr.duration_ms or 0) > 3000
    music = pack.manifest.get("asset_music")
    assert abs((music.duration_ms or 0) - 12000) <= 50


def test_baseline_plan_is_ordinary_first_cut(small_pack: Path) -> None:
    pack = load_pack(small_pack, verify_checksums=False)
    plan = pack.baseline_plan
    assert [s.asset_id for s in plan.segments] == ["asset_wide", "asset_result"]
    assert plan.narration is not None and plan.narration.asset_id == "asset_narration"
    assert plan.music is not None and plan.music.asset_id == "asset_music"
    assert plan.timeline_duration_ms() >= pack.manifest.get("asset_narration").duration_ms
    assert plan.timeline_duration_ms() <= pack.brief.max_duration_ms()
    assert len(plan.captions) == 3


def test_suite_questions_are_answer_free_in_viewer_views(small_pack: Path) -> None:
    pack = load_pack(small_pack, verify_checksums=False)
    views = pack.suite.viewer_views(seed=1)
    dumped = json.dumps([v.model_dump() for v in views])
    assert "correct_option_id" not in dumped
    assert "claim_ids" not in dumped
    for v in views:
        assert v.options[-1].id == "not_shown"
    guards = [q.id for q in pack.suite.questions if q.regression_guard]
    assert set(guards) == {"q_material", "q_object", "q_result_stable"}


def test_rendering_is_deterministic() -> None:
    a = render_frame("closeup", 1.2, 90, 160).tobytes()
    b = render_frame("closeup", 1.2, 90, 160).tobytes()
    assert a == b


def _count_colors(img, colors, tol: int = 40) -> int:
    import numpy as np

    a = np.array(img.convert("RGB")).astype(int)
    mask = np.zeros(a.shape[:2], dtype=bool)
    for c in colors:
        mask |= np.abs(a - np.array(c)).sum(axis=2) < tol
    return int(mask.sum())


def test_wide_and_result_shots_never_show_mechanism_and_closeup_does(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fixture's defining property (v3): no frame of the wide or result shot shows the tab, its guide
    loops or the slot; the close-up shows all three. The mechanism is repainted in sentinel colours that
    appear nowhere else in the scene, so the check measures visibility rather than colour coincidence
    (pencil tips and phone edges share dark browns with the slot)."""
    import make_pack_a as m

    sentinels = {"slot": (255, 0, 255), "tab": (0, 255, 255), "loops": (255, 255, 0)}
    for name in ("SLOT", "SLOT_INNER"):
        monkeypatch.setattr(m, name, sentinels["slot"])
    for name in ("TAB", "TAB_SIDE", "TAB_TOP"):
        monkeypatch.setattr(m, name, sentinels["tab"])
    for name in ("LOOP", "LOOP_SHADE"):
        monkeypatch.setattr(m, name, sentinels["loops"])

    def visible(shot: str, t: float) -> dict[str, int]:
        frame = m.render_frame(shot, t, 540, 960)
        return {k: _count_colors(frame, [c], tol=90) for k, c in sentinels.items()}

    for t in (0.2, 1.0, 1.8, 2.6, 3.2, 3.9, 4.6, 5.2, 5.9):
        assert visible("wide", t) == {"slot": 0, "tab": 0, "loops": 0}, f"mechanism visible in wide shot at {t}s"
    for t in (0.2, 1.6, 1.8, 2.3, 3.0, 3.9):
        assert visible("result", t) == {"slot": 0, "tab": 0, "loops": 0}, f"mechanism visible in result shot at {t}s"
    for t in (1.0, 2.0):
        seen = visible("closeup", t)
        assert seen["tab"] > 2000 and seen["slot"] > 200 and seen["loops"] > 2000, f"close-up hides the mechanism at {t}s: {seen}"
    monkeypatch.undo()
    from make_pack_a import TAB, TAB_SIDE, TAB_TOP

    orange = [TAB, TAB_SIDE, TAB_TOP]
    # seated end state: tab body mostly gone, only a band above the slot remains
    up = _count_colors(render_frame("closeup", 0.2, 540, 960), orange)
    seated = _count_colors(render_frame("closeup", 2.8, 540, 960), orange)
    assert 0 < seated < up * 0.35
