"""Conservative rejection and learning admission differ, especially after a provider failure."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from directorloop.planning import causal as P
from directorloop.planning.evidence_admission import comparison_learning_blockers, legacy_evaluation_receipt
from directorloop.runtime import causal as C
from tests.unit.test_causal_planner import World, bundle, comparison, install


def test_failed_pairwise_evaluation_can_reject_but_cannot_teach(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world = World(outcomes={"TRIM_PAUSE": "regression", "PUNCH_IN": "unstable"}, comparison_updates={"PUNCH_IN": {
        "full_preference": None, "regressed": ["predicted attention rises in the candidate"],
        "protected_checks": [{"item": "the payoff sentence", "kind": "unknown", "status": "unclear", "evidence": "check failed: provider quota exhausted"}]}})
    videos = install(monkeypatch, tmp_path / "stages", world)
    data = tmp_path / "data"
    run = C.run_causal(C.CausalConfig(video_id="video-a", video_path=str(videos["a"])), bundle(), data)
    first, partial = run.arms
    assert [a.verdict for a in run.arms] == ["loss", "loss"], "keep the conservative rejection when an independent audit flags a regression"
    assert first.evaluation_complete is True and first.policy_eligible is True
    assert partial.evaluation_complete is False and partial.policy_eligible is False and partial.policy_record_id is None
    assert any("whole-video" in x for x in partial.learning_blockers) and any("protected check" in x for x in partial.learning_blockers)
    policy = P.load_conditional_policy(P.policy_path(data))
    assert len(policy.records) == 1 and policy.records[0].mutation_type == "TRIM_PAUSE"
    assert policy.evaluation_receipts[policy.records[0].id]["complete"] is True
    assert run.hypothesis_results[partial.hypothesis_id].startswith("unresolved")
    assert any("learning withheld" in x for x in run.conclusions)
    saved = C.load_causal(data, run.id)
    assert saved is not None and saved.arms[1].learning_blockers == partial.learning_blockers
    update = next(s for s in run.workflow_stages if s.key == "update_policy")
    assert "whole-video" in update.summary["not_learned"][0]


def test_failed_or_missing_protected_checks_block_even_a_stable_negative_comparison() -> None:
    comp = comparison("regression", protected_requested=["keep CTA"], protected_checks=[{"item": "keep CTA", "status": "unclear", "evidence": "request failed"}]).model_dump(mode="json")
    assert any("protected check" in x for x in comparison_learning_blockers(comp))
    comp["protected_checks"] = []
    assert comparison_learning_blockers(comp)
    comp["protected_checks"] = [{"item": "keep CTA", "status": "violated", "evidence": "CTA removed"}]
    assert comparison_learning_blockers(comp) == [], "a completed negative check remains valid evidence"


def legacy_fixture(data: Path, *, partial: bool = False) -> P.PolicyRecord:
    record = P.PolicyRecord(id="prec_ab_cd", created_at="t", video_id="video-old", run_id="causal_ab_cd", experiment_id="X1", cause_type="static_visual",
                            mutation_type="PUNCH_IN", artifact_hash="a" * 64, variant_artifact_hash="b" * 64, plan_hash="c" * 64,
                            comparison_version=P.COMPARISON_VERSION, comparison_outcome="insufficient_evidence" if partial else "regression", conditions={"category": "educational_short"}, outcome="loss")
    comp = comparison("unstable" if partial else "regression", full_preference=None if partial else 0.0).model_dump(mode="json")
    arm = {"experiment_id": "X1", "policy_record_id": record.id, "render_hash": record.variant_artifact_hash, "plan_hash": record.plan_hash, "verdict": "loss",
           "verified": True, "candidate_audit_id": "audit_ab_cd", "comparison": comp}
    run = {"id": record.run_id, "artifact_hash": record.artifact_hash, "arms": [arm]}
    audit = {"id": "audit_ab_cd", "status": "complete", "artifact_hash": record.variant_artifact_hash}
    (data / "causal").mkdir(parents=True)
    (data / "audit/audit_ab_cd").mkdir(parents=True)
    (data / "causal" / f"{record.run_id}.json").write_text(json.dumps(run))
    (data / "audit/audit_ab_cd/audit.json").write_text(json.dumps(audit))
    P.save_conditional_policy(P.ConditionalPolicy(records=[record]), P.policy_path(data))
    return record


def test_legacy_records_are_checked_without_rewriting_history(tmp_path: Path) -> None:
    data = tmp_path / "legacy"
    record = legacy_fixture(data)
    policy_path = P.policy_path(data)
    before = policy_path.read_bytes()
    loaded = P.load_conditional_policy(policy_path)
    assert policy_path.read_bytes() == before
    assert loaded.records[0].model_dump() == record.model_dump()
    assert loaded.evaluation_receipts[record.id]["complete"] is True
    assert P.prior_support(loaded, record.cause_type, record.mutation_type, record.conditions, target_artifact_hash="d" * 64).records == 1
    failed_data = tmp_path / "failed"
    failed = legacy_fixture(failed_data, partial=True)
    loaded_failed = P.load_conditional_policy(P.policy_path(failed_data))
    assert loaded_failed.records[0].outcome == "loss", "historical conservative decision is immutable"
    prior = P.prior_support(loaded_failed, failed.cause_type, failed.mutation_type, failed.conditions, target_artifact_hash="d" * 64)
    assert prior.records == 0 and "evaluation incomplete" in prior.excluded[0]["reason"]


def test_portable_policy_evidence_requires_exact_manifest_hashes(tmp_path: Path) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    record = legacy_fixture(source)
    folder = target / "policy-evidence"
    folder.mkdir(parents=True)
    files = {}
    for name, path in [(f"{record.run_id}.json", source / "causal" / f"{record.run_id}.json"), ("audit_ab_cd.json", source / "audit/audit_ab_cd/audit.json")]:
        raw = path.read_bytes()
        (folder / name).write_bytes(raw)
        files[name] = {"sha256": hashlib.sha256(raw).hexdigest(), "source": str(path)}
    (folder / "manifest.json").write_text(json.dumps({"files": files}))
    receipt = legacy_evaluation_receipt(record.model_dump(mode="json"), target)
    assert receipt["complete"] is True and len(receipt["evidence_sha256"]) == 2
    (folder / "audit_ab_cd.json").write_text("{}")
    assert legacy_evaluation_receipt(record.model_dump(mode="json"), target)["complete"] is False
