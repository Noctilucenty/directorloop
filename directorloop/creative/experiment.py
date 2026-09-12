"""Creative experiment runner: one generation of controlled variants, evaluated and learned from.

directorloop.experiment
  analyze_creative -> identify_weak_region -> retrieve_reference_patterns -> retrieve_policy ->
  formulate_hypotheses -> design_experiment -> freeze_video_suite -> create_variant_* (parallel) ->
  evaluate_* (parallel) -> compare_variants -> update_policy
Every arm, including losses and rejected arms, is persisted and recorded in the policy.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import weave

from ..domain.creative import (
    CreativeExperiment,
    CreativeFitness,
    ExperimentArm,
    ExperimentDecision,
    HypothesisRanking,
    ReferenceCorpus,
    RetentionSeries,
)
from ..domain.edit_plan import EditPlan, apply_ops
from ..domain.ids import new_id, utc_now_iso
from ..media import render_plan
from ..observability.weave_ops import current_call_ref, set_display_name, traced
from ..providers.registry import ProviderBundle
from .design import ExperimentDesign, design_experiment
from .fitness import build_video_suite, evaluate_arm, load_or_run_leakage, prepare_arm_media
from .genome import extract_genome, genome_summary
from .investigate import Investigation, investigate
from .mutate import VIDEO_ASSET_ID, experiment_brief, identity_plan, source_manifest
from .patterns import MUTATION_PATTERN, pattern_lift
from .policy import CreativePolicyStore, StrategyEvidence

StageCallback = Callable[[str, str, dict | None], None]
LABELS = ["A", "B", "C", "D", "E", "F", "G"]

WIN_THRESHOLD = 0.75
LOSS_THRESHOLD = 0.25


def _emit(cb: StageCallback | None, stage: str, message: str, data: dict | None = None) -> None:
    if cb:
        cb(stage, message, data)


def classify_arm(control: CreativeFitness, arm: CreativeFitness) -> tuple[str, str]:
    """win | loss | neutral | rejected, with a one-line reason. Pure function of the fitness components."""
    if not arm.hard_gates_passed:
        return "rejected", "hard gate failed: " + "; ".join(arm.gate_notes[:2])
    c_msg, a_msg = control.value("message_comprehension"), arm.value("message_comprehension")
    if c_msg is not None and a_msg is not None and a_msg < c_msg:
        return "rejected", f"core message comprehension dropped ({a_msg:.0f} vs {c_msg:.0f} questions)"
    c_topic, a_topic = control.value("hook_topic_comprehension"), arm.value("hook_topic_comprehension")
    if c_topic is not None and a_topic is not None and a_topic < c_topic - 0.34:
        return "rejected", f"opening no longer communicates the topic ({a_topic:.2f} vs {c_topic:.2f})"
    full = arm.get("model_full_preference_vs_control")
    hook = arm.value("model_hook_preference_vs_control")
    if full is None or full.value is None or (full.valid or 0) < 3:
        return "neutral", "insufficient preference evidence"
    if full.value >= WIN_THRESHOLD and (hook is None or hook >= 0.5):
        return "win", f"full-video model preference {full.value:.2f}" + (f", opening {hook:.2f}" if hook is not None else "")
    if full.value <= LOSS_THRESHOLD:
        tradeoff = f"; tradeoff: the opening alone was preferred ({hook:.2f})" if hook is not None and hook >= WIN_THRESHOLD else ""
        return "loss", f"full-video model preference {full.value:.2f}{tradeoff}"
    return "neutral", f"full-video model preference {full.value:.2f}" + (f", opening {hook:.2f}" if hook is not None else "")


@traced("compare_variants", kind="tool")
def decide(control: CreativeFitness, arms: dict[str, CreativeFitness]) -> ExperimentDecision:
    per_arm: dict[str, str] = {}
    reasons: dict[str, str] = {}
    for arm_id, fit in arms.items():
        per_arm[arm_id], reasons[arm_id] = classify_arm(control, fit)
    wins = [a for a, o in per_arm.items() if o == "win"]
    if wins:
        best = max(wins, key=lambda a: (arms[a].value("model_full_preference_vs_control") or 0, arms[a].value("model_hook_preference_vs_control") or 0))
        return ExperimentDecision(outcome="winner", winner_arm_id=best, primary_metric="model_full_preference_vs_control", per_arm=per_arm,
                                  reason=f"{best} wins: {reasons[best]}. " + " ".join(f"{a}: {per_arm[a]} ({reasons[a]})." for a in per_arm if a != best))
    if per_arm and all(o == "rejected" for o in per_arm.values()):
        outcome = "all_rejected"
    elif per_arm and all(reasons[a] == "insufficient preference evidence" for a in per_arm if per_arm[a] == "neutral") and "loss" not in per_arm.values():
        outcome = "insufficient_evidence"
    else:
        outcome = "no_clear_winner"
    return ExperimentDecision(outcome=outcome, winner_arm_id=None, primary_metric="model_full_preference_vs_control", per_arm=per_arm,
                              reason="no arm met the win rule; keeping the control. " + " ".join(f"{a}: {per_arm[a]} ({reasons[a]})." for a in per_arm))


@traced("retrieve_reference_patterns", kind="search")
def retrieve_reference_patterns(corpus: ReferenceCorpus | None, design: ExperimentDesign) -> list[dict[str, Any]]:
    if corpus is None:
        return []
    out = []
    for c in design.candidates:
        pid = MUTATION_PATTERN.get(c.mutation.type)
        obs = corpus.pattern(pid) if pid else None
        if obs is None:
            continue
        out.append({"mutation": c.mutation.type.value, "pattern": obs.pattern_id, "description": obs.description, "count": obs.count, "total": obs.total_comparable,
                    "top": f"{obs.top_group_count}/{obs.top_group_total}" if obs.top_group_total else None,
                    "bottom": f"{obs.bottom_group_count}/{obs.bottom_group_total}" if obs.bottom_group_total else None,
                    "metric": obs.performance_metric, "lift": pattern_lift(obs), "label": "REFERENCE PRIOR (descriptive)"})
    return out


@traced("retrieve_policy", kind="search")
def retrieve_policy(policy: CreativePolicyStore, design: ExperimentDesign, category: str) -> list[dict[str, Any]]:
    out = []
    for c in design.candidates:
        s = policy.lookup(c.mutation.type, category)
        out.append({"mutation": c.mutation.type.value, "strategy": s.id if s else None, "status": s.status if s else "no evidence yet",
                    "wins": s.wins if s else 0, "losses": s.losses if s else 0, "neutral": s.neutral if s else 0, "rejected": s.rejected if s else 0})
    return out


@traced("formulate_hypotheses", kind="tool")
def formulate_hypotheses(inv: Investigation) -> list[dict[str, Any]]:
    return [{"id": h.id, "family": h.family.value, "statement": h.statement, "region_ms": [h.region_start_ms, h.region_end_ms], "basis": h.region_basis,
             "confidence": h.detection_confidence, "evidence": [e.text for e in h.evidence], "counterevidence": [e.text for e in h.counterevidence],
             "mutations": [m.value for m in h.candidate_mutations], "changed_variable": h.changed_variable} for h in inv.hypotheses]


@traced("create_variant", kind="tool")
def create_variant(label: str, plan: EditPlan, manifest, video_path: Path, data_dir: Path) -> dict[str, Any]:  # noqa: ANN001
    set_display_name(f"create_variant_{label}" if label != "control" else "render_control")
    r = render_plan(plan, manifest, {VIDEO_ASSET_ID: video_path}, publish_dir=data_dir / "renders", work_dir=data_dir / "work")
    return {"label": label, "artifact_hash": r.artifact_hash, "path": str(r.path), "duration_ms": r.duration_ms, "render_ms": r.render_ms + r.verify_ms}


@traced("update_policy", kind="tool")
def update_policy(policy: CreativePolicyStore, experiment: CreativeExperiment, category: str, weave_url: str | None) -> list[dict[str, Any]]:
    changes = []
    decision = experiment.decision
    if decision is None:
        return changes
    control = next(a for a in experiment.arms if a.label == "control")
    for arm in experiment.arms:
        if arm.mutation is None or arm.fitness is None:
            continue
        outcome = decision.per_arm.get(arm.id, "neutral")
        full = arm.fitness.value("model_full_preference_vs_control")
        strat = policy.record_outcome(
            arm.mutation.type,
            category,
            StrategyEvidence(experiment_id=experiment.id, video_id=experiment.video_id, arm_id=arm.id, outcome=outcome, evidence_class="model_eval",
                             primary_metric=decision.primary_metric, control_value=None if control.fitness is None else 0.5, arm_value=full,
                             note=f"{arm.mutation.description[:120]}", weave_url=weave_url),
            family=next((h.family for h in experiment.hypotheses if h.id == arm.mutation.hypothesis_id), None),
        )
        change = policy.changes[-1]
        changes.append({"strategy": strat.id, "mutation": arm.mutation.type.value, "outcome": outcome, "before": change.before, "after": change.after, "policy_version": change.version})
    return changes


def _rankings_for(design: ExperimentDesign) -> list[HypothesisRanking]:
    return [c.ranking for c in design.candidates]


@traced("directorloop.experiment", kind="agent")
def run_creative_experiment(
    *,
    video_id: str,
    video_path: Path,
    providers: ProviderBundle,
    data_dir: Path,
    policy: CreativePolicyStore,
    corpus: ReferenceCorpus | None = None,
    retention: RetentionSeries | None = None,
    category: str = "educational_short",
    objective: str = "",
    policy_mode: str = "learned",
    max_arms: int = 3,
    trials: int = 3,
    record_policy: bool = True,
    generation: int = 1,
    parent_version_id: str = "v0",
    arm_filter: list[str] | None = None,
    on_stage: StageCallback | None = None,
) -> CreativeExperiment:
    if providers.probe is None or providers.planner is None:
        raise RuntimeError("a vision probe provider and a text planner are required")
    set_display_name(f"experiment_{video_id}_gen{generation}_{policy_mode}")
    started = time.monotonic()
    timings: dict[str, int] = {}
    creative_dir = data_dir / "creative"
    cache = creative_dir / "cache"
    exp_id = new_id("cexp")
    project_id = f"proj_{video_id}"

    with weave.attributes({"experiment_id": exp_id, "video_id": video_id, "policy_version": policy.version, "policy_mode": policy_mode, "generation": generation,
                           "corpus_version": corpus.version if corpus else None, "category": category}):
        t = time.monotonic()
        _emit(on_stage, "ANALYZING", "building the creative genome of the actual video (cached per file hash)")
        genome = extract_genome(video_path, providers.probe, creative_dir / "genomes", category=category, declared_objective=objective)
        timings["genome_ms"] = int((time.monotonic() - t) * 1000)
        _emit(on_stage, "ANALYZING", f"genome: {len(genome.beats)} beats, {len(genome.shots)} shots, hook {genome.hook.hook_type.value}", {"genome": genome_summary(genome)})

        t = time.monotonic()
        inv = investigate(genome, retention=retention, corpus=corpus, scope=category)
        hyp_view = formulate_hypotheses(inv)
        timings["investigate_ms"] = int((time.monotonic() - t) * 1000)
        _emit(on_stage, "DIAGNOSING", inv.weak_region.description if inv.weak_region else "no weak region found", {"weak_region": inv.weak_region.model_dump() if inv.weak_region else None, "hypotheses": hyp_view, "retention_note": inv.retention_note})

        manifest = source_manifest(project_id, video_path, genome.artifact_hash)
        brief = experiment_brief(project_id, genome, objective)
        control_plan = identity_plan(genome)
        t = time.monotonic()
        design = design_experiment(hypotheses=inv.hypotheses, genome=genome, plan=control_plan, manifest=manifest, brief=brief, video_path=video_path,
                                   parent_version_id=parent_version_id, policy=policy, corpus=corpus, policy_mode=policy_mode, max_arms=max_arms, category=category)
        refs = retrieve_reference_patterns(corpus, design)
        pol = retrieve_policy(policy, design, category)
        timings["design_ms"] = int((time.monotonic() - t) * 1000)
        chosen = design.selected
        if arm_filter is not None:
            chosen = [c for c in design.candidates if c.mutation.type.value in arm_filter]
        _emit(on_stage, "PLANNING", "ranked experiments: " + ", ".join(f"{c.mutation.type.value} {c.ranking.score:.3f}" for c in design.candidates),
              {"ranking": [r.model_dump() for r in _rankings_for(design)], "selected": [c.mutation.type.value for c in chosen], "reference_patterns": refs, "policy": pol, "skipped": design.skipped})

        t = time.monotonic()
        suite = build_video_suite(genome, providers.planner, cache, video_id)
        leak = load_or_run_leakage(suite, providers.probe, cache)
        timings["suite_ms"] = int((time.monotonic() - t) * 1000)
        _emit(on_stage, "PLANNING", f"frozen suite {suite.content_hash()[:12]} ({len(suite.questions)} questions); no-media control weak: {leak.weak_question_ids or 'none'}")

        arms: list[ExperimentArm] = [ExperimentArm(id=f"{exp_id}_control", label="control", version_id=parent_version_id)]
        plans: dict[str, EditPlan] = {"control": control_plan}
        for label, cand in zip(LABELS, chosen, strict=False):
            arms.append(ExperimentArm(id=f"{exp_id}_{label}", label=label, mutation=cand.mutation))
            plans[label] = apply_ops(control_plan, cand.mutation.ops)

        t = time.monotonic()
        _emit(on_stage, "RENDERING", f"rendering control + {len(arms) - 1} variants in parallel")
        with weave.ThreadPoolExecutor(max_workers=4) as ex:
            renders = list(ex.map(lambda a: create_variant(a.label, plans[a.label], manifest, video_path, data_dir), arms))
        timings["render_wall_ms"] = int((time.monotonic() - t) * 1000)
        for arm, r in zip(arms, renders, strict=True):
            arm.artifact_hash, arm.artifact_path, arm.duration_ms, arm.render_ms, arm.status = r["artifact_hash"], r["path"], r["duration_ms"], r["render_ms"], "rendered"
            if arm.version_id is None:
                arm.version_id = f"ver_{r['artifact_hash'][:12]}"
        _emit(on_stage, "RENDERING", f"rendered {len(arms)} arms in {timings['render_wall_ms']} ms", {"arms": [{"label": a.label, "path": a.artifact_path, "render_ms": a.render_ms} for a in arms]})

        t = time.monotonic()
        with weave.ThreadPoolExecutor(max_workers=4) as ex:
            medias = list(ex.map(lambda a: prepare_arm_media(a.label, Path(a.artifact_path or ""), a.duration_ms or 0, plans[a.label], cache), arms))
        media_by_label = {m.label: m for m in medias}
        timings["arm_media_ms"] = int((time.monotonic() - t) * 1000)

        _emit(on_stage, "EVALUATING", f"evaluating {len(arms)} arms with the same frozen suite ({trials} trials, one question per call, pairwise both orders)")
        t = time.monotonic()
        weak = set(leak.weak_question_ids)

        def eval_one(arm: ExperimentArm) -> tuple[str, CreativeFitness, dict[str, Any]]:
            is_control = arm.label == "control"
            fit, detail = evaluate_arm(label=arm.label, arm_id=arm.id, media=media_by_label[arm.label], control_media=None if is_control else media_by_label["control"],
                                       plan=plans[arm.label], control_plan=control_plan, genome=genome, manifest=manifest, brief=brief, suite=suite,
                                       weak_question_ids=weak, provider=providers.probe, trials=trials, render_ms=arm.render_ms)
            return arm.id, fit, detail

        with weave.ThreadPoolExecutor(max_workers=4) as ex:
            results = list(ex.map(eval_one, arms))
        timings["evaluate_wall_ms"] = int((time.monotonic() - t) * 1000)
        details: dict[str, Any] = {}
        model_calls = 0
        for arm_id, fit, detail in results:
            arm = next(a for a in arms if a.id == arm_id)
            arm.fitness, arm.status = fit, "evaluated"
            details[arm.label] = detail
            model_calls += int(detail.get("model_calls", 0))

        control_fit = arms[0].fitness
        assert control_fit is not None
        decision = decide(control_fit, {a.id: a.fitness for a in arms[1:] if a.fitness is not None})
        _emit(on_stage, "DECIDING", f"{decision.outcome.upper()}: {decision.reason}", {"decision": decision.model_dump(), "per_arm_detail": details})

        call_id, url = current_call_ref()
        experiment = CreativeExperiment(
            id=exp_id, project_id=project_id, video_id=video_id, generation=generation,
            question=("Which single change best addresses: " + "; ".join(h.family.value for h in inv.hypotheses[:3])) if inv.hypotheses else "no hypotheses",
            policy_mode=policy_mode, policy_version_before=policy.version, corpus_version=corpus.version if corpus else None, genome_id=genome.id,
            hypotheses=inv.hypotheses, ranking=_rankings_for(design), arms=arms, suite_id=suite.id, suite_hash=suite.content_hash(), decision=decision,
            human_test_plan=[{"test": "hook", "left": arms[0].id, "right": a.id} for a in arms[1:]] + [{"test": "full", "left": arms[0].id, "right": a.id} for a in arms[1:]],
            timings_ms=timings, model_calls=model_calls, weave_url=url, weave_call_id=call_id, created_at=utc_now_iso(), status="completed",
            notes=[inv.retention_note] + design.skipped,
        )
        if record_policy:
            _emit(on_stage, "LEARNING", "recording every arm (wins, losses, neutral, rejected) in the creative policy")
            experiment.policy_updates = update_policy(policy, experiment, category, url)
            experiment.policy_version_after = policy.version
        timings["total_ms"] = int((time.monotonic() - started) * 1000)
        experiment.timings_ms = timings
        out_dir = creative_dir / "experiments"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{exp_id}.json").write_text(experiment.model_dump_json(indent=2), encoding="utf-8")
        (out_dir / f"{exp_id}.detail.json").write_text(json.dumps({"reference_patterns": refs, "policy_view": pol, "hypotheses": hyp_view, "per_arm_detail": details, "genome": genome_summary(genome)}, indent=2, default=str), encoding="utf-8")
        _emit(on_stage, "DONE", f"experiment {exp_id} finished in {timings['total_ms']} ms", {"experiment_id": exp_id, "weave_url": url, "timings_ms": timings})
        return experiment
