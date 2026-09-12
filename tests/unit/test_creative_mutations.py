"""Controlled mutations: each operator changes exactly one variable and renders to a valid file."""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

from directorloop.creative.mutate import (
    VIDEO_ASSET_ID,
    build_mutation,
    experiment_brief,
    identity_plan,
    source_manifest,
)
from directorloop.domain import apply_ops
from directorloop.domain.creative import (
    BeatRole,
    CreativeGenome,
    CreativeHypothesis,
    Feature,
    FeatureSource,
    HookProfile,
    HypothesisFamily,
    MutationType,
    TimelineBeat,
)
from directorloop.domain.ids import sha256_file
from directorloop.media import inspect_media, render_plan
from directorloop.media.probe import FFMPEG


def _f(v: object, src: FeatureSource = FeatureSource.MECHANICAL) -> Feature:
    return Feature(value=v, source=src, confidence=0.9)  # type: ignore[arg-type]


def make_genome(artifact_hash: str, roles: list[BeatRole], beat_ms: int = 2000, static: tuple[int, int] | None = (4500, 3000)) -> CreativeGenome:
    beats = [
        TimelineBeat(id=f"beat_{i:02d}", index=i, start_ms=i * beat_ms, end_ms=(i + 1) * beat_ms, text=f"Sentence {i} about {r.value}.", role=r, role_confidence=0.9)
        for i, r in enumerate(roles)
    ]
    duration = beat_ms * len(roles)
    hook = HookProfile(
        hook_type=_f("surprising_claim", FeatureSource.LANGUAGE_MODEL), first_spoken_words=_f("Sentence 0", FeatureSource.ASR),
        first_visual_description=_f("a thing", FeatureSource.VISION_MODEL), face_visible_at_start=_f(False, FeatureSource.VISION_MODEL),
        product_or_subject_visible_at_start=_f(True, FeatureSource.VISION_MODEL), first_visual_change_ms=_f(900), first_meaningful_motion_ms=_f(500),
        first_claim_ms=_f(0, FeatureSource.LANGUAGE_MODEL), first_question_ms=_f(None, FeatureSource.LANGUAGE_MODEL),
    )
    first = {r: next((b.start_ms for b in beats if b.role == r), None) for r in (BeatRole.PROOF, BeatRole.PAYOFF, BeatRole.CTA)}
    return CreativeGenome(
        id="g", artifact_hash=artifact_hash, duration_ms=duration, width=180, height=320, aspect_ratio="9:16", hook=hook, beats=beats, shots=[],
        speech_rate_wps=_f(2.5, FeatureSource.ASR), first_proof_ms=_f(first[BeatRole.PROOF], FeatureSource.LANGUAGE_MODEL),
        first_payoff_ms=_f(first[BeatRole.PAYOFF], FeatureSource.LANGUAGE_MODEL), context_before_proof_ms=_f(2000, FeatureSource.LANGUAGE_MODEL),
        longest_static_span_ms=_f(static[1] if static else 0), longest_static_span_start_ms=_f(static[0] if static else None),
        shots_per_10s=_f(1.0), cta_ms=_f(first[BeatRole.CTA], FeatureSource.LANGUAGE_MODEL),
    )


@pytest.fixture(scope="module")
def clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("mut") / "src.mp4"
    cmd = [
        FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=s=540x960:d=8:r=30", "-f", "lavfi", "-i", "sine=frequency=220:duration=8",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
    ]
    subprocess.run(cmd, check=True, timeout=60)
    return path


def _hyp() -> CreativeHypothesis:
    return CreativeHypothesis(
        id="h1", family=HypothesisFamily.PROOF_LATENCY, statement="proof arrives late", region_start_ms=2000, region_end_ms=4000,
        region_basis="creative structure", scope="test", detection_confidence=0.6, candidate_mutations=[MutationType.PROOF_EARLIER], changed_variable="x",
    )


def _setup(clip: Path, roles: list[BeatRole], **kw: object):
    genome = make_genome(sha256_file(clip), roles, **kw)  # type: ignore[arg-type]
    manifest = source_manifest("p", clip, genome.artifact_hash)
    brief = experiment_brief("p", genome, "")
    return genome, manifest, brief, identity_plan(genome)


def _order(plan) -> list[str]:  # noqa: ANN001
    return [s.id for s in plan.segments]


ROLES = [BeatRole.HOOK, BeatRole.CONTEXT, BeatRole.PROOF, BeatRole.PAYOFF]


def test_identity_plan_tiles_the_genome(clip: Path) -> None:
    genome, _, _, plan = _setup(clip, ROLES)
    assert plan.timeline_duration_ms() == genome.duration_ms
    assert all(s.audio_policy == "keep" for s in plan.segments)
    assert plan.audio_join_fade_ms > 0


