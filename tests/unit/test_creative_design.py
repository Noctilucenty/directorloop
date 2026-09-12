"""Policy statuses, investigator hypotheses, and the designer's memory effect on the first experiment."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from directorloop.creative.design import design_experiment
from directorloop.creative.investigate import investigate
from directorloop.creative.mutate import experiment_brief, identity_plan, source_manifest
from directorloop.creative.policy import CreativePolicyStore, StrategyEvidence
from directorloop.domain.creative import (
    BeatRole,
    HypothesisFamily,
    MutationType,
    RetentionPoint,
    RetentionSeries,
    RetentionSourceType,
)
from directorloop.domain.ids import sha256_file
from directorloop.media.probe import FFMPEG
from tests.unit.test_creative_mutations import make_genome


@pytest.fixture(scope="module")
def clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("design") / "src.mp4"
    subprocess.run(
        [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=540x960:d=12:r=30",
         "-f", "lavfi", "-i", "sine=frequency=220:duration=12", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
        check=True, timeout=60,
    )
    return path


def _ev(outcome: str, exp: str = "exp_a") -> StrategyEvidence:
    return StrategyEvidence(experiment_id=exp, video_id="video_a", arm_id="A", outcome=outcome, evidence_class="model_eval", primary_metric="model_hook_preference")


def test_policy_status_follows_recorded_counts() -> None:
    store = CreativePolicyStore()
    s = store.get_or_create(MutationType.PROOF_EARLIER, "educational_short")
    assert s.status == "PROPOSED" and s.evidence_mean() == 0.5
    store.record_outcome(MutationType.PROOF_EARLIER, "educational_short", _ev("win"))
    assert s.status == "SUPPORTED_OFFLINE" and s.evidence_mean() > 0.5
    store.record_outcome(MutationType.PROOF_EARLIER, "educational_short", _ev("loss", "exp_b"))
    assert s.status == "PROPOSED"
    store.record_outcome(MutationType.PROOF_EARLIER, "educational_short", _ev("loss", "exp_c"))
    assert s.status == "CONTRADICTED"
    store.record_outcome(MutationType.PROOF_EARLIER, "educational_short", _ev("rejected", "exp_d"))
    assert s.status == "CONTRADICTED" and s.evidence_mean() < 0.5, "one win keeps it contradicted, not rejected"
    assert store.version == 4 and len(store.changes) == 4 and store.changes[0].before["status"] == "PROPOSED"
    assert len(s.evidence) == 4, "losses stay in memory"
    r = store.get_or_create(MutationType.PATTERN_INTERRUPT, "educational_short")
    store.record_outcome(MutationType.PATTERN_INTERRUPT, "educational_short", _ev("loss", "exp_e"))
    assert r.status == "CONTRADICTED"
    store.record_outcome(MutationType.PATTERN_INTERRUPT, "educational_short", _ev("loss", "exp_f"))
    assert r.status == "REJECTED"


ROLES = [BeatRole.HOOK, BeatRole.CONTEXT, BeatRole.CONTEXT, BeatRole.MECHANISM, BeatRole.PROOF, BeatRole.PAYOFF]


def _genome(clip: Path, roles: list[BeatRole] = ROLES, static: tuple[int, int] | None = (6200, 3400)):
    return make_genome(sha256_file(clip), roles, beat_ms=2000, static=static)


def test_investigator_finds_competing_hypotheses(clip: Path) -> None:
    inv = investigate(_genome(clip))
    families = {h.family for h in inv.hypotheses}
    assert HypothesisFamily.PROOF_LATENCY in families  # proof at 8.0 s of 12 s
    assert HypothesisFamily.CONTEXT_INTERRUPTION in families  # 4 s of context before it
    assert HypothesisFamily.VISUAL_STAGNATION in families  # 3.4 s static span
    assert inv.weak_region is not None and inv.weak_region.basis == "creative structure"
    assert "no retention data" in inv.retention_note
    for h in inv.hypotheses:
        assert h.evidence and h.candidate_mutations and 0 < h.detection_confidence < 1


def test_investigator_does_not_flag_early_payoff(clip: Path) -> None:
    roles = [BeatRole.HOOK, BeatRole.PAYOFF, BeatRole.MECHANISM, BeatRole.CTA]
    inv = investigate(make_genome(sha256_file(clip), roles, beat_ms=3000, static=None))
    assert HypothesisFamily.PROOF_LATENCY not in {h.family for h in inv.hypotheses}


def test_measured_drop_becomes_the_weak_region(clip: Path) -> None:
    pts = [RetentionPoint(t_ms=t, remaining_fraction=f) for t, f in [(0, 1.0), (3000, 0.9), (4000, 0.88), (5000, 0.62), (6000, 0.6), (12000, 0.55)]]
    series = RetentionSeries(source_type=RetentionSourceType.HISTORICAL_OWNED, platform="facebook", points=pts, caveats=["first buckets carry no information"])
    inv = investigate(_genome(clip), retention=series)
    assert inv.weak_region is not None and inv.weak_region.basis == "measured retention drop"
    assert inv.weak_region.start_ms == 4000 and inv.weak_region.end_ms == 5000
    ctx = next(h for h in inv.hypotheses if h.family == HypothesisFamily.CONTEXT_INTERRUPTION)
    assert "measured retention drop" in ctx.region_basis
    assert any(e.kind.value == "historical" for e in ctx.evidence)


def _design(clip: Path, store: CreativePolicyStore, mode: str):
    g = _genome(clip)
    manifest = source_manifest("p", clip, g.artifact_hash)
    brief = experiment_brief("p", g, "")
    plan = identity_plan(g)
    inv = investigate(g)
    return design_experiment(
        hypotheses=inv.hypotheses, genome=g, plan=plan, manifest=manifest, brief=brief, video_path=clip,
        parent_version_id="v0", policy=store, corpus=None, policy_mode=mode, max_arms=3,
    )


def test_memory_changes_the_first_experiment_and_no_memory_mode_does_not(clip: Path) -> None:
    empty = CreativePolicyStore()
    learned0 = _design(clip, empty, "learned")
    none0 = _design(clip, empty, "none")
    assert [c.mutation.type for c in learned0.candidates] == [c.mutation.type for c in none0.candidates], "identical before any experiment"
    assert len(learned0.selected) >= 2
    first = learned0.candidates[0].mutation.type
    other = next(c.mutation.type for c in learned0.candidates if c.mutation.type != first)
    store = CreativePolicyStore()
    store.record_outcome(first, "educational_short", _ev("loss", "exp_1"))
    store.record_outcome(first, "educational_short", _ev("loss", "exp_2"))
    store.record_outcome(other, "educational_short", _ev("win", "exp_1"))
    store.record_outcome(other, "educational_short", _ev("win", "exp_2"))
    learned1 = _design(clip, store, "learned")
    none1 = _design(clip, store, "none")
    assert learned1.candidates[0].mutation.type == other, "prior evidence must change what is tried first"
    assert none1.candidates[0].mutation.type == first, "the no-memory baseline ignores experiment evidence"
    top = learned1.candidates[0].ranking
    assert top.policy_wins == 2 and "2 win(s)" in top.reason


def test_designer_selects_distinct_families_and_dedupes_identical_plans(clip: Path) -> None:
    design = _design(clip, CreativePolicyStore(), "learned")
    fams = [c.hypothesis.family for c in design.selected]
    assert len(set(fams)) == len(fams), "arms should test different explanations when possible"
    hashes = [c.plan_hash for c in design.candidates]
    assert len(hashes) == len(set(hashes))
