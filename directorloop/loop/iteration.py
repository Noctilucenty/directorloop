"""One bounded improvement iteration (spec section 20).

evaluate baseline (cached if identical) -> aggregate findings -> route + propose ->
validate -> render candidate -> evaluate candidate (always fresh) -> compare ->
decide -> record the experiment in strategy memory. Every stage is reported through
`on_stage`, every boundary is a Weave op, and nothing is cached in a way that would
make the candidate result look fresh when it is not.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import weave

from ..domain.decision import Comparison, Outcome, PromotionDecision
from ..domain.edit_plan import apply_ops, describe_ops, plan_diff
from ..domain.evaluation import EvaluationRun
from ..domain.findings import FailureFinding
from ..domain.ids import new_id
from ..domain.pack import DemoPack
from ..domain.policy import PolicyRule, PolicyStore
from ..domain.repair import RepairProposal
from ..evals import compare_runs, evaluate_version
from ..media import RenderError, render_plan, validate_ops_scope, validate_plan
from ..observability.weave_ops import current_call_ref, set_display_name, traced
from ..planning import (
    LatencyProfile,
    aggregate_findings,
    build_routing_table,
    decide_acceptance,
    declared_coverage,
    policy_hints,
    propose_repair,
    record_experiment,
)
from ..providers.registry import ProviderBundle
from .state import ProjectState, VersionRecord, state_path

StageCallback = Callable[[str, str, dict | None], None]


class IterationCancelled(RuntimeError):
    pass


class IterationTimedOut(RuntimeError):
    pass


@dataclass
class IterationResult:
    experiment_id: str
    base_version: VersionRecord
    candidate_version: VersionRecord | None
    baseline_run: EvaluationRun
    candidate_run: EvaluationRun | None
    findings: list[FailureFinding]
    finding: FailureFinding | None
    proposal: RepairProposal | None
    comparison: Comparison | None
    decision: PromotionDecision | None
    policy_rule: PolicyRule | None
    outcome: str  # COMPLETED | NO_GAIN | NEEDS_REVIEW | NEEDS_SOURCE_MATERIAL | TIMED_OUT
    diff_lines: list[str] = field(default_factory=list)
    plan_diff: dict | None = None
    timings_ms: dict[str, int] = field(default_factory=dict)
    weave_url: str | None = None
    weave_call_id: str | None = None
    notes: list[str] = field(default_factory=list)
    llm_calls: int = 0
    policy_mode: str = "learned"


def _emit(cb: StageCallback | None, stage: str, message: str, data: dict | None = None) -> None:
    if cb:
        cb(stage, message, data)


def _check(is_cancelled: Callable[[], bool] | None, started: float, deadline_ms: int | None, stage: str) -> None:
    if is_cancelled and is_cancelled():
        raise IterationCancelled(f"cancelled during {stage}")
    if deadline_ms is not None and (time.monotonic() - started) * 1000 > deadline_ms:
        raise IterationTimedOut(f"deadline of {deadline_ms} ms exceeded during {stage}")


@traced("directorloop.run_iteration", kind="agent")
def run_iteration(
    *,
    pack: DemoPack,
    state: ProjectState,
    base_version: VersionRecord,
    providers: ProviderBundle,
    store: PolicyStore,
    latency: LatencyProfile,
    data_dir: Path,
    trials: int,
    deadline_ms: int | None,
    policy_mode: str = "learned",
    experiment_id: str | None = None,
    split: str = "dev",
    persist: bool = True,
    on_stage: StageCallback | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    job_id: str | None = None,
) -> IterationResult:
    experiment_id = experiment_id or new_id("exp")
    iteration_index = state.next_index()
    set_display_name(f"directorloop_iteration_{iteration_index}_{pack.project_id}")
    started = time.monotonic()
    timings: dict[str, int] = {}
    notes: list[str] = []
    llm_calls = 0
    asset_paths = {a.id: pack.asset_path(a) for a in pack.manifest.assets}
    cache_dir = data_dir / "cache" / pack.project_id
    if providers.probe is None:
        raise RuntimeError("no probe provider configured")

    with weave.attributes(
        {
            "project_id": pack.project_id,
            "experiment_id": experiment_id,
            "base_version": base_version.id,
            "base_artifact": base_version.artifact_hash,
            "suite_hash": pack.suite.content_hash(),
            "policy_version": store.version,
            "policy_mode": policy_mode,
            "split": split,
        }
    ):
        # 1. Baseline evaluation (cache allowed only for the identical artifact + configuration)
        _emit(on_stage, "PREFLIGHT", f"iteration {iteration_index} on {base_version.label}; budget {pack.brief.budget.max_llm_calls} model calls, deadline {deadline_ms} ms")
        _check(is_cancelled, started, deadline_ms, "preflight")
        t = time.monotonic()
        _emit(on_stage, "EVALUATING_BASELINE", "evaluating the baseline file (cache allowed for the identical artifact and configuration)")
        baseline_run = evaluate_version(
            version_id=base_version.id,
            version_label=base_version.label,
            artifact_path=Path(base_version.artifact_path),
            artifact_hash=base_version.artifact_hash,
            plan=base_version.plan,
            baseline_plan=None,
            manifest=pack.manifest,
            brief=pack.brief,
            suite=pack.suite,
            provider=providers.probe,
            trials=trials,
            cache_dir=cache_dir,
            allow_cache=True,
            approved_texts=[c.text for c in pack.truth.claims],
        )
        timings["evaluate_baseline_ms"] = int((time.monotonic() - t) * 1000)
        if baseline_run.mode == "fresh":
            llm_calls += trials
            latency.record("evaluate", baseline_run.latency_ms)
        state.evaluations[baseline_run.id] = baseline_run
        base_version.evaluation_run_id = baseline_run.id
        _emit(
            on_stage,
            "EVALUATING_BASELINE",
            f"baseline {base_version.label}: {baseline_run.questions_passed}/{baseline_run.questions_total} probe questions pass "
            f"[{baseline_run.mode.upper()}, {baseline_run.probe_modality}]",
            {"run_id": baseline_run.id, "mode": baseline_run.mode, "failed": baseline_run.failed_question_ids()},
        )

        # 2. Diagnose
        _check(is_cancelled, started, deadline_ms, "diagnosis")
        _emit(on_stage, "DIAGNOSING", "aggregating failed checks into structured findings")
        coverage = state.coverage or declared_coverage(pack.manifest, pack.truth)
        findings = aggregate_findings(baseline_run, pack.suite, pack.truth, base_version.plan, pack.manifest, coverage, pack.brief)
        if not findings:
            call_id, url = current_call_ref()
            _emit(on_stage, "DONE", "no supported failure found; the baseline already satisfies the suite", {"outcome": "NO_GAIN"})
            return IterationResult(
                experiment_id=experiment_id, base_version=base_version, candidate_version=None, baseline_run=baseline_run,
                candidate_run=None, findings=[], finding=None, proposal=None, comparison=None, decision=None, policy_rule=None,
                outcome="NO_GAIN", timings_ms=timings, weave_url=url, weave_call_id=call_id, notes=["no findings"], llm_calls=llm_calls, policy_mode=policy_mode,
            )
        finding = findings[0]
        _emit(
            on_stage,
            "DIAGNOSING",
            f"top finding: {finding.category} (severity {finding.severity:.2f}, confidence {finding.confidence:.2f}) at "
            f"{finding.start_ms}-{finding.end_ms} ms; {finding.observed}",
            {"finding": finding.model_dump(mode="json"), "finding_count": len(findings)},
        )

        # 3. Route + propose
        _check(is_cancelled, started, deadline_ms, "planning")
        _emit(on_stage, "PLANNING", f"building the routing table and candidate edits ({policy_mode} policy)")
        t = time.monotonic()
        hints = policy_hints(store, pack.brief.profile, finding.category) if policy_mode == "learned" else []
        routing_store = store if policy_mode == "learned" else PolicyStore()
        generation_available = providers.generation_available if hasattr(providers, "generation_available") else False
        elapsed = int((time.monotonic() - started) * 1000)
        remaining = None if deadline_ms is None else max(0, deadline_ms - elapsed - 8000)  # leave room for re-evaluation
        routing = build_routing_table(finding, pack.brief, base_version.plan, pack.manifest, coverage, routing_store, latency, generation_available)
        proposal = propose_repair(
            finding=finding,
            brief=pack.brief,
            plan=base_version.plan,
            baseline_plan=base_version.plan,
            manifest=pack.manifest,
            coverage=coverage,
            truth=pack.truth,
            asset_paths=asset_paths,
            routing=routing,
            policy_hints=hints,
            planner=providers.planner,
            deadline_ms=remaining,
            transcript=baseline_run.transcript_text,
        )
        timings["plan_ms"] = int((time.monotonic() - t) * 1000)
        if providers.planner is not None:
            llm_calls += 1
            latency.record("planner", timings["plan_ms"])
        labels = {a.id: a.label for a in pack.manifest.assets}
        _emit(
            on_stage,
            "PLANNING",
            f"chosen repair: {proposal.action} via {proposal.planner}. {proposal.decision_summary}",
            {"proposal": proposal.model_dump(mode="json"), "hints_used": len(hints)},
        )
        if proposal.action.value == "do_nothing" or (not proposal.ops and not proposal.requires_generation):
            call_id, url = current_call_ref()
            outcome = "NEEDS_SOURCE_MATERIAL" if not any(r.feasible and r.allowed for r in routing if r.action.value != "do_nothing") else "NO_GAIN"
            _emit(on_stage, "DONE", f"planner chose to leave the video unchanged: {proposal.decision_summary}", {"outcome": outcome})
            return IterationResult(
                experiment_id=experiment_id, base_version=base_version, candidate_version=None, baseline_run=baseline_run,
                candidate_run=None, findings=findings, finding=finding, proposal=proposal, comparison=None, decision=None,
                policy_rule=None, outcome=outcome, timings_ms=timings, weave_url=url, weave_call_id=call_id,
                notes=["no repair applied"], llm_calls=llm_calls, policy_mode=policy_mode,
            )
        if proposal.requires_generation:
            call_id, url = current_call_ref()
            _emit(on_stage, "GENERATING", "generation was selected but no generation provider is configured in this environment", {"outcome": "NEEDS_SOURCE_MATERIAL"})
            return IterationResult(
                experiment_id=experiment_id, base_version=base_version, candidate_version=None, baseline_run=baseline_run,
                candidate_run=None, findings=findings, finding=finding, proposal=proposal, comparison=None, decision=None,
                policy_rule=None, outcome="NEEDS_SOURCE_MATERIAL", timings_ms=timings, weave_url=url, weave_call_id=call_id,
                notes=["generation requested; provider unavailable"], llm_calls=llm_calls, policy_mode=policy_mode,
            )

        # 4. Validate + render
        _check(is_cancelled, started, deadline_ms, "rendering")
        scope_errors = validate_ops_scope(proposal.ops, pack.brief)
        if scope_errors:
            raise RenderError("proposal outside the permitted action scope: " + "; ".join(scope_errors))
        candidate_plan = apply_ops(base_version.plan, proposal.ops)
        candidate_plan.change_rationale = proposal.hypothesis[:300]
        vr = validate_plan(candidate_plan, pack.manifest, pack.brief, baseline=base_version.plan)
        if not vr.ok:
            raise RenderError("candidate plan failed validation: " + "; ".join(vr.errors))
        diff_lines = describe_ops(proposal.ops, base_version.plan, labels)
        _emit(on_stage, "RENDERING", "validated edit plan; rendering the candidate with FFmpeg", {"diff_lines": diff_lines, "warnings": vr.warnings})
        t = time.monotonic()
        res = render_plan(candidate_plan, pack.manifest, asset_paths, publish_dir=data_dir / "renders", work_dir=data_dir / "work")
        timings["render_ms"] = res.render_ms + res.verify_ms
        latency.record("render", timings["render_ms"])
        candidate = VersionRecord(
            id=new_id("ver"),
            project_id=pack.project_id,
            index=iteration_index,
            parent_version_id=base_version.id,
            role="candidate",
            status="unevaluated",
            plan=candidate_plan,
            plan_hash=candidate_plan.content_hash(),
            artifact_hash=res.artifact_hash,
            artifact_path=str(res.path),
            duration_ms=res.duration_ms,
            width=res.width,
            height=res.height,
            job_id=job_id,
            diff_lines=diff_lines,
            render_ms=res.render_ms,
            policy_mode=policy_mode,
        )
        state.versions.append(candidate)
        same_artifact = res.artifact_hash == base_version.artifact_hash
        _emit(
            on_stage,
            "RENDERING",
            f"candidate {candidate.label} rendered in {timings['render_ms']} ms ({res.duration_ms} ms, {res.artifact_hash[:12]})"
            + ("; identical bytes to the baseline" if same_artifact else ""),
            {"version_id": candidate.id, "artifact_hash": res.artifact_hash, "duration_ms": res.duration_ms},
        )

        # 5. Re-evaluate under the same frozen suite (never cached)
        _check(is_cancelled, started, deadline_ms, "re-evaluation")
        _emit(on_stage, "REEVALUATING", f"evaluating the candidate file with the same suite, provider and {trials} trials [FRESH]")
        t = time.monotonic()
        candidate_run = evaluate_version(
            version_id=candidate.id,
            version_label=candidate.label,
            artifact_path=res.path,
            artifact_hash=res.artifact_hash,
            plan=candidate_plan,
            baseline_plan=base_version.plan,
            manifest=pack.manifest,
            brief=pack.brief,
            suite=pack.suite,
            provider=providers.probe,
            trials=trials,
            cache_dir=cache_dir,
            allow_cache=False,
            approved_texts=[c.text for c in pack.truth.claims],
        )
        timings["evaluate_candidate_ms"] = int((time.monotonic() - t) * 1000)
        latency.record("evaluate", candidate_run.latency_ms)
        llm_calls += trials
        state.evaluations[candidate_run.id] = candidate_run
        candidate.evaluation_run_id = candidate_run.id
        _emit(
            on_stage,
            "REEVALUATING",
            f"candidate {candidate.label}: {candidate_run.questions_passed}/{candidate_run.questions_total} probe questions pass; "
            f"mechanical {'pass' if candidate_run.mechanical_passed else 'FAIL'}, constraints {'pass' if candidate_run.constraints_passed else 'FAIL'}",
            {"run_id": candidate_run.id, "failed": candidate_run.failed_question_ids()},
        )

        # 6. Regression check + decision
        _emit(on_stage, "REGRESSION_TESTING", "re-running previously passing questions and protected constraints against the candidate")
        comparison = compare_runs(baseline_run, candidate_run, pack.suite, base_version.duration_ms, candidate.duration_ms)
        _emit(
            on_stage,
            "REGRESSION_TESTING",
            f"fixed: {comparison.fixed or 'none'}; kept: {len(comparison.kept)}; regressed: {comparison.regressed or 'none'}; still failing: {comparison.still_failing or 'none'}",
            {"comparison": comparison.model_dump(mode="json")},
        )
        _emit(on_stage, "DECIDING", "applying hard gates, then the goal metric")
        from ..evals.compare import text_assisted_fixes

        assisted = text_assisted_fixes(pack.suite, pack.truth, base_version.plan, candidate_plan, comparison.fixed)
        decision = decide_acceptance(comparison, candidate_run, pack.brief, text_assisted=assisted)
        candidate.decision = decision
        candidate.status = {
            Outcome.PROMOTED: "promoted",
            Outcome.REJECTED: "rejected",
            Outcome.NEEDS_REVIEW: "needs_review",
            Outcome.NO_GAIN: "no_gain",
            Outcome.INSUFFICIENT_EVIDENCE: "insufficient_evidence",
            Outcome.NEEDS_SOURCE_MATERIAL: "needs_review",
        }[decision.outcome]
        if decision.outcome == Outcome.PROMOTED:
            state.best_version_id = candidate.id
        _emit(on_stage, "DECIDING", f"{decision.outcome.upper()}: {decision.reason}", {"decision": decision.model_dump(mode="json")})

        # 7. Learn
        _emit(on_stage, "LEARNING", "recording the experiment in strategy memory")
        total_ms = int((time.monotonic() - started) * 1000)
        cost = float(proposal.estimated_cost_usd)
        rule = record_experiment(
            store,
            experiment_id=experiment_id,
            project_id=pack.project_id,
            story_family=pack.suite.story_family,
            split=split,
            profile=pack.brief.profile,
            finding=finding,
            proposal=proposal,
            decision=decision,
            comparison=comparison,
            latency_ms=total_ms,
            cost_usd=cost,
            probe_config=f"{providers.probe.capability.name}:{providers.probe.capability.model}/{trials} trials",
        )
        _emit(
            on_stage,
            "LEARNING",
            f"rule {rule.id} is {rule.status} (supporting {rule.support_count}, counterexamples {rule.counter_count}, confidence {rule.confidence})",
            {"rule": rule.model_dump(mode="json")},
        )
        call_id, url = current_call_ref()
        candidate.weave_url = url
        timings["total_ms"] = total_ms
        if persist:
            state.save(state_path(data_dir, pack.project_id))
        outcome_state = {
            Outcome.PROMOTED: "COMPLETED",
            Outcome.REJECTED: "COMPLETED",
            Outcome.NEEDS_REVIEW: "NEEDS_REVIEW",
            Outcome.NO_GAIN: "NO_GAIN",
            Outcome.INSUFFICIENT_EVIDENCE: "NEEDS_REVIEW",
            Outcome.NEEDS_SOURCE_MATERIAL: "NEEDS_SOURCE_MATERIAL",
        }[decision.outcome]
        _emit(on_stage, "DONE", f"iteration finished in {total_ms} ms: {decision.outcome}", {"outcome": outcome_state, "timings_ms": timings, "weave_url": url})
        return IterationResult(
            experiment_id=experiment_id,
            base_version=base_version,
            candidate_version=candidate,
            baseline_run=baseline_run,
            candidate_run=candidate_run,
            findings=findings,
            finding=finding,
            proposal=proposal,
            comparison=comparison,
            decision=decision,
            policy_rule=rule,
            outcome=outcome_state,
            diff_lines=diff_lines,
            plan_diff=plan_diff(base_version.plan, candidate_plan),
            timings_ms=timings,
            weave_url=url,
            weave_call_id=call_id,
            notes=notes,
            llm_calls=llm_calls,
            policy_mode=policy_mode,
        )