@pytest.mark.parametrize(
    ("mtype", "expected_order"),
    [
        (MutationType.PROOF_EARLIER, ["beat_00", "beat_02", "beat_01", "beat_03"]),
        (MutationType.PAYOFF_EARLIER, ["beat_00", "beat_03", "beat_01", "beat_02"]),
        (MutationType.RESULT_FIRST, ["beat_03", "beat_00", "beat_01", "beat_02"]),
        (MutationType.CONTEXT_COMPRESSION, ["beat_00", "beat_02", "beat_03"]),
    ],
)
def test_structural_mutations_change_one_variable(clip: Path, mtype: MutationType, expected_order: list[str]) -> None:
    genome, manifest, brief, plan = _setup(clip, ROLES)
    m = build_mutation(mtype, _hyp(), genome, plan, manifest, brief, "v0", clip)
    assert m is not None
    new = apply_ops(plan, m.ops)
    assert _order(new) == expected_order
    before = {s.id: (s.source_in_ms, s.source_out_ms, s.crop) for s in plan.segments}
    for s in new.segments:  # every surviving beat keeps its exact source interval and framing
        assert before[s.id] == (s.source_in_ms, s.source_out_ms, s.crop)
    assert m.changed_variable and m.protected_variables and m.hypothesis_id == "h1"


def test_pattern_interrupt_splits_one_beat_and_only_reframes(clip: Path) -> None:
    genome, manifest, brief, plan = _setup(clip, ROLES)
    m = build_mutation(MutationType.PATTERN_INTERRUPT, _hyp(), genome, plan, manifest, brief, "v0", clip)
    assert m is not None
    new = apply_ops(plan, m.ops)
    assert new.timeline_duration_ms() == plan.timeline_duration_ms()
    punch = [s for s in new.segments if s.id.endswith("_punch")]
    assert len(punch) == 1 and punch[0].crop is not None
    head = new.segments[new.segment_index(punch[0].id) - 1]
    assert head.id == punch[0].id.removesuffix("_punch") and head.crop is None
    assert head.source_out_ms == punch[0].source_in_ms, "audio stays contiguous across the punch-in"
    assert 4500 <= punch[0].source_in_ms <= 7500, "the cut sits inside the static span"
    assert len(new.segments) == len(plan.segments) + 1


def test_operators_refuse_when_precondition_fails(clip: Path) -> None:
    roles = [BeatRole.HOOK, BeatRole.PROOF, BeatRole.MECHANISM, BeatRole.CTA]  # proof already second; no payoff; no context
    genome, manifest, brief, plan = _setup(clip, roles, static=None)
    for mtype in (MutationType.PROOF_EARLIER, MutationType.PAYOFF_EARLIER, MutationType.CONTEXT_COMPRESSION, MutationType.PATTERN_INTERRUPT, MutationType.REMOVE_REDUNDANT_BEAT):
        assert build_mutation(mtype, _hyp(), genome, plan, manifest, brief, "v0", clip) is None, mtype


def test_mutation_renders_with_matching_duration_and_no_join_click(clip: Path, tmp_path: Path) -> None:
    genome, manifest, brief, plan = _setup(clip, ROLES)
    m = build_mutation(MutationType.RESULT_FIRST, _hyp(), genome, plan, manifest, brief, "v0", clip)
    assert m is not None
    new = apply_ops(plan, m.ops)
    res = render_plan(new, manifest, {VIDEO_ASSET_ID: clip}, publish_dir=tmp_path / "r")
    assert abs(res.duration_ms - new.timeline_duration_ms()) <= 70
    assert inspect_media(res.path).has_audio
    pcm = np.frombuffer(
        subprocess.run([FFMPEG, "-nostdin", "-loglevel", "error", "-i", str(res.path), "-vn", "-ac", "1", "-ar", "48000", "-f", "f32le", "-"], capture_output=True, check=True).stdout,
        dtype=np.float32,
    )
    jump = np.abs(np.diff(pcm))
    join = 2000 * 48  # the moved payoff beat ends at 2.0 s
    assert jump[join - 480 : join + 480].max() <= max(0.05, float(np.percentile(jump, 99.9)) * 1.5), "audible click at the join"


def test_punch_in_is_refused_when_it_would_upscale_a_tiny_source(tmp_path: Path) -> None:
    tiny = tmp_path / "tiny.mp4"
    subprocess.run(
        [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=180x320:d=8:r=30",
         "-f", "lavfi", "-i", "sine=frequency=220:duration=8", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(tiny)],
        check=True, timeout=60,
    )
    genome, manifest, brief, plan = _setup(tiny, ROLES)
    assert build_mutation(MutationType.PATTERN_INTERRUPT, _hyp(), genome, plan, manifest, brief, "v0", tiny) is None
