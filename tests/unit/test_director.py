"""Runtime loop decisions with scripted stages: what happens after a regression, a failed render, an unverified change,
an incomplete audit, a version-label mismatch, an exhausted budget and a refused input. No network, no model calls."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from directorloop.audit.models import (
    AuditComparison,
    AuditFinding,
    AuditRepair,
    AuditReport,
    ChangeVerification,
    CoverageRecord,
)
from directorloop.audit.repairs import RepairCandidate
from directorloop.audit.review import Word
from directorloop.audit.revise import removed_source_ranges, sentence_presence
from directorloop.creative.mutate import identity_plan
from directorloop.creative.policy import load_policy
from directorloop.domain.creative import BeatRole
from directorloop.domain.edit_plan import MoveSegment, RemoveSegment, TrimSegment, apply_ops
from directorloop.domain.ids import sha256_file
from directorloop.media.probe import FFMPEG
from directorloop.providers.base import ProviderCapability
from directorloop.providers.registry import ProviderBundle
from directorloop.runtime import director as D
from tests.unit.test_creative_mutations import make_genome

ROLES = [BeatRole.HOOK, BeatRole.CONTEXT, BeatRole.PROOF, BeatRole.PAYOFF]


class FakeProvider:
    def __init__(self, name: str = "fake") -> None:
        self.capability = ProviderCapability(name=name, role="probe", model="scripted", modalities={"text", "image"}, structured_output=True, docs_ref="", present=True)


def finding(fid: str, start: int = 2000, end: int = 4000, severity: str = "medium", objective: str = "attention") -> AuditFinding:
    return AuditFinding(id=fid, version_id="v", start_ms=start, end_ms=end, weakness=f"weakness {fid}", issue_type="visual_stagnation", objective=objective,
                        severity=severity, uncertainty="low", proposed_repair="tighten", repair_kind="trim_or_tighten", keep_unchanged=["the ending"])


@dataclass
class Script:
    """What each stage returns. Keys are candidate keys chosen by the fake selector."""

    findings_by_version: dict[str, list[AuditFinding]]
    choices: list[str]  # candidate keys offered in order; a key already excluded is skipped
    outcomes: dict[str, str] = field(default_factory=dict)  # key -> comparison outcome
    render_fail: set[str] = field(default_factory=set)
    unverified: set[str] = field(default_factory=set)
    incomplete_audit_versions: set[str] = field(default_factory=set)
    wrong_hash_versions: set[str] = field(default_factory=set)
    selector_calls: list[dict[str, Any]] = field(default_factory=list)
    audits: list[str] = field(default_factory=list)


def install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, script: Script) -> tuple[Path, Path]:
    video = tmp_path / "in.mp4"
    video.write_bytes(b"original-bytes")
    data = tmp_path / "data"
    renders = tmp_path / "renders"
    renders.mkdir()
    version_of_path: dict[str, str] = {str(video): "v0"}

    monkeypatch.setattr(D, "validate_input", lambda config: None)
    monkeypatch.setattr(D, "source_manifest", lambda *a, **k: None)
    monkeypatch.setattr(D, "extract_genome", lambda path, *a, **k: make_genome(sha256_file(path), ROLES))

    def fake_audit(*, video_id: str, video_path: Path, version_id: str, provider: Any, data_dir: Path, on_stage: Any = None, closeups: bool = True) -> AuditReport:
        key = version_of_path.get(str(video_path), "v0")
        script.audits.append(key)
        h = sha256_file(video_path)
        return AuditReport(id=f"audit_{len(script.audits):x}_00", video_id=video_id, version_id=version_id, artifact_hash="0" * 64 if key in script.wrong_hash_versions else h,
                           artifact_path=str(video_path), duration_ms=8000, created_at="t", coverage=CoverageRecord(mode="sampled_frames_plus_asr", duration_ms=8000),
                           findings=[f.model_copy(deep=True) for f in script.findings_by_version.get(key, [])],
                           status="incomplete" if key in script.incomplete_audit_versions else "complete",
                           incomplete_reasons=["2 of 4 window reviews failed"] if key in script.incomplete_audit_versions else [])

    def fake_map(f: AuditFinding, genome: Any, path: Path, project_id: str, planner: Any, source_project: Any, exclude_keys: tuple = (), prior_attempts: tuple = (),
                 constraints: tuple = (), objective: str = "", allowed_types: tuple | None = None) -> tuple[AuditRepair, RepairCandidate | None]:
        script.selector_calls.append({"finding": f.id, "exclude": exclude_keys, "prior": prior_attempts, "constraints": constraints, "objective": objective, "allowed": allowed_types})
        plan = identity_plan(genome)
        for key in script.choices:
            if key in exclude_keys:
                continue
            mtype, ops = {"remove": ("REMOVE_BEAT", [RemoveSegment(segment_id="beat_01")]), "later": ("MOVE_BEAT_LATER", [MoveSegment(segment_id="beat_01", after_segment_id="beat_02")]),
                          "trim": ("TRIM_PAUSE", [TrimSegment(segment_id="beat_01", source_in_ms=2000, source_out_ms=3500)])}[key.split("_")[0]]
            if allowed_types is not None and mtype not in allowed_types:
                continue
            cand = RepairCandidate(key=key, mutation_type=mtype, description=f"edit {key}", ops=ops, secondary_changes=[], plan=apply_ops(plan, ops))
            return AuditRepair(summary="tighten", route="A", runnable=True, mutation_type=mtype, edit_description=cand.description, selection_reason=f"chose {key}"), cand
        return AuditRepair(summary="tighten", route="B", runnable=False, why_not_runnable="no untried existing-video edit implements it", selection_reason="none fits"), None

    def fake_render(plan: Any, manifest: Any, path: Path, data_dir: Path) -> dict[str, Any]:
        key = next(k for k in script.choices if [c for c in script.selector_calls][-1] is not None and apply_key_matches(plan, k))
        if key in script.render_fail:
            raise RuntimeError("ffmpeg exited 1")
        out = renders / f"{key}.mp4"
        out.write_bytes(f"render-{key}".encode())
        version_of_path[str(out)] = key
        return {"path": str(out), "artifact_hash": sha256_file(out), "duration_ms": 6000, "render_ms": 5}

    def apply_key_matches(plan: Any, key: str) -> bool:
        ids = [s.id for s in plan.segments]
        if key.startswith("remove"):
            return "beat_01" not in ids
        if key.startswith("later"):
            return ids.index("beat_01") > ids.index("beat_02")
        return any(s.id == "beat_01" and s.source_out_ms == 3500 for s in plan.segments)

    def fake_verify(mutation_type: str, description: str, original_path: Path, candidate_path: Path, *a: Any, **k: Any) -> ChangeVerification:
        key = version_of_path[str(candidate_path)]
        return ChangeVerification(intended=description, verified=key not in script.unverified, checks=["scripted"])

    def fake_compare(*, provider: Any, original: AuditReport, candidate: AuditReport, finding: AuditFinding, candidate_plan: Any, protected_items: list[str]) -> AuditComparison:
        key = version_of_path[candidate.artifact_path]
        outcome = script.outcomes.get(key, "tie")
        return AuditComparison(outcome=outcome, target_resolved="yes" if outcome in ("improvement", "mixed") else "no",
                               regressed=["whole video: the original was preferred (0.00)"] if outcome in ("regression", "mixed") else [],
                               improved=["target moment preferred"] if outcome in ("improvement", "mixed") else [], full_preference=0.0 if outcome == "regression" else 0.75)

    monkeypatch.setattr(D, "run_audit", fake_audit)
    monkeypatch.setattr(D, "map_repair", fake_map)
    monkeypatch.setattr(D, "render_candidate", fake_render)
    monkeypatch.setattr(D, "verify_change", fake_verify)
    monkeypatch.setattr(D, "compare_versions", fake_compare)
    return video, data


def bundle() -> ProviderBundle:
    return ProviderBundle(probe=FakeProvider(), planner=FakeProvider())  # type: ignore[arg-type]


def config(video: Path, **kw: Any) -> D.RunConfig:
    return D.RunConfig(video_id="t-video", video_path=str(video), objective="keep viewers", constraints=["do not add text"], **kw)


def test_regression_leads_to_a_justified_alternative_that_is_accepted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    s = Script(findings_by_version={"v0": [finding("f1")], "later_a": []}, choices=["remove_a", "later_a"], outcomes={"remove_a": "regression", "later_a": "improvement"})
    video, data = install(monkeypatch, tmp_path, s)
    events: list[tuple[str, str]] = []
    run = D.run_director(config(video, iteration_budget=3), bundle(), data, on_stage=lambda st, m, d: events.append((st, m)))

    assert [i.decision for i in run.iterations] == ["reject_keep_current", "accept", "stop"]
    first, second, stop = run.iterations
    assert first.outcome == "regression" and first.next_action == "try_alternative" and "worse" in first.reason
    assert second.current_version_id == "v0", "the rejected candidate never becomes the base of the next attempt"
    # the selector saw the failed attempt and excluded it
    retry = [c for c in s.selector_calls if c["exclude"]][0]
    assert retry["exclude"] == ("remove_a",) and "regression" in retry["prior"][0] and retry["constraints"] == ("do not add text",)
    assert second.next_action == "continue_from_candidate"
    assert stop.reason.startswith("the audit of v2-") and "no weaknesses" in stop.reason, "the accepted version was audited fresh"
    assert run.final_version_id.startswith("v2-") and run.final_artifact_hash != run.original_artifact_hash
    assert run.final_decision.startswith("keep v2-") and run.status == "completed"
    assert s.audits == ["v0", "remove_a", "later_a"]
    assert run.mocked_stages and all("scripted test provider" in m for m in run.mocked_stages)
    assert run.manual_interventions == []
    # memory: one loss and one win recorded with their lessons
    lessons = [json.loads(line) for line in (data / "audit" / "lessons.jsonl").read_text().splitlines()]
    assert [ln["result"]["outcome"] for ln in lessons] == ["regression", "improvement"]
    pol = load_policy(data / "creative" / "policy.json")
    assert {(st.mutation_type.value, st.wins, st.losses) for st in pol.strategies} == {("REMOVE_REDUNDANT_BEAT", 0, 1), ("SHOT_REORDER", 1, 0)}
    stored = D.load_run(data, run.id)
    assert stored is not None and stored.iterations[0].repair_run_id and (data / "repairs" / f"{stored.iterations[0].repair_run_id}.json").exists()
    assert any(st == "DECIDED" for st, _ in events) and events[-1][0] == "DONE"


def test_failed_render_and_unverified_change_keep_the_original_then_stop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    s = Script(findings_by_version={"v0": [finding("f1")]}, choices=["remove_a", "later_a"], render_fail={"remove_a"}, unverified={"later_a"})
    video, data = install(monkeypatch, tmp_path, s)
    run = D.run_director(config(video, iteration_budget=4, max_attempts_per_finding=3), bundle(), data)
    assert [i.decision for i in run.iterations] == ["incomplete_keep_current", "incomplete_keep_current", "stop"]
    assert "render failed" in run.iterations[0].reason and run.iterations[0].candidate_audit_id is None
    assert run.iterations[1].change_verified is False and run.iterations[1].candidate_audit_id is None, "an unverified render is never reviewed as if it were the repair"
    assert "no runnable existing-video repair remains" in run.stop_reason
    assert run.final_version_id == "v0" and run.final_decision.startswith("retain the original: none of 2")
    assert s.audits == ["v0"]


def test_budget_exhaustion_and_attempt_limit_are_explicit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    s = Script(findings_by_version={"v0": [finding("f1")]}, choices=["remove_a", "later_a", "trim_a"], outcomes={"remove_a": "regression", "later_a": "tie", "trim_a": "mixed"})
    video, data = install(monkeypatch, tmp_path, s)
    run = D.run_director(config(video, iteration_budget=2), bundle(), data)
    assert [i.decision for i in run.iterations] == ["reject_keep_current", "reject_keep_current"]
    assert run.iterations[-1].next_action == "stop" and run.stop_reason == "iteration budget of 2 used"
    assert run.final_version_id == "v0"

    s2 = Script(findings_by_version={"v0": [finding("f1")]}, choices=["remove_a", "later_a", "trim_a"], outcomes={"remove_a": "regression", "later_a": "insufficient_evidence"})
    video2, data2 = install(monkeypatch, tmp_path / "b", s2) if (tmp_path / "b").mkdir() is None else (None, None)
    run2 = D.run_director(config(video2, iteration_budget=5, max_attempts_per_finding=2), bundle(), data2)
    assert [i.decision for i in run2.iterations] == ["reject_keep_current", "reject_keep_current", "stop"]
    assert "2 attempts on this finding already failed" in run2.stop_reason
    assert "not enough evidence" in run2.iterations[1].reason


def test_incomplete_original_audit_and_label_mismatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    s = Script(findings_by_version={"v0": [finding("f1")]}, choices=["remove_a"], incomplete_audit_versions={"v0"})
    video, data = install(monkeypatch, tmp_path, s)
    run = D.run_director(config(video), bundle(), data)
    assert run.iterations == [] and "incomplete" in run.stop_reason and run.final_version_id == "v0"

    (tmp_path / "c").mkdir()
    s2 = Script(findings_by_version={"v0": [finding("f1")]}, choices=["remove_a"], wrong_hash_versions={"remove_a"}, outcomes={"remove_a": "improvement"})
    video2, data2 = install(monkeypatch, tmp_path / "c", s2)
    run2 = D.run_director(config(video2, iteration_budget=1), bundle(), data2)
    assert run2.iterations[0].decision == "incomplete_keep_current" and "hash mismatch" in run2.iterations[0].reason
    assert run2.final_version_id == "v0", "an improvement verdict on a mislabeled file is void"


def test_focus_ranking_and_allowed_edits(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    s = Script(findings_by_version={"v0": [finding("f_attention", 1000, 2000, severity="high"), finding("f_comp", 5000, 6000, severity="low", objective="comprehension")]},
               choices=["remove_a", "trim_a"], outcomes={"trim_a": "tie"})
    video, data = install(monkeypatch, tmp_path, s)
    run = D.run_director(config(video, iteration_budget=1, focus="comprehension", allowed_edits=["TRIM_PAUSE"]), bundle(), data)
    it = run.iterations[0]
    assert it.finding_id == "f_comp" and "matches the run focus" in it.ranking_reason, "the run focus outranks severity"
    assert it.mutation_type == "TRIM_PAUSE" and all(c["allowed"] == ("TRIM_PAUSE",) for c in s.selector_calls)


def test_decide_next_action_rules() -> None:
    base = {"rendered": True, "verified": True, "labels_match": True, "candidate_audit_complete": True, "target_resolved": "yes", "regressed": []}
    assert D.decide_next_action(**base, outcome="improvement", budget_left=0) == {
        "decision": "accept", "reason": "the fresh audit and the comparisons show the target weakness resolved with no regression", "next_action": "stop",
        "next_action_reason": "iteration budget used"}
    assert D.decide_next_action(**{**base, "verified": False}, outcome="improvement", budget_left=2)["decision"] == "incomplete_keep_current"
    assert D.decide_next_action(**{**base, "candidate_audit_complete": False}, outcome="improvement", budget_left=2)["decision"] == "incomplete_keep_current"
    mixed = D.decide_next_action(**{**base, "regressed": ["protected content lost: the ending"]}, outcome="mixed", budget_left=1)
    assert mixed["decision"] == "reject_keep_current" and "protected content lost" in mixed["reason"] and mixed["next_action"] == "try_alternative"


def test_config_validation() -> None:
    with pytest.raises(ValueError):
        D.RunConfig(video_id="x1", video_path="/v.mp4", focus="virality")
    with pytest.raises(ValueError):
        D.RunConfig(video_id="x1", video_path="/v.mp4", allowed_edits=["GENERATE_SHOT"])
    with pytest.raises(ValueError):
        D.RunConfig(video_id="../x", video_path="/v.mp4")


def test_input_refusals_use_the_real_media_probe(tmp_path: Path) -> None:
    missing = D.validate_input(D.RunConfig(video_id="x1", video_path=str(tmp_path / "nope.mp4")))
    assert missing and "not found" in missing
    audio = tmp_path / "audio.m4a"
    subprocess.run([FFMPEG, "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=300:duration=3", "-c:a", "aac", str(audio)], check=True, timeout=60)
    refused = D.validate_input(D.RunConfig(video_id="x1", video_path=str(audio)))
    assert refused and "no video stream" in refused
    short = tmp_path / "short.mp4"
    subprocess.run([FFMPEG, "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=180x320:d=1:r=30", "-pix_fmt", "yuv420p", str(short)], check=True, timeout=60)
    assert "shorter" in (D.validate_input(D.RunConfig(video_id="x1", video_path=str(short))) or "")
    text = tmp_path / "notes.mp4"
    text.write_text("not a video")
    assert D.validate_input(D.RunConfig(video_id="x1", video_path=str(text)))


def test_failed_run_record_for_refused_input(tmp_path: Path) -> None:
    run = D.run_director(D.RunConfig(video_id="x1", video_path=str(tmp_path / "missing.mp4")), bundle(), tmp_path / "data")
    assert run.status == "failed" and run.stop_reason.startswith("input refused") and run.iterations == []
    assert D.load_run(tmp_path / "data", run.id) is not None


def test_sentence_presence_needs_word_pairs_and_position() -> None:
    words = [Word(t, i * 300, i * 300 + 250) for i, t in enumerate("the top flips over onto its stem and the red side faces down".split())]
    assert sentence_presence(words, "The top flips over onto its stem.") == 1.0
    assert sentence_presence(words, "The top flips over onto its stem.", 2400, 4000) < 0.5, "the sentence is not at that position"
    assert sentence_presence(words, "the red the top the stem") < 0.5, "common words alone do not count as the sentence"


def test_removed_source_ranges() -> None:
    g = make_genome("h" * 64, ROLES)
    plan = identity_plan(g)
    trimmed = apply_ops(plan, [TrimSegment(segment_id="beat_01", source_in_ms=2000, source_out_ms=3400)])
    assert removed_source_ranges(plan, trimmed) == [(3400, 4000)]
    moved = apply_ops(plan, [MoveSegment(segment_id="beat_01", after_segment_id="beat_02")])
    assert removed_source_ranges(plan, moved) == []
