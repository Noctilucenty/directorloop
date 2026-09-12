"""Decision rule over fitness components, and payoff timing derived from the plan."""

from __future__ import annotations

from directorloop.creative.experiment import classify_arm, decide
from directorloop.creative.fitness import plan_timings
from directorloop.creative.mutate import identity_plan
from directorloop.domain import MoveSegment, apply_ops
from directorloop.domain.creative import BeatRole, CreativeFitness, EvidenceClass, FitnessComponent
from tests.unit.test_creative_mutations import make_genome


def fit(arm: str, *, msg: float = 3, topic: float = 1.0, full: float | None = None, hook: float | None = None, gates: bool = True, valid: int = 4) -> CreativeFitness:
    comps = [
        FitnessComponent(name="message_comprehension", value=msg, evidence=EvidenceClass.MODEL_EVAL),
        FitnessComponent(name="hook_topic_comprehension", value=topic, evidence=EvidenceClass.MODEL_EVAL),
    ]
    if full is not None:
        comps.append(FitnessComponent(name="model_full_preference_vs_control", value=full, evidence=EvidenceClass.MODEL_EVAL, valid=valid))
    comps.append(FitnessComponent(name="model_hook_preference_vs_control", value=hook, evidence=EvidenceClass.MODEL_EVAL))
    return CreativeFitness(arm_id=arm, components=comps, hard_gates_passed=gates, gate_notes=[] if gates else ["duration_matches_plan: off"])


def test_classify_arm_rules() -> None:
    control = fit("control")
    assert classify_arm(control, fit("A", full=0.875, hook=0.75))[0] == "win"
    assert classify_arm(control, fit("A", full=1.0, hook=0.25))[0] == "neutral", "a full-video win that makes the opening worse is not a clean win"
    outcome, reason = classify_arm(control, fit("A", full=0.0, hook=0.875))
    assert outcome == "loss" and "tradeoff" in reason, "better opening but worse full video is a recorded tradeoff loss"
    assert classify_arm(control, fit("A", full=0.5, hook=None))[0] == "neutral"
    assert classify_arm(control, fit("A", msg=2, full=1.0))[0] == "rejected", "losing the core message is never a win"
    assert classify_arm(control, fit("A", topic=0.33, full=1.0))[0] == "rejected"
    assert classify_arm(control, fit("A", full=1.0, gates=False))[0] == "rejected"
    assert classify_arm(control, fit("A", full=1.0, valid=2))[0] == "neutral", "fewer than 3 valid preference calls is not evidence"


def test_decide_picks_best_win_and_reports_every_arm() -> None:
    control = fit("control")
    d = decide(control, {"A": fit("A", full=0.0, hook=0.9), "B": fit("B", full=0.875, hook=None), "C": fit("C", full=1.0, hook=0.5)})
    assert d.outcome == "winner" and d.winner_arm_id == "C"
    assert d.per_arm == {"A": "loss", "B": "win", "C": "win"}
    d2 = decide(control, {"A": fit("A", full=0.5), "B": fit("B", full=0.25)})
    assert d2.outcome == "no_clear_winner" and d2.winner_arm_id is None and d2.per_arm["B"] == "loss"
    d3 = decide(control, {"A": fit("A", gates=False, full=1.0)})
    assert d3.outcome == "all_rejected"


def test_plan_timings_follow_the_edit() -> None:
    g = make_genome("x" * 64, [BeatRole.HOOK, BeatRole.CONTEXT, BeatRole.MECHANISM, BeatRole.PAYOFF], beat_ms=2000, static=None)
    plan = identity_plan(g)
    t0 = plan_timings(plan, g)
    assert t0 == {"payoff_ms": 6000, "context_before_payoff_ms": 2000, "duration_ms": 8000}
    moved = apply_ops(plan, [MoveSegment(segment_id="beat_03", after_segment_id="beat_00")])
    t1 = plan_timings(moved, g)
    assert t1["payoff_ms"] == 2000 and t1["context_before_payoff_ms"] == 0 and t1["duration_ms"] == 8000
