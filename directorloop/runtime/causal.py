"""Causal experiment loop: explain a weakness before editing it, test the explanations with controlled edits, learn.

One launch:
  cold audit of the original (attention timeline)            -> the symptom
  evidence dossier and competing cause hypotheses             -> evidence for and against each hypothesis
  policy lookup and ranked single-variable experiments        -> or an explicit decision not to edit
  for each chosen experiment: real render, render check, blind audit of the variant (the reviewer receives only the rendered
  file and neutral labels), frozen-evaluator check, comparison against the same original in both presentation orders with
  every protected item and constraint, regression gate, verdict
  conclusions about the hypotheses                            -> which explanation the results favour, if any
  conditional policy update                                   -> conclusive outcomes stored with their conditions and provenance

Every stage is a workflow stage and a Weave call; model calls stay nested under them. The planning inputs (dossier,
executable candidates, base plan, policy snapshot) are frozen in the run record, so the ranking can be replayed exactly.
Verdicts are model judgments of offline variants, not audience data; a win means "improvement observed after the edit",
not proof of a cause.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from ..audit.attention import COLD_VIEWER_PROMPT_VERSION, AttentionConfig
from ..audit.models import AuditComparison, AuditFinding, AuditReport, EvaluatorRecord
from ..audit.repairs import RepairCandidate, build_candidates
from ..audit.review import run_audit
from ..audit.revise import compare_versions, render_candidate, verify_change
from ..config import get_settings
from ..creative.genome import extract_genome
from ..creative.mutate import source_manifest
from ..creative.signals import extract_signals
from ..domain.edit_plan import EditOp, EditPlan, apply_ops
from ..domain.ids import new_id, sha256_file, utc_now_iso
from ..jobs.worker import JobCanceled, safe_failure, safe_limit_reason
from ..media.probe import inspect_media
from ..observability.weave_ops import current_call_ref, set_display_name, traced
from ..observability.workflow import WorkflowStage, attach_workflow, workflow_session, workflow_stage
from ..planning.causal import (
    ConditionalPolicy,
    Dossier,
    Experiment,
    ExperimentPlan,
    build_dossier,
    competing_hypotheses,
    load_conditional_policy,
    new_policy_record,
    plan_experiments,
    policy_lock,
    policy_path,
    policy_sha256,
    policy_statements,
    save_conditional_policy,
)
from ..providers.registry import ProviderBundle
from .budget import BudgetedProvider, BudgetExceeded, CallBudget
from .director import mocked_stages, runtime_record, validate_video_path

CAUSAL_VERSION = "causal-v1"
Verdict = Literal["win", "neutral", "loss", "rejected", "inconclusive", "incomplete"]
LEARNED_VERDICTS = ("win", "neutral", "loss", "rejected")  # inconclusive and incomplete arms never enter the policy


class CausalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_id: str = Field(pattern=r"^[a-z0-9_\-]{2,80}$")
    video_path: str
    objective: str = Field(default="Keep an unfamiliar viewer watching and understanding the video without losing what already works.", max_length=500)
    category: str = "educational_short"
    constraints: list[str] = Field(default_factory=list, max_length=12)  # every one is sent to the protected-content check
    arms: int = Field(default=2, ge=0, le=3)  # experiment renders this run may spend
    plan_only: bool = False  # stop after the ranked plan: no render and no variant review
    audit_id: str | None = None  # reuse a stored audit of this exact file under the current evaluator settings (recorded as such)
    max_model_calls: int | None = Field(default=120, ge=10, le=1000)
    deadline_s: int | None = Field(default=1800, ge=60, le=7200)


class CandidateRecord(BaseModel):
    """One executable edit offered to the planner, stored so the plan can be rebuilt from the base plan."""

    model_config = ConfigDict(extra="forbid")

    key: str
    mutation_type: str
    description: str
    ops: list[dict[str, Any]]
    secondary_changes: list[str] = Field(default_factory=list)
    plan_hash: str


class PlanInputs(BaseModel):
    """Everything the deterministic planner read, frozen at planning time."""

    model_config = ConfigDict(extra="forbid")

    base_plan: EditPlan
    candidates: list[CandidateRecord]
    policy_sha256: str
    policy_snapshot: ConditionalPolicy
    target_artifact_hash: str
    arms: int
    include_mocked: bool


class ArmAxes(BaseModel):
    """Evaluation axes kept apart: preference, predicted attention, comprehension signals, payoff timing, regressions."""

    model_config = ConfigDict(extra="forbid")

    continue_watching_preference: float | None = None  # whole-video pairwise share for the variant, both presentation orders
    continue_watching_agreement: str = ""
    target_moment_preference: float | None = None
    target_moment_agreement: str = ""
    target_resolved: str = "unclear"
    attention_target_before: str | None = None
    attention_target_after: str | None = None
    attention_direction: str = "insufficient_evidence"
    elevated_attention_ms_before: int = 0
    elevated_attention_ms_after: int = 0
    confused_readings_before: int = 0
    confused_readings_after: int = 0
    comprehension_findings_before: int = 0
    comprehension_findings_after: int = 0
    payoff_delivered_before_ms: int | None = None  # original time
    payoff_delivered_after_ms: int | None = None  # variant time
    protected_checked: int = 0
    protected_unchecked: list[str] = Field(default_factory=list)
    regressions: list[str] = Field(default_factory=list)


class ArmResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experiment_id: str
    hypothesis_id: str
    cause_type: str
    also_tests: list[str] = Field(default_factory=list)
    intervention_id: str = ""
    plan_hash: str = ""
    mutation_type: str
    edit_key: str
    description: str
    changes: list[str] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)
    side_effects: list[str] = Field(default_factory=list)
    render_hash: str | None = None
    render_path: str | None = None
    duration_ms: int | None = None
    verified: bool | None = None
    checks: list[str] = Field(default_factory=list)
    candidate_audit_id: str | None = None
    review_inputs: dict[str, str] = Field(default_factory=dict)  # exactly what the blind reviewer was given
    evaluator_differences: list[str] = Field(default_factory=list)
    protected_items: list[str] = Field(default_factory=list)
    comparison: AuditComparison | None = None
    axes: ArmAxes | None = None
    verdict: Verdict = "incomplete"
    verdict_reason: str = ""
    policy_record_id: str | None = None
    evaluation_complete: bool | None = None  # unknown on historical runs, never inferred from the keep/reject decision
    policy_eligible: bool | None = None
    learning_blockers: list[str] = Field(default_factory=list)
    model_calls: int = 0
    timings_ms: dict[str, int] = Field(default_factory=dict)


class CausalRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    config: CausalConfig
    version: str = CAUSAL_VERSION
    status: Literal["running", "completed", "failed", "cancelled"] = "running"
    created_at: str
    ended_at: str | None = None
    audit_id: str | None = None
    audit_source: Literal["fresh", "recorded"] | None = None
    artifact_hash: str = ""
    dossier: Dossier | None = None
    plan_inputs: PlanInputs | None = None
    plan: ExperimentPlan | None = None
    arms: list[ArmResult] = Field(default_factory=list)
    hypothesis_results: dict[str, str] = Field(default_factory=dict)
    conclusions: list[str] = Field(default_factory=list)
    policy_before: list[str] = Field(default_factory=list)
    policy_after: list[str] = Field(default_factory=list)
    policy_records_added: list[str] = Field(default_factory=list)
    budget: dict[str, Any] = Field(default_factory=dict)
    runtime: dict[str, Any] = Field(default_factory=dict)
    mocked_stages: list[str] = Field(default_factory=list)
    stop_reason: str = ""
    error: str | None = None
    launched_via: str = "python"
    weave_url: str | None = None
    weave_call_id: str | None = None
    timings_ms: dict[str, int] = Field(default_factory=dict)
    workflow_stages: list[WorkflowStage] = Field(default_factory=list)


def causal_dir(data_dir: Path) -> Path:
    return data_dir / "causal"


def save_causal(run: CausalRun, data_dir: Path) -> None:
    d = causal_dir(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f"{run.id}.json.tmp"
    tmp.write_text(run.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(d / f"{run.id}.json")


def load_causal(data_dir: Path, run_id: str) -> CausalRun | None:
    f = causal_dir(data_dir) / f"{run_id}.json"
    return CausalRun.model_validate_json(f.read_text(encoding="utf-8")) if f.exists() else None


# ----------------------------------------------------------------------------- deterministic gates


EVALUATOR_FIELDS = ("cold_viewer_prompt_version", "provider", "model", "reasoning_effort", "coarse_window_ms", "precision_enabled", "precision_window_ms",
                    "precision_lead_ms", "max_precision_regions", "max_precision_windows", "frame_sampling", "asr_language")


@traced("frozen_evaluator_check", kind="tool", display=lambda i: "Frozen evaluator check",
        summarize=lambda r: {"frozen": not r, "differences": r})
def evaluator_differences(original: EvaluatorRecord | None, variant: EvaluatorRecord | None) -> list[str]:
    """The variant must be reviewed by the same reviewer model, prompt version and scan settings as the original."""
    if original is None or variant is None:
        return ["an attention timeline is missing on one side, so the evaluator settings cannot be compared"]
    return [f"{name}: {getattr(original, name)} for the original, {getattr(variant, name)} for the variant" for name in EVALUATOR_FIELDS
            if getattr(original, name) != getattr(variant, name)]


def baseline_differences(recorded: EvaluatorRecord | None, provider: Any) -> list[str]:
    """A recorded audit of the original must have been produced under the settings the variants will be reviewed with."""
    if recorded is None:
        return ["the recorded audit has no attention timeline"]
    cfg = AttentionConfig.from_settings(get_settings())
    now = {"cold_viewer_prompt_version": COLD_VIEWER_PROMPT_VERSION, "provider": provider.capability.name, "model": provider.capability.model,
           "reasoning_effort": getattr(provider, "reasoning_effort", None), "coarse_window_ms": cfg.coarse_window_ms, "precision_enabled": cfg.precision_enabled,
           "precision_window_ms": cfg.precision_window_ms, "precision_lead_ms": cfg.precision_lead_ms, "max_precision_regions": cfg.max_precision_regions,
           "max_precision_windows": cfg.max_precision_windows, "asr_language": get_settings().dl_asr_language}
    return [f"{k}: {getattr(recorded, k)} recorded, {v} now" for k, v in now.items() if getattr(recorded, k) != v]


@traced("regression_check", kind="tool", display=lambda i: "Regression gate",
        summarize=lambda r: {"passed": r[0], "regressions": r[1]})
def regression_check(comparison: AuditComparison | None) -> tuple[bool, list[str]]:
    """Nothing may get worse: protected content and constraints, new weaknesses, whole-video preference for the original, audience
    predictions, and predicted attention (a new high-risk moment, or any rise in the opening window)."""
    if comparison is None:
        return False, ["no comparison"]
    return not comparison.regressed, list(comparison.regressed)


@traced("arm_verdict", kind="tool", display=lambda i: "Experiment verdict", summarize=lambda r: {"verdict": r[0], "reason": r[1]})
def arm_verdict(*, rendered: bool, verified: bool | None, candidate_audit_complete: bool | None, evaluator_diff: list[str],
                comparison: AuditComparison | None) -> tuple[Verdict, str]:
    """Deterministic verdict for one experiment.
    incomplete: the edit was not tested (render, render check, blind audit, frozen evaluator or comparison missing).
    rejected / loss: something got worse (the target resolved / did not). Known regressions count even when preference is uncertain.
    inconclusive: nothing is known to be worse, but the evidence cannot say more: an order-dependent or failed whole-video
    comparison, or protected items and constraints that were never checked. Not learned as an outcome.
    win: the target moment resolved, the whole-video comparison was stable, every protected item was checked, and nothing got worse.
    neutral: a valid comparison in which the target moment did not reliably change and nothing got worse."""
    if not rendered:
        return "incomplete", "the render failed, so the hypothesis was not tested"
    if not verified:
        return "incomplete", "the rendered file does not show the intended change, so reviewing it would not test the hypothesis"
    if candidate_audit_complete is not True:
        return "incomplete", "the blind audit of the variant was not completed"
    if evaluator_diff:
        return "incomplete", "the variant was reviewed under different evaluation settings (" + "; ".join(evaluator_diff[:3]) + "), so the two reviews are not comparable"
    if comparison is None:
        return "incomplete", "the variant was not compared with the original"
    passed, regressions = regression_check(comparison)
    if not passed:
        if comparison.target_resolved == "yes":
            return "rejected", "the target moment resolved, but the regression gate found: " + "; ".join(regressions[:3])
        return "loss", "the target moment did not resolve and something got worse: " + "; ".join(regressions[:3])
    if comparison.protected_unchecked:
        return "inconclusive", (f"{len(comparison.protected_unchecked)} protected item(s) or constraint(s) got no check result ("
                                + "; ".join(comparison.protected_unchecked[:3]) + "), so the variant cannot be shown to respect them")
    if comparison.outcome == "insufficient_evidence":
        return "inconclusive", "the whole-video comparison changed with presentation order or failed, so the effect of the edit is unknown"
    if comparison.target_resolved == "yes":
        return "win", "improvement observed after the edit: the target moment resolved, the whole-video comparison was stable, and nothing got worse"
    return "neutral", f"the target moment did not reliably change (resolved: {comparison.target_resolved}), and nothing got worse"


HYPOTHESIS_STATUS: dict[str, str] = {
    "win": "supported: improvement observed after an edit that changed only what this hypothesis says matters",
    "neutral": "not supported: the edit that addresses it made no reliable difference",
    "loss": "contradicted: the edit that addresses it made the video worse",
    "rejected": "mixed: the target moment improved but something else got worse",
    "inconclusive": "unresolved: the comparison could not establish an effect either way",
    "incomplete": "untested: the experiment could not be completed",
}


def conclude(plan: ExperimentPlan, arms: list[ArmResult]) -> tuple[dict[str, str], list[str]]:
    by_h = {h.id: h for h in plan.hypotheses}
    results = {h.id: (f"not tested: contradicted by {', '.join(h.evidence_against)}" if h.status == "contradicted" else "not tested in this run") for h in plan.hypotheses}
    for arm in arms:
        status = ("unresolved: the edit was conservatively rejected, but evaluation was incomplete; learning withheld: " + "; ".join(arm.learning_blockers)
                  if arm.verdict in LEARNED_VERDICTS and arm.evaluation_complete is False else HYPOTHESIS_STATUS[arm.verdict])
        results[arm.hypothesis_id] = status
        for other in arm.also_tests:
            results[other] = f"{status} (shared edit with {arm.hypothesis_id}, so this result does not separate them)"
    lines: list[str] = []
    conclusive = [a for a in arms if a.verdict in LEARNED_VERDICTS and a.evaluation_complete is not False]
    separating = [a for a in conclusive if not a.also_tests]
    wins = [a for a in separating if a.verdict == "win"]
    if len(separating) >= 2:
        others = [a for a in separating if a.verdict != "win"]
        if len(wins) == 1:
            w = wins[0]
            lines.append(f"The results favour {w.hypothesis_id} ({w.cause_type}) over " + ", ".join(f"{a.hypothesis_id} ({a.cause_type})" for a in others)
                         + f": {w.mutation_type} alone improved the video, while " + ", ".join(f"{a.mutation_type} alone was {a.verdict}" for a in others) + ".")
        elif len(wins) >= 2:
            lines.append("More than one edit improved the video; these results do not separate the hypotheses.")
        else:
            lines.append("No edit produced an improvement; these experiments leave the cause of the weakness undetermined.")
    elif len(conclusive) >= 1:
        a = separating[0] if separating else conclusive[0]
        if a.also_tests:
            lines.append(f"One conclusive experiment: {a.mutation_type} addresses {', '.join([a.hypothesis_id, *a.also_tests])} with the same edit and was {a.verdict}; "
                         "a shared edit cannot separate those explanations.")
        else:
            lines.append(f"One conclusive experiment: {a.mutation_type} for {a.hypothesis_id} ({a.cause_type}) was {a.verdict}. "
                         "No competing hypothesis was tested conclusively with a different edit, so this does not separate the explanations.")
    elif arms and any(a.verdict == "inconclusive" or a.verdict in LEARNED_VERDICTS and a.evaluation_complete is False for a in arms):
        lines.append("No experiment gave a conclusive result; the cause of the weakness remains undetermined.")
    else:
        lines.append("No experiment was completed.")
    for hid, status in results.items():
        h = by_h.get(hid)
        if h is not None:
            lines.append(f"{hid} {h.cause_type}: {status}")
    return results, lines


def arm_axes(original: AuditReport, candidate: AuditReport, comparison: AuditComparison | None) -> ArmAxes:
    def confused(a: AuditReport) -> int:
        return sum(1 for r in a.window_reactions + a.precision_reactions if r.reaction == "confused" and not r.error)

    def comprehension(a: AuditReport) -> int:
        return sum(1 for f in a.findings if f.objective == "comprehension")

    def payoff(a: AuditReport) -> int | None:
        return next((ev.start_ms for ev in a.attention.events if ev.type == "payoff_delivered"), None) if a.attention else None

    axes = ArmAxes(confused_readings_before=confused(original), confused_readings_after=confused(candidate), comprehension_findings_before=comprehension(original),
                   comprehension_findings_after=comprehension(candidate), payoff_delivered_before_ms=payoff(original), payoff_delivered_after_ms=payoff(candidate))
    if comparison is not None:
        axes.continue_watching_preference = comparison.full_preference
        axes.target_moment_preference = comparison.target_preference
        axes.target_resolved = comparison.target_resolved
        if comparison.stability is not None:
            parts = comparison.stability.agreement.split("; ")
            axes.target_moment_agreement = next((p for p in parts if p.startswith("target")), "")
            axes.continue_watching_agreement = next((p for p in parts if p.startswith("whole")), "")
        axes.protected_checked = len(comparison.protected_checks)
        axes.protected_unchecked = list(comparison.protected_unchecked)
        axes.regressions = list(comparison.regressed)
        if comparison.attention is not None:
            axes.attention_target_before, axes.attention_target_after = comparison.attention.target_peak_before, comparison.attention.target_peak_after
            axes.attention_direction = comparison.attention.global_direction
            axes.elevated_attention_ms_before, axes.elevated_attention_ms_after = comparison.attention.elevated_ms_before, comparison.attention.elevated_ms_after
    return axes


def candidate_records(candidates: list[RepairCandidate]) -> list[CandidateRecord]:
    return [CandidateRecord(key=c.key, mutation_type=c.mutation_type, description=c.description, ops=[op.model_dump(mode="json") for op in c.ops],
                            secondary_changes=list(c.secondary_changes), plan_hash=c.plan.content_hash()) for c in candidates]


def replay_plan(run: CausalRun, *, use_policy: bool = True) -> ExperimentPlan:
    """Re-run the deterministic planner on the inputs frozen in the run record: the dossier, the executable candidates rebuilt
    from the base plan, and the policy snapshot (or no policy). No model call, render or current policy file is involved."""
    if run.dossier is None or run.plan_inputs is None:
        raise ValueError("this run has no frozen planning inputs")
    inputs = run.plan_inputs
    if use_policy and any(record.id not in inputs.policy_snapshot.evaluation_receipts for record in inputs.policy_snapshot.records):
        raise ValueError("this historical policy snapshot predates evaluation-completion receipts; exact replay requires the original planner version")
    adapter = TypeAdapter(list[EditOp])
    cands: list[RepairCandidate] = []
    for c in inputs.candidates:
        ops = adapter.validate_python(c.ops)
        plan = apply_ops(inputs.base_plan, ops)
        if plan.content_hash() != c.plan_hash:
            raise ValueError(f"candidate {c.key} does not rebuild to its recorded plan")
        cands.append(RepairCandidate(key=c.key, mutation_type=c.mutation_type, description=c.description, ops=ops, secondary_changes=list(c.secondary_changes), plan=plan))
    return plan_experiments(run.dossier, competing_hypotheses(run.dossier), cands, inputs.base_plan, inputs.policy_snapshot if use_policy else None, arms=inputs.arms,
                            target_artifact_hash=inputs.target_artifact_hash, include_mocked=inputs.include_mocked)


# ----------------------------------------------------------------------------- the loop


@traced("directorloop.causal_run", kind="agent", display=lambda i: f"DirectorLoop causal experiments | {i['config']['video_id']}")
@workflow_session(persist=save_causal)
def run_causal(config: CausalConfig, providers: ProviderBundle, data_dir: Path, on_stage: Any = None, run_id: str | None = None, launched_via: str = "python") -> CausalRun:
    def emit(stage: str, msg: str, data: dict | None = None) -> None:
        if on_stage:
            on_stage(stage, msg, data)

    set_display_name(f"DirectorLoop causal experiments | {config.video_id}")
    started = time.monotonic()
    budget = CallBudget(config.max_model_calls, config.deadline_s)
    probe = BudgetedProvider(providers.probe, budget) if providers.probe is not None else None
    runtime = runtime_record(providers)
    runtime.update({"runner": "directorloop.runtime.causal.run_causal", "causal_version": CAUSAL_VERSION,
                    "decision_rules": "plan_experiments and arm_verdict (deterministic, no model call)",
                    "state": "data/causal/<causal_id>.json, data/audit/<audit_id>/audit.json, data/creative/conditional_policy.json"})
    run = CausalRun(id=run_id or new_id("causal"), config=config, created_at=utc_now_iso(), runtime=runtime, mocked_stages=mocked_stages(providers), launched_via=launched_via)
    attach_workflow(run)
    run.weave_call_id, run.weave_url = current_call_ref()
    save_causal(run, data_dir)
    emit("STARTED", f"causal run {run.id}: {config.video_id}, up to {config.arms} experiment render(s){' (plan only)' if config.plan_only else ''}",
         {"causal_id": run.id, "weave_url": run.weave_url})

    def finish(status: str, reason: str, error: str | None = None) -> CausalRun:
        with workflow_stage("final_selection", "Conclusions and stop reason") as stage:
            run.status, run.stop_reason, run.error = status, reason, error  # type: ignore[assignment]
            run.ended_at = utc_now_iso()
            run.budget = {**budget.summary(), "renders_allowed": 0 if config.plan_only else config.arms, "renders_used": sum(1 for a in run.arms if a.render_hash)}
            run.timings_ms["total_ms"] = int((time.monotonic() - started) * 1000)
            stage.update(status=status, stop_reason=reason, conclusions=run.conclusions, budget=run.budget)
        save_causal(run, data_dir)
        emit({"completed": "DONE", "cancelled": "CANCELED"}.get(status, "FAILED"), f"causal run {run.id}: {reason}", {"causal_id": run.id})
        return run

    def wrap_up() -> None:
        """Conclusions from the finished experiments, then one policy record per conclusive experiment, with its provenance."""
        assert run.plan is not None and run.dossier is not None and audit is not None
        from ..planning.evidence_admission import arm_learning_blockers

        for arm in run.arms:
            arm.learning_blockers = arm_learning_blockers(arm.model_dump(mode="json"))
            arm.evaluation_complete = not arm.learning_blockers
            arm.policy_eligible = arm.verdict in LEARNED_VERDICTS and arm.evaluation_complete
        with workflow_stage("conclude", "What the experiments say about the hypotheses", kind="agent") as stage:
            run.hypothesis_results, run.conclusions = conclude(run.plan, run.arms)
            stage.update(results=run.hypothesis_results, conclusions=run.conclusions)
        with workflow_stage("update_policy", "Update the conditional policy") as stage:
            path = policy_path(data_dir)
            experiments = {x.id: x for x in run.plan.experiments}
            added: list[str] = []
            skipped = [f"{a.experiment_id} {a.verdict}: " + "; ".join(a.learning_blockers or ["verdict is not conclusive"]) for a in run.arms if not a.policy_eligible]
            with policy_lock(path):
                policy = load_conditional_policy(path)
                for arm in run.arms:
                    if not arm.policy_eligible:
                        continue
                    rec = new_policy_record(video_id=config.video_id, run_id=run.id, experiment=experiments[arm.experiment_id], conditions=run.dossier.conditions,
                                            outcome=arm.verdict, evidence=f"{arm.mutation_type} for {arm.cause_type}: {arm.verdict_reason}", artifact_hash=run.artifact_hash,
                                            variant_artifact_hash=arm.render_hash or "",
                                            evaluator=audit.attention.evaluator.model_dump() if audit.attention else {}, mocked=bool(run.mocked_stages),
                                            comparison_outcome=arm.comparison.outcome if arm.comparison else "",
                                            comparison_version=arm.comparison.comparison_version if arm.comparison else "")
                    policy.records.append(rec)
                    policy.evaluation_receipts[rec.id] = {"complete": True, "blockers": [], "method": "runtime-comparison-admission-v1"}
                    arm.policy_record_id = rec.id
                    added.append(rec.id)
                if added:
                    policy.version += 1
                    save_conditional_policy(policy, path)
            run.policy_records_added = added
            run.policy_after = policy_statements(policy)
            stage.update(records_added=added, not_learned=skipped, policy_version=policy.version, policy_sha256=policy_sha256(policy), statements=run.policy_after)
        emit("POLICY", f"{len(run.policy_records_added)} policy record(s) added under the observed conditions"
             + (f"; not learned: {', '.join(skipped)}" if skipped else ""))

    audit: AuditReport | None = None
    video = Path(config.video_path)
    refused: str | None = None
    with workflow_stage("ingest", "Validate video", inputs={"video_id": config.video_id}) as stage:
        refused = validate_video_path(video) or (None if probe is not None else "no vision reviewer is configured")
        if refused:
            stage.status = "failed"
            stage.update(refused=refused)
        else:
            run.artifact_hash = sha256_file(video)
            stage.update(artifact_hash=run.artifact_hash)
    if refused:
        return finish("failed", f"input refused: {refused}", error=refused)
    assert probe is not None

    try:
        early: tuple[str, str, str | None] | None = None
        with workflow_stage("analyze_original", "Analyze the original", kind="agent") as stage:
            if config.audit_id:
                stored = data_dir / "audit" / config.audit_id / "audit.json"
                recorded = AuditReport.model_validate_json(stored.read_text(encoding="utf-8")) if stored.exists() else None
                if recorded is None or recorded.artifact_hash != run.artifact_hash or recorded.attention is None:
                    stage.status = "failed"
                    early = ("failed", f"stored audit {config.audit_id} is missing, belongs to another file, or has no attention timeline", "audit mismatch")
                elif diff := baseline_differences(recorded.attention.evaluator, probe):
                    stage.status = "failed"
                    stage.update(evaluator_differences=diff)
                    early = ("failed", f"stored audit {config.audit_id} was produced under different evaluation settings ({'; '.join(diff[:3])}); "
                             "audit the original again so the variants are reviewed the same way", "evaluator mismatch")
                else:
                    audit, run.audit_source = recorded, "recorded"
                    emit("AUDITING", f"using recorded audit {recorded.id} of this exact file under the current evaluator settings (not a fresh review)")
            else:
                emit("AUDITING", "cold-audience audit of the original (unprimed: no objective, notes or labels)")
                audit = run_audit(video_id=config.video_id, video_path=video, version_id="v0", provider=probe, data_dir=data_dir, on_stage=on_stage)
                run.audit_source = "fresh"
            if audit is not None:
                run.audit_id = audit.id
                stage.update(audit_id=audit.id, audit_source=run.audit_source, attention=audit.attention.summary.text if audit.attention else None, findings=len(audit.findings))
                if audit.status != "complete":
                    stage.status = "incomplete"
                    early = ("completed", "the audit of the original was incomplete; no experiment is designed on partial evidence", None)
        if early is not None or audit is None:
            status, reason, error = early or ("failed", "no audit", "no audit")
            return finish(status, reason, error)

        with workflow_stage("build_genome", "Build the creative genome") as stage:
            genome = extract_genome(video, probe, data_dir / "creative" / "genomes", category=config.category)
            info = inspect_media(video)
            signals = extract_signals(video, int(info.duration_ms or 0), int(info.width or 0), int(info.height or 0))
            stage.update(beats=[f"{b.id} {b.role.value} {b.start_ms}-{b.end_ms} ms" for b in genome.beats], cuts_ms=signals.cuts_ms[:12])

        with workflow_stage("evidence_dossier", "Symptom and evidence dossier") as stage:
            dossier = build_dossier(audit, genome, signals, config.category)
            run.dossier = dossier
            if dossier is None:
                stage.update(symptom=None)
            else:
                stage.update(symptom=dossier.symptom.description, evidence=[f"{e.id} [{e.source}] {e.text} (for {e.supports}, against {e.against})" for e in dossier.evidence],
                             open_loops=len(dossier.open_loops), conditions=dossier.conditions)
        if dossier is None:
            return finish("completed", "no symptom: the audit found no predicted attention rise and no weakness to explain")
        emit("PLANNING", f"symptom: {dossier.symptom.description}; {len(dossier.evidence)} evidence items")

        with workflow_stage("hypotheses", "Competing cause hypotheses", kind="agent") as stage:
            hypotheses = competing_hypotheses(dossier)
            stage.update(hypotheses=[f"{h.id} {h.cause_type} [{h.status}, {h.strength}, share {h.evidence_share}]: for {h.evidence_for}, against {h.evidence_against}"
                                     for h in hypotheses])

        with workflow_stage("policy_lookup", "Policy lookup under matching conditions") as stage:
            policy = load_conditional_policy(policy_path(data_dir))
            run.policy_before = policy_statements(policy)
            stage.update(policy_version=policy.version, policy_sha256=policy_sha256(policy), records=len(policy.records), statements=run.policy_before,
                         conditions=dossier.conditions, admission="other source files only; scripted test records only for scripted runs")

        project_id = f"proj_{config.video_id}"
        symptom = dossier.symptom
        finding = next((f for f in audit.findings if f.id in symptom.finding_ids), None) or AuditFinding(
            id=f"{audit.id}_symptom", version_id="v0", start_ms=symptom.start_ms, end_ms=symptom.end_ms, weakness=symptom.description, objective="attention")
        with workflow_stage("rank_experiments", "Rank single-variable experiments", kind="agent") as stage:
            candidates, base_plan, _manifest, _brief = build_candidates(finding, genome, video, project_id)
            run.plan_inputs = PlanInputs(base_plan=base_plan, candidates=candidate_records(candidates), policy_sha256=policy_sha256(policy), policy_snapshot=policy,
                                         target_artifact_hash=run.artifact_hash, arms=config.arms, include_mocked=bool(run.mocked_stages))
            plan = plan_experiments(dossier, hypotheses, candidates, base_plan, policy, arms=config.arms, target_artifact_hash=run.artifact_hash,
                                    include_mocked=bool(run.mocked_stages))  # plan-only ranks and selects exactly as a run would
            run.plan = plan
            stage.update(queue=[f"#{x.rank} {x.id} {x.intervention_id} {x.mutation_type} tests {', '.join([x.hypothesis_id, *x.also_tests])} ({x.cause_type}): priority "
                                f"{x.priority}, without policy {x.priority_without_policy} (rank {x.rank_without_policy}); gain {x.information_gain}; "
                                f"{'discriminating' if x.discriminating else 'not discriminating'}; prior {x.prior.score:+.2f} from {x.prior.records} records"
                                for x in plan.experiments], candidates_offered=len(candidates), policy_effect=plan.policy_effect.model_dump())
        with workflow_stage("choose_experiment", "Choose experiments or decide not to edit") as stage:
            stage.update(decision=plan.decision, reason=plan.decision_reason, chosen=plan.chosen, chosen_without_policy=plan.chosen_without_policy,
                         policy_effect=plan.policy_effect.summary, uncertainty=plan.uncertainty, best_discriminating_test=plan.best_discriminating_test)
        emit("PLANNED", f"{plan.decision}: {plan.decision_reason}")
        save_causal(run, data_dir)
        if plan.decision == "do_not_edit":
            run.hypothesis_results, run.conclusions = conclude(plan, [])
            return finish("completed", f"do not edit: {plan.decision_reason}")
        if config.plan_only:
            xs = {x.id: x for x in plan.experiments}
            run.conclusions = ["plan only, nothing rendered: a run would test " + "; ".join(f"{i} {xs[i].mutation_type} for {xs[i].hypothesis_id} ({xs[i].cause_type})"
                                                                                           for i in plan.chosen),
                               plan.policy_effect.summary, *plan.policy_effect.rank_changes]
            return finish("completed", "plan only: no render requested")

        manifest = source_manifest(project_id, video, genome.artifact_hash)
        by_key = {c.key: c for c in candidates}
        experiments = {x.id: x for x in plan.experiments}
        protected = list(dict.fromkeys(finding.keep_unchanged + config.constraints))  # every item: nothing is dropped before the check
        for n, xid in enumerate(plan.chosen, start=1):
            x: Experiment = experiments[xid]
            cand = by_key[x.edit_key]
            arm = ArmResult(experiment_id=x.id, hypothesis_id=x.hypothesis_id, cause_type=x.cause_type, also_tests=x.also_tests, intervention_id=x.intervention_id,
                            plan_hash=x.plan_hash, mutation_type=x.mutation_type, edit_key=x.edit_key, description=x.description, changes=x.changes, unchanged=x.unchanged,
                            side_effects=x.side_effects, protected_items=protected)
            run.arms.append(arm)
            calls_before = budget.calls
            with workflow_stage("experiment", f"Experiment {x.id}: {x.mutation_type} tests {x.hypothesis_id} ({x.cause_type})", kind="agent",
                                inputs={"experiment": x.id, "intervention": x.intervention_id, "hypotheses": [x.hypothesis_id, *x.also_tests], "cause": x.cause_type,
                                        "edit": x.description, "changes": x.changes, "unchanged": x.unchanged, "side_effects": x.side_effects}) as exp_stage:
                emit("EXPERIMENT", f"{x.id}: {x.description} (tests {x.hypothesis_id} {x.cause_type}; changes only: {'; '.join(x.changes) or 'nothing measurable'})")
                t = time.monotonic()
                rend: dict[str, Any] | None = None
                budget.check_deadline()  # the wall-clock limit also covers renders, not only model calls
                with workflow_stage("render_variant", "Render the variant") as stage:
                    try:
                        rend = render_candidate(cand.plan, manifest, video, data_dir)
                        arm.render_hash, arm.render_path, arm.duration_ms = rend["artifact_hash"], rend["path"], rend["duration_ms"]
                        stage.update(artifact_hash=arm.render_hash, duration_ms=arm.duration_ms)
                    except Exception as exc:  # noqa: BLE001 - a failed render is a recorded outcome, not a crash
                        stage.status = "failed"
                        stage.update(error=safe_failure(exc))
                arm.timings_ms["render_ms"] = int((time.monotonic() - t) * 1000)
                cand_audit: AuditReport | None = None
                comparison: AuditComparison | None = None
                if rend is not None:
                    beat = next((b for b in genome.beats if cand.ops and getattr(cand.ops[0], "segment_id", None) == b.id), None)
                    with workflow_stage("verify", "Verify the rendered change") as stage:
                        check = verify_change(cand.mutation_type, cand.description, video, Path(rend["path"]), base_plan, cand.plan, beat.text if beat else None,
                                              (finding.start_ms, finding.end_ms), (beat.start_ms, beat.end_ms) if beat else None)
                        arm.verified, arm.checks = check.verified, check.checks
                        stage.update(verified=check.verified, checks=check.checks)
                        if not check.verified:
                            stage.status = "incomplete"
                if arm.verified and rend is not None:
                    t = time.monotonic()
                    version_id = f"v{n}-{rend['artifact_hash'][:8]}"
                    arm.review_inputs = {"video_id": config.video_id, "version_id": version_id, "video_sha256": rend["artifact_hash"],
                                         "withheld": "finding, hypotheses, edit, objective, constraints and the other arms"}
                    with workflow_stage("blind_candidate_audit", "Blind audit of the variant", kind="agent", inputs={"video_id": config.video_id, "version_id": version_id}) as stage:
                        emit("FRESH_REVIEW", f"{x.id}: blind cold-audience audit of the variant (the reviewer receives only the rendered file)")
                        cand_audit = run_audit(video_id=config.video_id, video_path=Path(rend["path"]), version_id=version_id, provider=probe, data_dir=data_dir, on_stage=on_stage)
                        arm.candidate_audit_id = cand_audit.id
                        arm.evaluator_differences = evaluator_differences(audit.attention.evaluator if audit.attention else None,
                                                                          cand_audit.attention.evaluator if cand_audit.attention else None)
                        stage.update(audit_id=cand_audit.id, audit_status=cand_audit.status, attention=cand_audit.attention.summary.text if cand_audit.attention else None,
                                     findings=len(cand_audit.findings), evaluator_frozen=not arm.evaluator_differences, evaluator_differences=arm.evaluator_differences)
                        if cand_audit.status != "complete" or cand_audit.artifact_hash != rend["artifact_hash"]:
                            stage.status = "incomplete"
                    arm.timings_ms["blind_audit_ms"] = int((time.monotonic() - t) * 1000)
                    if cand_audit.status == "complete" and cand_audit.artifact_hash == rend["artifact_hash"] and not arm.evaluator_differences:
                        t = time.monotonic()
                        with workflow_stage("pairwise_eval", "Compare with the same original in both presentation orders", kind="agent",
                                            inputs={"protected_items": protected}) as stage:
                            emit("COMPARING", f"{x.id}: target moment and whole video in both orders, predicted attention, {len(protected)} protected item(s)")
                            comparison = compare_versions(provider=probe, original=audit, candidate=cand_audit, finding=finding, candidate_plan=cand.plan, protected_items=protected)
                            arm.comparison = comparison
                            stage.update(outcome=comparison.outcome, target_resolved=comparison.target_resolved, full_preference=comparison.full_preference,
                                         target_preference=comparison.target_preference, improved=comparison.improved, regressed=comparison.regressed,
                                         protected_checked=len(comparison.protected_checks), protected_unchecked=comparison.protected_unchecked)
                        arm.timings_ms["compare_ms"] = int((time.monotonic() - t) * 1000)
                        arm.axes = arm_axes(audit, cand_audit, comparison)
                with workflow_stage("decision", "Regression gate and verdict") as stage:
                    arm.verdict, arm.verdict_reason = arm_verdict(rendered=rend is not None, verified=arm.verified,
                                                                  candidate_audit_complete=None if cand_audit is None else cand_audit.status == "complete",
                                                                  evaluator_diff=arm.evaluator_differences, comparison=comparison)
                    stage.update(verdict=arm.verdict, reason=arm.verdict_reason, learned=arm.verdict in LEARNED_VERDICTS)
                arm.model_calls = budget.calls - calls_before
                emit("DECIDED", f"{x.id} {x.mutation_type} for {x.hypothesis_id}: {arm.verdict.upper()}: {arm.verdict_reason[:220]}")
                exp_stage.update(verdict=arm.verdict, reason=arm.verdict_reason, model_calls=arm.model_calls, axes=arm.axes.model_dump() if arm.axes else None)
                if arm.verdict == "incomplete":
                    exp_stage.status = "incomplete"
            save_causal(run, data_dir)

        wrap_up()
        return finish("completed", run.conclusions[0] if run.conclusions else "experiments completed")
    except BudgetExceeded as exc:
        if run.plan is not None and run.dossier is not None and run.arms:
            wrap_up()  # experiments that finished before the limit still count
        return finish("completed", f"budget reached: {safe_limit_reason(exc)}")
    except JobCanceled as exc:
        finish("cancelled", "the run was cancelled by the operator", error=safe_failure(exc))
        raise  # the job worker distinguishes cancellation by this exception
    except Exception as exc:  # noqa: BLE001 - close the record with the real reason instead of leaving it running
        return finish("failed", f"the run stopped with an error: {safe_failure(exc)}", error=safe_failure(exc))
