"""Causal experiments: evidence for and against competing causes, single-variable experiment ranking, one intervention per
executable plan, the decision not to edit, verdicts and the regression gate, inconclusive results kept out of learning,
constraint coverage, the frozen-evaluator and blind-review checks, cancellation, conditional policy memory with provenance,
exact replay from frozen inputs, and a policy learned on one file changing the experiment ranking on another.
No network and no model calls: scripted stages stand in for the reviewer, the renderer and the comparison."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from directorloop.audit import attention as A
from directorloop.audit.models import (
    COMPARISON_VERSION,
    AuditComparison,
    AuditFinding,
    AuditReport,
    ChangeVerification,
    CoverageRecord,
    EvaluatorRecord,
    StabilityRecord,
    TimeRange,
    WindowReaction,
)
from directorloop.audit.repairs import RepairCandidate
from directorloop.audit.revise import unchecked_items
from directorloop.creative.mutate import identity_plan
from directorloop.creative.signals import VideoSignals
from directorloop.domain.creative import BeatRole, CreativeGenome
from directorloop.domain.edit_plan import (
    CropRect,
    EditPlan,
    MoveSegment,
    RemoveSegment,
    SetCrop,
    TrimSegment,
    apply_ops,
)
from directorloop.domain.ids import sha256_file
from directorloop.jobs.worker import JobCanceled
from directorloop.planning import causal as P
from directorloop.providers.registry import ProviderBundle
from directorloop.runtime import causal as C
from directorloop.runtime.budget import BudgetExceeded, CallBudget
from tests.unit.test_creative_mutations import make_genome
from tests.unit.test_director import FakeProvider

# the evaluator a fresh audit records for the scripted provider under default settings
EVAL = EvaluatorRecord(cold_viewer_prompt_version=A.COLD_VIEWER_PROMPT_VERSION, provider="fake", model="scripted", coarse_window_ms=2000, precision_enabled=True,
                       precision_window_ms=500, precision_lead_ms=500, max_precision_regions=2, max_precision_windows=10)
ENDING = [BeatRole.HOOK, BeatRole.CONTEXT, BeatRole.PROOF, BeatRole.PAYOFF]


def w(start: int, end: int, risk: str, reaction: str = "engaged", payoff: str = "none", since: int | None = None, cause: str = "") -> WindowReaction:
    return WindowReaction(start_ms=start, end_ms=end, attention_risk=risk, reaction=reaction, payoff=payoff, waiting_since_ms=since,  # type: ignore[arg-type]
                          understanding="a story about how a bridge was measured", cause=cause or f"reading at {start}")


def report(windows: list[WindowReaction], findings: list[AuditFinding], *, hash_: str = "0" * 64, path: str = "x", evaluator: EvaluatorRecord = EVAL,
           version: str = "v0", video_id: str = "video-a") -> AuditReport:
    duration = windows[-1].end_ms
    timeline = A.build_attention_timeline(windows, [], [], duration, evaluator)
    return AuditReport(id=f"audit_{version}_{hash_[:6]}", video_id=video_id, version_id=version, artifact_hash=hash_, artifact_path=path, duration_ms=duration,
                       created_at="t", coverage=CoverageRecord(mode="sampled_frames_plus_asr", duration_ms=duration), window_reactions=windows, attention=timeline,
                       findings=findings)


def signals(*, silent_from: int | None = None, static_from: int | None = None, duration: int = 8000) -> VideoSignals:
    rms = np.array([0.5 if silent_from is None or t < silent_from else 0.01 for t in range(0, duration, 10)])
    motion = [(t, 0.001 if static_from is not None and t >= static_from else 0.05) for t in range(0, duration + 1, 100)]
    return VideoSignals(duration_ms=duration, width=180, height=320, cuts_ms=[], motion=motion, rms=rms)


def dead_air_audit(*, issue_type: str = "other", repair_kind: str = "trim_or_tighten", **kw: Any) -> AuditReport:
    """The awaited payoff lands at 4-6 s; the picture then holds silently to 8 s, where predicted risk rises."""
    windows = [w(0, 2000, "low"), w(2000, 4000, "low", payoff="waiting", since=500), w(4000, 6000, "low", payoff="delivered"),
               w(6000, 8000, "medium", "losing_interest", cause="the screen holds silently on a static sign")]
    finding = AuditFinding(id="f_end", version_id="v0", start_ms=6000, end_ms=8000, weakness="the ending holds too long", issue_type=issue_type,
                           repair_kind=repair_kind, severity="medium", keep_unchanged=["the payoff sentence"])
    return report(windows, [finding], **kw)


def candidates(genome: CreativeGenome) -> tuple[list[RepairCandidate], EditPlan]:
    plan = identity_plan(genome)

    def cand(key: str, mtype: str, ops: list[Any], secondary: list[str]) -> RepairCandidate:
        return RepairCandidate(key=key, mutation_type=mtype, description=f"{mtype.lower()} on beat_03", ops=ops, secondary_changes=secondary, plan=apply_ops(plan, ops))

    return [
        cand("remove_beat_03", "REMOVE_BEAT", [RemoveSegment(segment_id="beat_03")], ["the payoff sentence is removed"]),
        cand("trim_end_beat_03", "TRIM_PAUSE", [TrimSegment(segment_id="beat_03", source_in_ms=6000, source_out_ms=6380)], ["the footage in the removed pause is dropped"]),
        cand("zoom_center_beat_03", "PUNCH_IN", [SetCrop(segment_id="beat_03", crop=CropRect(x=20, y=36, w=138, h=246))],
             ["burned-in captions near the frame edges may be cropped"]),
    ], plan


def comparison(kind: str, **update: Any) -> AuditComparison:
    agree = StabilityRecord(runs=4, agreeing_runs=2, order_flips=0, agreement="target: 1 of 1 order-swapped pairs agreed; whole video: 1 of 1 order-swapped pairs agreed")
    flip = StabilityRecord(runs=4, agreeing_runs=1, order_flips=1, agreement="target: 1 of 1 order-swapped pairs agreed; whole video: 0 of 1 order-swapped pairs agreed")
    return {
        "improvement": AuditComparison(outcome="improvement", target_resolved="yes", improved=["target moment preferred"], full_preference=0.75, target_preference=0.75,
                                       stability=agree),
        "unstable": AuditComparison(outcome="insufficient_evidence", target_resolved="unclear", full_preference=0.5, target_preference=0.75, stability=flip),
        "mixed": AuditComparison(outcome="mixed", target_resolved="yes", regressed=["protected content lost: the payoff sentence"], improved=["target moment preferred"],
                                 full_preference=0.5, stability=agree),
        "regression": AuditComparison(outcome="regression", target_resolved="no", regressed=["whole video: the original was preferred (0.00)"], full_preference=0.0,
                                      stability=agree),
        "tie": AuditComparison(outcome="tie", target_resolved="unclear", full_preference=0.5, stability=agree),
    }[kind].model_copy(update={"comparison_version": COMPARISON_VERSION, **update})


# ----------------------------------------------------------------------------- evidence and hypotheses


def test_dossier_weighs_evidence_for_and_against_each_cause() -> None:
    genome = make_genome("0" * 64, ENDING)
    d = P.build_dossier(dead_air_audit(), genome, signals(silent_from=6300, static_from=6000))
    assert d is not None and d.symptom.kind == "predicted_attention_rise" and (d.symptom.onset.start_ms, d.symptom.severity) == (6000, "medium")
    assert [(e.id, e.source, e.supports, e.against) for e in d.evidence] == [
        ("e1", "viewer_report", ["dead_air_after_payoff"], ["delayed_payoff"]),
        ("e2", "viewer_report", [], ["missing_context"]),
        ("e3", "viewer_report", [], []),
        ("e4", "mechanical", ["static_visual"], []),
        ("e5", "mechanical", ["dead_air_after_payoff"], []),
        ("e6", "diagnosis", ["dead_air_after_payoff"], []),
    ]
    assert "quoted cause at the onset (not classified)" in d.evidence[2].text, "the viewer's words ('silently', 'static') are quoted, never parsed into a cause"
    assert d.conditions == {"category": "educational_short", "symptom_position": "ending", "symptom_severity": "medium", "payoff_delivered_before_symptom": True,
                            "open_loop_unresolved_at_symptom": False, "static_picture_at_symptom": True, "silence_at_symptom": True, "confusion_at_symptom": False,
                            "hook_type": "surprising_claim"}
    hyps = P.competing_hypotheses(d)
    assert [(h.id, h.cause_type, h.status, h.strength, h.evidence_for, h.evidence_against) for h in hyps] == [
        ("H1", "dead_air_after_payoff", "supported", "high", ["e1", "e5", "e6"], []),
        ("H2", "static_visual", "supported", "low", ["e4"], []),
        ("H3", "delayed_payoff", "contradicted", "low", [], ["e1"]),
        ("H4", "missing_context", "contradicted", "low", [], ["e2"]),
    ]
    assert hyps[0].evidence_share == 0.75 and hyps[1].why_not_high == ["only 1 supporting item"] and not hyps[3].testable


def test_a_payoff_still_awaited_points_to_delay_and_rules_out_removing_the_payoff() -> None:
    genome = make_genome("1" * 64, [BeatRole.CONTEXT, BeatRole.SETUP, BeatRole.TENSION, BeatRole.PAYOFF])
    windows = [w(0, 2000, "low", payoff="waiting", since=200), w(2000, 4000, "low", payoff="waiting", since=200),
               w(4000, 6000, "medium", "losing_interest", payoff="waiting", since=300), w(6000, 8000, "low", payoff="delivered")]
    finding = AuditFinding(id="f_mid", version_id="v0", start_ms=4000, end_ms=6000, issue_type="delayed_expected_action", repair_kind="trim_or_tighten")
    d = P.build_dossier(report(windows, [finding], hash_="1" * 64), genome, signals())
    assert d is not None and d.conditions["open_loop_unresolved_at_symptom"] is True and d.conditions["payoff_delivered_before_symptom"] is False
    hyps = P.competing_hypotheses(d)
    assert (hyps[0].id, hyps[0].cause_type, hyps[0].strength) == ("H1", "delayed_payoff", "high")
    dead_air = next(h for h in hyps if h.cause_type == "dead_air_after_payoff")
    assert dead_air.status == "contradicted" and len(dead_air.evidence_against) == 2, "still waiting, and the sound continues"
    diagnosis = next(e for e in d.evidence if e.source == "diagnosis")
    assert diagnosis.supports == ["delayed_payoff"], "a trim-kind diagnosis does not count for dead air while the viewer is still waiting"
    assert any(e.source == "structure" and e.supports == ["delayed_payoff"] for e in d.evidence), "two set-up sentences lead into the rise and the payoff comes later"

    base = identity_plan(genome)
    remove_payoff = RepairCandidate("remove_beat_03", "REMOVE_BEAT", "remove the payoff", [RemoveSegment(segment_id="beat_03")], [],
                                    apply_ops(base, [RemoveSegment(segment_id="beat_03")]))
    move = [MoveSegment(segment_id="beat_03", after_segment_id="beat_01")]
    move_payoff = RepairCandidate("earlier_beat_03", "MOVE_BEAT_EARLIER", "move the payoff earlier", move, [], apply_ops(base, move))
    plan = P.plan_experiments(d, hyps, [remove_payoff, move_payoff], base, None)
    assert [(x.mutation_type, x.cause_type) for x in plan.experiments] == [("MOVE_BEAT_EARLIER", "delayed_payoff")], "removing the payoff never tests a delayed payoff"
    assert plan.decision == "run_experiments" and plan.chosen == ["X1"] and plan.experiments[0].information_gain == "medium"
    assert plan.uncertainty == "only H1 (delayed_payoff) has supporting evidence; its strength is high"
    assert plan.policy_effect.summary == "only one eligible experiment, so there was no ranking choice for the policy to change"


# ----------------------------------------------------------------------------- experiments and decisions


def test_competing_single_variable_experiments_are_ranked_from_evidence() -> None:
    genome = make_genome("0" * 64, ENDING)
    d = P.build_dossier(dead_air_audit(), genome, signals(silent_from=6300, static_from=6000))
    assert d is not None
    hyps = P.competing_hypotheses(d)
    cands, base = candidates(genome)
    plan = P.plan_experiments(d, hyps, cands, base, None)
    x1, x2 = plan.experiments
    assert (x1.id, x1.mutation_type, x1.hypothesis_id, x1.priority, x1.information_gain, x1.discriminating) == ("X1", "TRIM_PAUSE", "H1", 5.5, "high", True)
    assert (x2.id, x2.mutation_type, x2.hypothesis_id, x2.priority, x2.information_gain, x2.discriminating) == ("X2", "PUNCH_IN", "H2", 1.5, "high", True)
    assert x1.plan_hash == cands[1].plan.content_hash() and x1.intervention_id == f"trim_end_beat_03@{x1.plan_hash[:12]}"
    assert x1.changes == ["removed 6.4-8.0s (1.62s) of footage and its sound"] and x1.isolation == "single-variable" and "framing" in x1.unchanged
    assert x2.changes == ["framing: a punch-in on 6.0-8.0s"] and "framing" not in x2.unchanged and "nothing removed" in x2.unchanged
    assert x2.side_effects == ["burned-in captions near the frame edges may be cropped"], "known consequences the plan diff does not name are carried with the experiment"
    assert (x1.rank, x1.rank_without_policy, x2.rank, x2.rank_without_policy) == (1, 1, 2, 2)
    assert plan.decision == "run_experiments" and plan.chosen == ["X1", "X2"] == plan.chosen_without_policy
    assert "with a different edit, so the two results can be told apart" in plan.decision_reason
    assert plan.best_discriminating_test == "run TRIM_PAUSE alone and PUNCH_IN alone against the same original"
    assert plan.policy_effect.summary == "the policy did not change the ranking: no admitted earlier record matches these conditions for any queued edit"
    assert all(x.cause_type != "delayed_payoff" for x in plan.experiments), "a contradicted cause gets no experiment"
    assert P.plan_experiments(d, hyps, cands, base, None, arms=1).chosen == ["X1"]


def symptom_dossier(*items: P.EvidenceItem, beats: tuple[dict[str, Any], ...] = ()) -> P.Dossier:
    sym = P.Symptom(kind="predicted_attention_rise", start_ms=6000, end_ms=8000, onset=TimeRange(start_ms=6000, end_ms=8000), severity="medium", resolution_ms=2000,
                    description="predicted attention risk rises low -> medium")
    return P.Dossier(audit_id="a", video_id="v", duration_ms=8000, symptom=sym, evidence=list(items), beats=list(beats), conditions={"category": "educational_short"})


def item(n: int, supports: tuple[str, ...] = (), against: tuple[str, ...] = ()) -> P.EvidenceItem:
    return P.EvidenceItem(id=f"e{n}", source="mechanical", text=f"item {n}", supports=list(supports), against=list(against))


def test_one_executable_edit_is_one_intervention_even_when_it_fits_two_causes() -> None:
    genome = make_genome("2" * 64, [BeatRole.HOOK, BeatRole.SETUP, BeatRole.PAYOFF, BeatRole.OTHER])
    beats = ({"id": "beat_00", "role": "hook", "start_ms": 0, "end_ms": 2000, "redundant_with": None},
             {"id": "beat_01", "role": "setup", "start_ms": 2000, "end_ms": 4000, "redundant_with": "beat_00"},
             {"id": "beat_02", "role": "payoff", "start_ms": 4000, "end_ms": 6000, "redundant_with": None},
             {"id": "beat_03", "role": "other", "start_ms": 6000, "end_ms": 8000, "redundant_with": "beat_02"})
    d = symptom_dossier(item(1, ("delayed_payoff",)), item(2, ("delayed_payoff",)), item(3, ("redundant_information",)), item(4, ("redundant_information",)), beats=beats)
    hyps = P.competing_hypotheses(d)
    base = identity_plan(genome)
    remove_b1 = RepairCandidate("remove_beat_01", "REMOVE_BEAT", "remove the repeated set-up", [RemoveSegment(segment_id="beat_01")], [],
                                apply_ops(base, [RemoveSegment(segment_id="beat_01")]))
    shared = P.plan_experiments(d, hyps, [remove_b1], base, None)
    assert len(shared.experiments) == 1, "the same edit is never rendered twice as if it were two experiments"
    x = shared.experiments[0]
    assert (x.hypothesis_id, x.also_tests, x.also_causes, x.discriminating, x.information_gain) == ("H1", ["H2"], ["redundant_information"], False, "low")
    assert shared.chosen == ["X1"] and "so its result cannot tell those apart" in shared.decision_reason
    assert "the only available edit for both is the same" in shared.uncertainty and shared.best_discriminating_test == ""

    remove_b3 = RepairCandidate("remove_beat_03", "REMOVE_BEAT", "remove the repeated ending", [RemoveSegment(segment_id="beat_03")], [],
                                apply_ops(base, [RemoveSegment(segment_id="beat_03")]))
    split = P.plan_experiments(d, hyps, [remove_b1, remove_b3], base, None)
    assert sorted((z.edit_key, z.cause_type, z.discriminating) for z in split.experiments) == [
        ("remove_beat_01", "delayed_payoff", False), ("remove_beat_03", "redundant_information", True)], "a second eligible edit is preferred over reusing the first"
    assert len({z.plan_hash for z in split.experiments}) == 2 and "do not separate them" in split.decision_reason

    arms = [C.ArmResult(experiment_id="X1", hypothesis_id="H1", cause_type="delayed_payoff", also_tests=["H2"], mutation_type="REMOVE_BEAT", edit_key="remove_beat_01",
                        description="", verdict="win")]
    results, lines = C.conclude(shared, arms)
    assert results["H2"].endswith("(shared edit with H1, so this result does not separate them)")
    assert lines[0] == "One conclusive experiment: REMOVE_BEAT addresses H1, H2 with the same edit and was win; a shared edit cannot separate those explanations."


def test_do_not_edit_when_evidence_is_weak_absent_untestable_or_unaffordable() -> None:
    cands, base = candidates(make_genome("0" * 64, ENDING))

    def decide(d: P.Dossier, arms: int = 2) -> P.ExperimentPlan:
        return P.plan_experiments(d, P.competing_hypotheses(d), cands, base, None, arms=arms)

    weak = decide(symptom_dossier(item(1, ("dead_air_after_payoff",)), item(2, ("static_visual",))))
    assert weak.decision == "do_not_edit" and "every cause hypothesis is weak" in weak.decision_reason and weak.chosen == []
    assert len(weak.experiments) == 2, "the queue is still recorded, so what evidence is missing stays visible"
    absent = decide(symptom_dossier(item(1, against=("dead_air_after_payoff",))))
    assert absent.decision == "do_not_edit" and absent.decision_reason == "no cause hypothesis has supporting evidence"
    untestable = decide(symptom_dossier(item(1, ("missing_context",)), item(2, ("missing_context",))))
    assert untestable.decision == "do_not_edit" and untestable.decision_reason.endswith("tests the supported causes: missing_context")
    unaffordable = decide(symptom_dossier(item(1, ("dead_air_after_payoff",)), item(2, ("dead_air_after_payoff",))), arms=0)
    assert unaffordable.decision == "do_not_edit" and unaffordable.decision_reason == "no render budget"
    # found on a real ad: a strongly supported delayed payoff with no fitting edit, and one executable edit for a weak rival cause
    beats = ({"id": "beat_03", "role": "payoff", "start_ms": 6000, "end_ms": 8000, "redundant_with": None},)
    mismatched = P.plan_experiments(symptom_dossier(item(1, ("delayed_payoff",)), item(2, ("delayed_payoff",)), item(3, ("delayed_payoff",)), item(4, ("static_visual",)),
                                                    beats=beats), P.competing_hypotheses(symptom_dossier(item(1, ("delayed_payoff",)), item(2, ("delayed_payoff",)),
                                                                                                         item(3, ("delayed_payoff",)), item(4, ("static_visual",)))),
                                    cands, base, None)
    assert [x.cause_type for x in mismatched.experiments] == ["static_visual"], "removing the payoff sentence does not test a delayed payoff"
    assert mismatched.decision == "do_not_edit" and mismatched.chosen == []
    assert "best-supported cause H1 (delayed_payoff, high)" in mismatched.decision_reason and "H2 static_visual" in mismatched.decision_reason


def test_verdicts_follow_the_regression_gate_and_keep_uncertainty_out_of_outcomes() -> None:
    ok: dict[str, Any] = {"rendered": True, "verified": True, "candidate_audit_complete": True, "evaluator_diff": []}
    assert C.arm_verdict(**ok, comparison=comparison("improvement"))[0] == "win"
    assert C.arm_verdict(**ok, comparison=comparison("tie"))[0] == "neutral"
    verdict, reason = C.arm_verdict(**ok, comparison=comparison("unstable"))
    assert verdict == "inconclusive" and "effect of the edit is unknown" in reason, "an order-dependent or failed comparison is not an observed null effect"
    verdict, reason = C.arm_verdict(**ok, comparison=comparison("mixed"))
    assert verdict == "rejected" and "protected content lost" in reason
    assert C.arm_verdict(**ok, comparison=comparison("regression"))[0] == "loss"
    unstable_but_worse = comparison("unstable", regressed=["predicted attention: a new high-risk moment at 2.0-2.5s"])
    assert C.arm_verdict(**ok, comparison=unstable_but_worse)[0] == "loss", "an order-dependent preference never hides a regression"
    unchecked = comparison("improvement", protected_unchecked=["do not add text"])
    verdict, reason = C.arm_verdict(**ok, comparison=unchecked)
    assert verdict == "inconclusive" and "do not add text" in reason, "a constraint that was never checked cannot be claimed as respected"
    assert C.arm_verdict(**{**ok, "rendered": False}, comparison=None)[0] == "incomplete"
    assert C.arm_verdict(**{**ok, "verified": False}, comparison=comparison("improvement"))[0] == "incomplete"
    assert C.arm_verdict(**{**ok, "candidate_audit_complete": False}, comparison=comparison("improvement"))[0] == "incomplete"
    assert C.arm_verdict(**{**ok, "evaluator_diff": ["model: a for the original, b for the variant"]}, comparison=comparison("improvement"))[0] == "incomplete"
    assert C.regression_check(comparison("mixed")) == (False, ["protected content lost: the payoff sentence"])
    assert set(C.LEARNED_VERDICTS) == {"win", "neutral", "loss", "rejected"}


def test_unchecked_protected_items_are_found_by_their_words() -> None:
    results = [{"item": "The payoff sentence", "status": "kept"}, {"item": "no new text on screen (rule)", "status": "respected"}]
    assert unchecked_items(["the payoff sentence", "no new text on screen", "keep the music"], results) == ["keep the music"]
    assert unchecked_items(["x"], []) == ["x"]


def test_the_variant_must_be_reviewed_by_the_frozen_evaluator() -> None:
    assert C.evaluator_differences(EVAL, EVAL.model_copy()) == []
    assert C.evaluator_differences(EVAL, EVAL.model_copy(update={"model": "other", "coarse_window_ms": 1000})) == [
        "model: scripted for the original, other for the variant", "coarse_window_ms: 2000 for the original, 1000 for the variant"]
    assert C.evaluator_differences(EVAL, None)


def test_conclusions_state_what_the_results_favour_and_what_stays_open() -> None:
    genome = make_genome("0" * 64, ENDING)
    d = P.build_dossier(dead_air_audit(), genome, signals(silent_from=6300, static_from=6000))
    assert d is not None
    cands, base = candidates(genome)
    plan = P.plan_experiments(d, P.competing_hypotheses(d), cands, base, None)
    x1, x2 = plan.experiments

    def arm(x: P.Experiment, verdict: str) -> C.ArmResult:
        return C.ArmResult(experiment_id=x.id, hypothesis_id=x.hypothesis_id, cause_type=x.cause_type, mutation_type=x.mutation_type, edit_key=x.edit_key,
                           description="", verdict=verdict)  # type: ignore[arg-type]

    results, lines = C.conclude(plan, [arm(x1, "win"), arm(x2, "neutral")])
    assert lines[0] == ("The results favour H1 (dead_air_after_payoff) over H2 (static_visual): TRIM_PAUSE alone improved the video, "
                        "while PUNCH_IN alone was neutral.")
    assert results == {"H1": C.HYPOTHESIS_STATUS["win"], "H2": C.HYPOTHESIS_STATUS["neutral"], "H3": "not tested: contradicted by e1", "H4": "not tested: contradicted by e2"}
    assert C.conclude(plan, [arm(x1, "neutral"), arm(x2, "loss")])[1][0].startswith("No edit produced an improvement")
    assert C.conclude(plan, [arm(x1, "win"), arm(x2, "win")])[1][0].startswith("More than one edit improved the video")
    one = C.conclude(plan, [arm(x1, "win"), arm(x2, "inconclusive")])
    assert "does not separate the explanations" in one[1][0] and one[0]["H2"].startswith("unresolved")
    assert C.conclude(plan, [arm(x1, "inconclusive"), arm(x2, "inconclusive")])[1][0] == "No experiment gave a conclusive result; the cause of the weakness remains undetermined."


def test_policy_prior_admits_matching_records_from_other_files_with_full_provenance() -> None:
    cond = {"category": "educational_short", "symptom_position": "ending", "symptom_severity": "medium", "silence_at_symptom": True}
    far = {"category": "educational_short", "symptom_position": "opening", "symptom_severity": "high", "silence_at_symptom": False}

    def rec(outcome: str, conditions: dict[str, Any], mtype: str = "PUNCH_IN", cause: str = "static_visual", artifact: str = "", mocked: bool = False,
            version: str = COMPARISON_VERSION) -> P.PolicyRecord:
        x = P.Experiment(id="X1", hypothesis_id="H1", cause_type=cause, edit_key="k", mutation_type=mtype, description="")
        return P.new_policy_record(video_id="v1", run_id="r", experiment=x, conditions=conditions, outcome=outcome, evidence="scripted", artifact_hash=artifact, mocked=mocked,
                                   comparison_version=version)

    policy = P.ConditionalPolicy(records=[rec("win", cond), rec("loss", {**cond, "symptom_severity": "high"}), rec("win", far),
                                          rec("loss", cond, mtype="TRIM_PAUSE", cause="dead_air_after_payoff")])
    policy.evaluation_receipts = {r.id: {"complete": True, "blockers": [], "method": "scripted test receipt"} for r in policy.records}
    prior = P.prior_support(policy, "static_visual", "PUNCH_IN", cond)
    assert prior.records == 2 and prior.score == round((1.0 - 0.75) / 1.75, 2), "the opening, high-severity record matches 1 of 4 conditions and is ignored"
    assert [m["weight"] for m in prior.matches] == [round(1 / 1.75, 4), round(0.75 / 1.75, 4)] and prior.excluded[0]["reason"] == "conditions match 0.25, below 0.5"
    assert P.similarity(cond, {**cond, "silence_at_symptom": None}) == 1.0, "an unknown condition is not counted as a mismatch"
    assert P.prior_support(policy, "static_visual", "PUNCH_IN", {"category": "product_demo"}).records == 0
    assert P.policy_statements(policy) == [
        "WHEN category=educational_short; symptom_position=ending; symptom_severity=medium; silence_at_symptom=True THEN TRIM_PAUSE for dead_air_after_payoff: "
        "0 win, 0 neutral, 1 loss, 0 rejected",
        "WHEN category=educational_short THEN PUNCH_IN for static_visual: 2 win, 0 neutral, 1 loss, 0 rejected",
    ]

    target, other = "a" * 64, "b" * 64
    many = P.ConditionalPolicy(records=[rec("win", cond, artifact=other) for _ in range(7)] + [rec("loss", cond, artifact=target), rec("loss", cond, artifact=other, mocked=True),
                                                                                                rec("loss", cond), rec("loss", cond, artifact=other, version="")])
    many.evaluation_receipts = {r.id: {"complete": True, "blockers": [], "method": "scripted test receipt"} for r in many.records}
    admitted = P.prior_support(many, "static_visual", "PUNCH_IN", cond, target_artifact_hash=target)
    assert admitted.records == 7 and len(admitted.matches) == 7 and admitted.score == 1.0, "every contributing record is listed, none truncated"
    assert sorted(x["reason"].split(":")[0].split(",")[0] for x in admitted.excluded) == [
        "judged by comparison compare-v1", "same source file", "scripted test run", "source file not recorded"]
    assert P.prior_support(many, "static_visual", "PUNCH_IN", cond, target_artifact_hash=target, include_mocked=True).records == 8


# ----------------------------------------------------------------------------- the loop with scripted stages


@dataclass
class World:
    outcomes: dict[str, str] = field(default_factory=dict)  # mutation type -> scripted comparison
    comparison_updates: dict[str, dict[str, Any]] = field(default_factory=dict)
    render_fail: set[str] = field(default_factory=set)
    unverified: set[str] = field(default_factory=set)
    changed_evaluator: set[str] = field(default_factory=set)
    limit_on: set[str] = field(default_factory=set)  # mutation types whose blind audit reaches the model-call limit
    audit_calls: list[dict[str, Any]] = field(default_factory=list)
    renders: list[str] = field(default_factory=list)
    compares: list[str] = field(default_factory=list)
    protected_seen: list[list[str]] = field(default_factory=list)


def install(monkeypatch: pytest.MonkeyPatch, root: Path, world: World) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    videos = {}
    for name in ("a", "b", "quiet"):
        videos[name] = root / f"{name}.mp4"
        videos[name].write_bytes(f"video-{name}".encode())
    kinds = {str(p): name for name, p in videos.items()}
    renders = root / "renders"
    renders.mkdir()
    plans: dict[str, str] = {}

    def fake_candidates(finding: AuditFinding, genome: CreativeGenome, video: Path, project_id: str) -> tuple[list[RepairCandidate], EditPlan, None, None]:
        cands, base = candidates(genome)
        plans.update({c.plan.model_dump_json(): c.mutation_type for c in cands})
        return cands, base, None, None

    def fake_audit(**kw: Any) -> AuditReport:
        world.audit_calls.append(kw)
        path = Path(kw["video_path"])
        kind, h = kinds.get(str(path), ""), sha256_file(path)
        if kind == "a":
            return dead_air_audit(hash_=h, path=str(path), video_id=kw["video_id"])
        if kind == "b":
            return dead_air_audit(hash_=h, path=str(path), video_id=kw["video_id"], issue_type="visual_stagnation", repair_kind="reframe_or_zoom")
        if kind == "quiet":
            return report([w(0, 2000, "low"), w(2000, 4000, "low"), w(4000, 6000, "low"), w(6000, 8000, "low")], [], hash_=h, path=str(path))
        mtype = kind.split(":", 1)[1]
        if mtype in world.limit_on:
            raise BudgetExceeded("model-call limit of 10 reached")
        evaluator = EVAL.model_copy(update={"cold_viewer_prompt_version": "cold-viewer-v1"}) if mtype in world.changed_evaluator else EVAL
        return report([w(0, 2000, "low"), w(2000, 4000, "low"), w(4000, 6400, "low")], [], hash_=h, path=str(path), evaluator=evaluator, version=kw["version_id"])

    def fake_render(plan: EditPlan, manifest: Any, video: Path, data_dir: Path) -> dict[str, Any]:
        mtype = plans[plan.model_dump_json()]
        world.renders.append(mtype)
        if mtype in world.render_fail:
            raise RuntimeError("ffmpeg exited 1")
        out = renders / f"render-{len(world.renders)}.mp4"
        out.write_bytes(f"render {len(world.renders)} {mtype}".encode())
        kinds[str(out)] = f"variant:{mtype}"
        return {"path": str(out), "artifact_hash": sha256_file(out), "duration_ms": 6400}

    def fake_verify(mutation_type: str, description: str, original_path: Path, candidate_path: Path, *a: Any, **k: Any) -> ChangeVerification:
        return ChangeVerification(intended=description, verified=mutation_type not in world.unverified, checks=["scripted render check"])

    def fake_compare(*, provider: Any, original: AuditReport, candidate: AuditReport, finding: AuditFinding, candidate_plan: EditPlan,
                     protected_items: list[str]) -> AuditComparison:
        mtype = plans[candidate_plan.model_dump_json()]
        world.compares.append(mtype)
        world.protected_seen.append(list(protected_items))
        updates = {"protected_requested": list(protected_items),
                   "protected_checks": [{"item": item, "kind": "rule", "status": "respected", "evidence": "scripted check"} for item in protected_items],
                   **world.comparison_updates.get(mtype, {})}
        return comparison(world.outcomes.get(mtype, "tie"), **updates)

    monkeypatch.setattr(C, "validate_video_path", lambda p: None)
    monkeypatch.setattr(C, "source_manifest", lambda *a, **k: None)
    monkeypatch.setattr(C, "extract_genome", lambda path, *a, **k: make_genome(sha256_file(path), ENDING))
    monkeypatch.setattr(C, "inspect_media", lambda path: SimpleNamespace(duration_ms=8000, width=180, height=320))
    monkeypatch.setattr(C, "extract_signals", lambda path, *a: signals() if kinds.get(str(path)) == "quiet" else signals(silent_from=6300, static_from=6000))
    monkeypatch.setattr(C, "build_candidates", fake_candidates)
    monkeypatch.setattr(C, "run_audit", fake_audit)
    monkeypatch.setattr(C, "render_candidate", fake_render)
    monkeypatch.setattr(C, "verify_change", fake_verify)
    monkeypatch.setattr(C, "compare_versions", fake_compare)
    return videos


def bundle() -> ProviderBundle:
    return ProviderBundle(probe=FakeProvider(), planner=FakeProvider())  # type: ignore[arg-type]


def test_causal_loop_end_to_end_and_policy_transfer_to_another_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world = World(outcomes={"TRIM_PAUSE": "tie", "PUNCH_IN": "improvement"})
    videos = install(monkeypatch, tmp_path / "w", world)
    data = tmp_path / "data"
    events: list[tuple[str, str]] = []
    constraints = [f"constraint {i}" for i in range(9)]
    run = C.run_causal(C.CausalConfig(video_id="video-a", video_path=str(videos["a"]), constraints=constraints), bundle(), data,
                       on_stage=lambda st, m, d: events.append((st, m)))

    assert run.status == "completed" and run.audit_source == "fresh" and run.plan is not None and run.dossier is not None and run.plan_inputs is not None
    plan = run.plan
    assert [(h.id, h.cause_type, h.status) for h in plan.hypotheses] == [("H1", "dead_air_after_payoff", "supported"), ("H2", "static_visual", "supported"),
                                                                       ("H3", "delayed_payoff", "contradicted"), ("H4", "missing_context", "contradicted")]
    assert [(x.id, x.mutation_type, x.hypothesis_id) for x in plan.experiments] == [("X1", "TRIM_PAUSE", "H1"), ("X2", "PUNCH_IN", "H2")]
    assert plan.chosen == ["X1", "X2"] and world.renders == ["TRIM_PAUSE", "PUNCH_IN"] and world.compares == ["TRIM_PAUSE", "PUNCH_IN"]
    assert [(a.experiment_id, a.verdict) for a in run.arms] == [("X1", "neutral"), ("X2", "win")]
    assert run.conclusions[0] == ("The results favour H2 (static_visual) over H1 (dead_air_after_payoff): PUNCH_IN alone improved the video, "
                                  "while TRIM_PAUSE alone was neutral.")
    assert run.hypothesis_results["H1"].startswith("not supported") and run.hypothesis_results["H2"].startswith("supported")
    assert run.hypothesis_results["H3"] == "not tested: contradicted by e1"
    assert world.protected_seen == [["the payoff sentence", *constraints]] * 2, "every protected item and every constraint reaches the check"
    axes = run.arms[1].axes
    assert axes is not None and axes.continue_watching_preference == 0.75 and axes.continue_watching_agreement.startswith("whole video") and axes.target_resolved == "yes"

    # blind review: the variant reviewer receives the rendered file and neutral labels, nothing about the experiment
    assert all(set(call) <= {"video_id", "video_path", "version_id", "provider", "data_dir", "on_stage"} for call in world.audit_calls)
    variant_calls = world.audit_calls[1:]
    assert len(variant_calls) == 2 and all(re.fullmatch(r"v[12]-[0-9a-f]{8}", call["version_id"]) for call in variant_calls)
    leaked = json.dumps([{k: str(v) for k, v in call.items()} for call in variant_calls]).lower()
    assert not any(word in leaked for word in ("trim", "punch", "dead_air", "static_visual", "hypothes", "keep an unfamiliar", "constraint"))
    assert run.arms[0].review_inputs["video_sha256"] == run.arms[0].render_hash and "hypotheses" in run.arms[0].review_inputs["withheld"]

    # every transition is its own stage; each experiment nests its render, check, blind audit, comparison and verdict
    top = [s.key for s in run.workflow_stages if s.parent_id is None]
    assert top == ["ingest", "analyze_original", "build_genome", "evidence_dossier", "hypotheses", "policy_lookup", "rank_experiments", "choose_experiment",
                   "experiment", "experiment", "conclude", "update_policy", "final_selection"]
    for exp in [s for s in run.workflow_stages if s.key == "experiment"]:
        assert [s.key for s in run.workflow_stages if s.parent_id == exp.id] == ["render_variant", "verify", "blind_candidate_audit", "pairwise_eval", "decision"]
    assert all(s.status == "completed" for s in run.workflow_stages)

    policy = P.load_conditional_policy(P.policy_path(data))
    assert policy.version == 1 and sorted((r.mutation_type, r.cause_type, r.outcome) for r in policy.records) == [
        ("PUNCH_IN", "static_visual", "win"), ("TRIM_PAUSE", "dead_air_after_payoff", "neutral")]
    punch_record = next(r for r in policy.records if r.mutation_type == "PUNCH_IN")
    assert (punch_record.artifact_hash, punch_record.variant_artifact_hash, punch_record.plan_hash) == (run.artifact_hash, run.arms[1].render_hash, run.arms[1].plan_hash)
    assert punch_record.mocked is True and punch_record.evaluator["cold_viewer_prompt_version"] == A.COLD_VIEWER_PROMPT_VERSION and punch_record.comparison_outcome == "improvement"
    assert all(r.conditions == run.dossier.conditions and r.video_id == "video-a" and r.run_id == run.id for r in policy.records)
    assert any("PUNCH_IN for static_visual: 1 win" in line and "(on 1 source file); 1 from scripted test runs" in line for line in run.policy_after)
    stored = C.load_causal(data, run.id)
    assert stored is not None and stored.status == "completed" and len(stored.workflow_stages) == len(run.workflow_stages)
    assert run.mocked_stages and run.budget["renders_used"] == 2 and events[-1][0] == "DONE" and run.runtime["runner"] == "directorloop.runtime.causal.run_causal"

    # the same file again: its own earlier outcomes are memory of this file, never counted as transfer
    again = C.run_causal(C.CausalConfig(video_id="video-a-copy", video_path=str(videos["a"]), plan_only=True), bundle(), data)
    assert again.plan is not None and all(x.prior.records == 0 and x.already_tested_here for x in again.plan.experiments)
    assert again.plan.policy_effect.summary.startswith("the policy did not change the ranking")

    # a different file under matching conditions: the learned outcomes change the first experiment, without rendering anything
    world.audit_calls.clear()
    world.renders.clear()
    transfer = C.run_causal(C.CausalConfig(video_id="video-b", video_path=str(videos["b"]), arms=1, plan_only=True), bundle(), data)
    tp = transfer.plan
    assert tp is not None and transfer.dossier is not None and transfer.status == "completed" and transfer.artifact_hash != run.artifact_hash
    assert [(h.cause_type, h.strength) for h in tp.hypotheses[:2]] == [("dead_air_after_payoff", "moderate"), ("static_visual", "moderate")]
    by_type = {x.mutation_type: x for x in tp.experiments}
    punch, trim = by_type["PUNCH_IN"], by_type["TRIM_PAUSE"]
    assert (punch.rank, punch.rank_without_policy, trim.rank, trim.rank_without_policy) == (1, 2, 2, 1)
    assert punch.prior.records == 1 and punch.prior.score == 1.0 and punch.prior.matches[0]["artifact_hash"] == run.artifact_hash and punch.prior.matches[0]["weight"] == 1.0
    effect = tp.policy_effect
    assert effect.first_choice_changed and effect.selection_changed and effect.order_changed
    assert (effect.first_choice_with_policy, effect.first_choice_without_policy) == (punch.intervention_id, trim.intervention_id)
    assert sorted(effect.records_used) == sorted(r.id for r in policy.records) and tp.chosen == [punch.id] and tp.chosen_without_policy == [trim.id]
    assert world.renders == [] and len(world.audit_calls) == 1 and transfer.policy_records_added == [] and transfer.budget["renders_used"] == 0
    assert transfer.plan_inputs is not None and transfer.plan_inputs.policy_sha256 == P.policy_sha256(policy)

    # exact replay from the frozen inputs, with the snapshot and with no policy at all
    replayed = C.replay_plan(transfer)
    assert [(x.intervention_id, x.rank, x.priority) for x in replayed.experiments] == [(x.intervention_id, x.rank, x.priority) for x in tp.experiments]
    no_policy = C.replay_plan(transfer, use_policy=False)
    assert [x.intervention_id for x in no_policy.experiments] == [trim.intervention_id, punch.intervention_id]
    assert no_policy.policy_effect.summary.startswith("the policy did not change the ranking")
    assert P.prior_support(policy, "static_visual", "PUNCH_IN", transfer.dossier.conditions, target_artifact_hash=transfer.artifact_hash).excluded[0]["reason"] == \
        "scripted test run", "a real run never learns from scripted records"


def test_inconclusive_and_untested_experiments_never_reach_the_policy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world = World(outcomes={"TRIM_PAUSE": "improvement", "PUNCH_IN": "improvement"}, render_fail={"PUNCH_IN"}, unverified={"TRIM_PAUSE"})
    videos = install(monkeypatch, tmp_path / "w", world)
    run = C.run_causal(C.CausalConfig(video_id="video-a", video_path=str(videos["a"])), bundle(), tmp_path / "data")
    assert [(a.mutation_type, a.verdict) for a in run.arms] == [("TRIM_PAUSE", "incomplete"), ("PUNCH_IN", "incomplete")]
    assert "does not show the intended change" in run.arms[0].verdict_reason and "render failed" in run.arms[1].verdict_reason
    assert len(world.audit_calls) == 1 and world.compares == [], "an unverified or unrendered variant is never reviewed"
    assert not P.policy_path(tmp_path / "data").exists() and run.policy_records_added == [] and run.conclusions[0] == "No experiment was completed."
    assert [s.status for s in run.workflow_stages if s.key == "render_variant"] == ["completed", "failed"]
    assert [s.status for s in run.workflow_stages if s.key == "experiment"] == ["incomplete", "incomplete"]

    world2 = World(outcomes={"TRIM_PAUSE": "improvement"}, changed_evaluator={"TRIM_PAUSE"})
    videos2 = install(monkeypatch, tmp_path / "e", world2)
    run2 = C.run_causal(C.CausalConfig(video_id="video-a", video_path=str(videos2["a"]), arms=1), bundle(), tmp_path / "e" / "data")
    arm = run2.arms[0]
    assert arm.verdict == "incomplete" and "different evaluation settings" in arm.verdict_reason
    assert arm.evaluator_differences == ["cold_viewer_prompt_version: cold-viewer-v2 for the original, cold-viewer-v1 for the variant"]
    assert world2.compares == [] and run2.policy_records_added == []

    world3 = World(outcomes={"TRIM_PAUSE": "unstable", "PUNCH_IN": "improvement"}, comparison_updates={"PUNCH_IN": {"protected_unchecked": ["constraint 0"]}})
    videos3 = install(monkeypatch, tmp_path / "u", world3)
    run3 = C.run_causal(C.CausalConfig(video_id="video-a", video_path=str(videos3["a"]), constraints=["constraint 0"]), bundle(), tmp_path / "u" / "data")
    assert [(a.mutation_type, a.verdict) for a in run3.arms] == [("TRIM_PAUSE", "inconclusive"), ("PUNCH_IN", "inconclusive")]
    assert run3.policy_records_added == [] and not P.policy_path(tmp_path / "u" / "data").exists(), "uncertainty is never learned as a neutral or winning outcome"
    assert run3.conclusions[0] == "No experiment gave a conclusive result; the cause of the weakness remains undetermined."
    assert run3.hypothesis_results["H1"].startswith("unresolved") and run3.arms[1].axes is not None and run3.arms[1].axes.protected_unchecked == ["constraint 0"]


def test_gate_verdicts_reach_the_policy_and_limits_stop_cleanly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world = World(outcomes={"TRIM_PAUSE": "regression", "PUNCH_IN": "mixed"})
    videos = install(monkeypatch, tmp_path / "w", world)
    data = tmp_path / "data"
    run = C.run_causal(C.CausalConfig(video_id="video-a", video_path=str(videos["a"])), bundle(), data)
    assert [(a.mutation_type, a.verdict) for a in run.arms] == [("TRIM_PAUSE", "loss"), ("PUNCH_IN", "rejected")]
    assert "protected content lost" in run.arms[1].verdict_reason and run.conclusions[0].startswith("No edit produced an improvement")
    assert sorted(r.outcome for r in P.load_conditional_policy(P.policy_path(data)).records) == ["loss", "rejected"]

    world2 = World(outcomes={"TRIM_PAUSE": "improvement"}, limit_on={"PUNCH_IN"})
    videos2 = install(monkeypatch, tmp_path / "b", world2)
    run2 = C.run_causal(C.CausalConfig(video_id="video-a", video_path=str(videos2["a"])), bundle(), tmp_path / "b" / "data")
    assert run2.status == "completed" and run2.stop_reason == "budget reached: model-call limit of 10 reached"
    assert [(a.mutation_type, a.verdict) for a in run2.arms] == [("TRIM_PAUSE", "win"), ("PUNCH_IN", "incomplete")]
    assert [r.mutation_type for r in P.load_conditional_policy(P.policy_path(tmp_path / "b" / "data")).records] == ["TRIM_PAUSE"]
    assert run2.conclusions[0].startswith("One conclusive experiment: TRIM_PAUSE")
    assert [s.status for s in run2.workflow_stages if s.key == "blind_candidate_audit"] == ["completed", "failed"]

    class Expired(CallBudget):
        def check_deadline(self) -> None:
            raise BudgetExceeded("deadline of 60 s reached after 61 s")

    world3 = World()
    videos3 = install(monkeypatch, tmp_path / "d", world3)
    monkeypatch.setattr(C, "CallBudget", Expired)
    run3 = C.run_causal(C.CausalConfig(video_id="video-a", video_path=str(videos3["a"])), bundle(), tmp_path / "d" / "data")
    assert run3.stop_reason == "budget reached: deadline of 60 s reached after 61 s" and world3.renders == [], "the deadline is checked before every render"


def test_cancellation_is_persisted_and_re_raised(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world = World()
    videos = install(monkeypatch, tmp_path / "w", world)
    data = tmp_path / "data"

    def operator(stage: str, message: str, payload: Any) -> None:
        if stage == "AUDITING":
            raise JobCanceled("canceled during AUDITING")

    with pytest.raises(JobCanceled):
        C.run_causal(C.CausalConfig(video_id="video-a", video_path=str(videos["a"])), bundle(), data, on_stage=operator, run_id="causal_cancel_test")
    stored = C.load_causal(data, "causal_cancel_test")
    assert stored is not None and stored.status == "cancelled" and stored.stop_reason == "the run was cancelled by the operator"
    assert [(s.key, s.status) for s in stored.workflow_stages if s.parent_id is None][:2] == [("ingest", "completed"), ("analyze_original", "cancelled")]
    assert world.renders == []


def test_recorded_audits_must_match_the_file_and_the_evaluator_and_a_video_without_a_symptom_is_not_edited(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    world = World()
    videos = install(monkeypatch, tmp_path / "w", world)
    data = tmp_path / "data"

    def store(a: AuditReport, audit_id: str) -> None:
        a.id = audit_id
        (data / "audit" / audit_id).mkdir(parents=True)
        (data / "audit" / audit_id / "audit.json").write_text(a.model_dump_json())

    store(dead_air_audit(hash_="f" * 64, path=str(videos["a"])), "audit_other_file")
    refused = C.run_causal(C.CausalConfig(video_id="video-a", video_path=str(videos["a"]), audit_id="audit_other_file"), bundle(), data)
    assert refused.status == "failed" and "belongs to another file" in refused.stop_reason and world.audit_calls == []

    store(dead_air_audit(hash_=sha256_file(videos["a"]), path=str(videos["a"]), evaluator=EVAL.model_copy(update={"model": "older-model"})), "audit_old_model")
    stale = C.run_causal(C.CausalConfig(video_id="video-a", video_path=str(videos["a"]), audit_id="audit_old_model"), bundle(), data)
    assert stale.status == "failed" and "model: older-model recorded, scripted now" in stale.stop_reason and world.renders == [], "refused before any render"

    store(dead_air_audit(hash_=sha256_file(videos["a"]), path=str(videos["a"])), "audit_recorded_a")
    planned = C.run_causal(C.CausalConfig(video_id="video-a", video_path=str(videos["a"]), audit_id="audit_recorded_a", plan_only=True), bundle(), data)
    assert planned.audit_source == "recorded" and planned.audit_id == "audit_recorded_a" and world.audit_calls == []
    assert planned.plan is not None and planned.plan.chosen == ["X1", "X2"] and world.renders == [] and planned.stop_reason == "plan only: no render requested"

    quiet = C.run_causal(C.CausalConfig(video_id="video-q", video_path=str(videos["quiet"])), bundle(), data)
    assert quiet.stop_reason.startswith("no symptom") and quiet.plan is None and world.renders == []
