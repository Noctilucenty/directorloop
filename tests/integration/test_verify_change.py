"""Render verification against real ffmpeg output, with positive and negative controls: a verification that cannot fail
proves nothing."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from directorloop.audit.models import AuditFinding
from directorloop.audit.repairs import build_candidates
from directorloop.audit.review import Word
from directorloop.audit.revise import audio_fidelity, planned_words, render_candidate, verify_change
from directorloop.creative.mutate import source_manifest
from directorloop.domain.creative import BeatRole
from directorloop.domain.edit_plan import MoveSegment, RemoveSegment, apply_ops
from directorloop.domain.ids import sha256_file
from directorloop.media.probe import FFMPEG
from tests.unit.test_creative_mutations import make_genome

ROLES = [BeatRole.HOOK, BeatRole.CONTEXT, BeatRole.PROOF, BeatRole.PAYOFF]


@pytest.fixture(scope="module")
def clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("verify") / "src.mp4"
    # loudness steps every 250 ms in a pattern that differs between the four 2 s beats
    envelope = "volume='0.15+0.85*mod(floor(t*4)*7,5)/4':eval=frame"
    subprocess.run([FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=540x960:d=8:r=30",
                    "-f", "lavfi", "-i", "sine=frequency=330:duration=8", "-af", envelope, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(path)], check=True, timeout=90)
    return path


def test_punch_in_is_verified_and_an_unedited_file_is_not(clip: Path, tmp_path: Path) -> None:
    g = make_genome(sha256_file(clip), ROLES)
    f = AuditFinding(id="f", version_id="v0", start_ms=2900, end_ms=3100, weakness="the small action is hard to see", repair_kind="reframe_or_zoom")
    cands, base, _, _ = build_candidates(f, g, clip, "p")
    zoom = next(c for c in cands if c.key.startswith("zoom_center"))
    assert zoom.plan.timeline_duration_ms() == base.timeline_duration_ms(), "a punch-in never drops footage or sound"
    rendered = render_candidate(zoom.plan, source_manifest("p", clip, g.artifact_hash), clip, tmp_path)
    ok = verify_change("PUNCH_IN", zoom.description, clip, Path(rendered["path"]), base, zoom.plan, None, (f.start_ms, f.end_ms))
    assert ok.verified, ok.checks
    unedited = tmp_path / "copy.mp4"
    shutil.copy(clip, unedited)
    bad = verify_change("PUNCH_IN", zoom.description, clip, unedited, base, zoom.plan, None, (f.start_ms, f.end_ms))
    assert not bad.verified and any("NOT THE PLANNED REFRAME" in c for c in bad.checks), bad.checks


def test_audio_fidelity_detects_the_wrong_edit(clip: Path, tmp_path: Path) -> None:
    g = make_genome(sha256_file(clip), ROLES)
    manifest = source_manifest("p", clip, g.artifact_hash)
    from directorloop.creative.mutate import identity_plan

    base = identity_plan(g)
    drop1 = apply_ops(base, [RemoveSegment(segment_id="beat_01")])
    drop2 = apply_ops(base, [RemoveSegment(segment_id="beat_02")])
    r1 = render_candidate(drop1, manifest, clip, tmp_path)
    assert (audio_fidelity(clip, Path(r1["path"]), drop1) or 0) >= 0.8
    assert (audio_fidelity(clip, Path(r1["path"]), drop2) or 1) < 0.8, "same duration, different audio: the check must notice"
    swapped = apply_ops(base, [MoveSegment(segment_id="beat_01", after_segment_id="beat_02")])
    assert (audio_fidelity(clip, Path(r1["path"]), swapped) or 1) < 0.8


def test_planned_words_follow_the_plan() -> None:
    g = make_genome("h" * 64, ROLES)
    from directorloop.creative.mutate import identity_plan

    base = identity_plan(g)
    words = [Word("one", 500, 900), Word("two", 2500, 2900), Word("three", 4500, 4900), Word("four", 6500, 6900)]
    moved = apply_ops(base, [MoveSegment(segment_id="beat_01", after_segment_id="beat_02")])
    assert [(w.text, w.start_ms) for w in planned_words(moved, words)] == [("one", 500), ("three", 2500), ("two", 4500), ("four", 6500)]
    removed = apply_ops(base, [RemoveSegment(segment_id="beat_02")])
    assert [w.text for w in planned_words(removed, words)] == ["one", "two", "four"]
    assert [w.text for w in planned_words(moved, words, 2000, 4000)] == ["two"]


class ScriptedJudge:
    """Answers pairwise questions with no preference and the protected check with a fixed verdict per item."""

    def __init__(self, statuses: dict[str, tuple[str, str]]) -> None:
        from directorloop.providers.base import ProviderCapability

        self.capability = ProviderCapability(name="fake", role="probe", model="scripted", modalities={"text", "image"}, structured_output=True, docs_ref="", present=True)
        self.statuses = statuses
        self.instructions: list[str] = []

    def judge_json(self, media, instruction, schema):  # noqa: ANN001, ANN201
        from directorloop.providers.base import CompletionResult

        self.instructions.append(instruction)
        if "first decide its kind" in instruction:
            assert any(f.timestamp_ms >= 100000 for f in media.frames) and any(f.timestamp_ms < 100000 for f in media.frames), "both versions are shown"
            items = [{"item": k, "kind": v[0], "status": v[1], "evidence": "scripted"} for k, v in self.statuses.items()]
            return CompletionResult(data={"items": items}, latency_ms=1, model="scripted")
        return CompletionResult(data={"choice": "no_preference", "reason": "same"}, latency_ms=1, model="scripted")


def test_compare_treats_rules_and_content_differently(clip: Path, tmp_path: Path) -> None:
    from directorloop.audit.models import AuditReport, CoverageRecord
    from directorloop.audit.revise import compare_versions
    from directorloop.creative.mutate import identity_plan

    g = make_genome(sha256_file(clip), ROLES)
    base = identity_plan(g)
    plan = apply_ops(base, [RemoveSegment(segment_id="beat_03")])
    r = render_candidate(plan, source_manifest("p", clip, g.artifact_hash), clip, tmp_path)
    finding = AuditFinding(id="f", version_id="v0", start_ms=6000, end_ms=8000, weakness="w", issue_type="weak_or_missing_payoff")
    orig = AuditReport(id="audit_1_1", video_id="t1", version_id="v0", artifact_hash=g.artifact_hash, artifact_path=str(clip), duration_ms=8000, created_at="t",
                       coverage=CoverageRecord(mode="sampled_frames_plus_asr", duration_ms=8000), findings=[finding])
    cand = AuditReport(id="audit_2_2", video_id="t1", version_id="v1", artifact_hash=r["artifact_hash"], artifact_path=r["path"], duration_ms=6000, created_at="t",
                       coverage=CoverageRecord(mode="sampled_frames_plus_asr", duration_ms=6000))
    judge = ScriptedJudge({"Do not add new claims or on-screen text": ("rule", "respected"), "Keep the ending": ("content", "lost")})
    comp = compare_versions(provider=judge, original=orig, candidate=cand, finding=finding, candidate_plan=plan,  # type: ignore[arg-type]
                            protected_items=["Do not add new claims or on-screen text", "Keep the ending"])
    assert any(x.startswith("protected content lost: Keep the ending") for x in comp.regressed)
    assert not any("Do not add new claims" in x for x in comp.regressed) and any("rule respected" in x for x in comp.unchanged)
    assert comp.outcome in ("mixed", "regression"), comp.outcome

    judge2 = ScriptedJudge({"Do not add new claims or on-screen text": ("rule", "violated")})
    comp2 = compare_versions(provider=judge2, original=orig, candidate=cand, finding=finding, candidate_plan=plan, protected_items=["Do not add new claims or on-screen text"])  # type: ignore[arg-type]
    assert any(x.startswith("constraint violated") for x in comp2.regressed)
