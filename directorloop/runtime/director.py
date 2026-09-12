"""DirectorLoop runtime: one launch from a video, an objective, constraints and an iteration budget to a decided outcome.

Per iteration: map the current audit's findings to repairs the system can perform (text selector constrained to validated
edits) -> rank -> real render -> verify the intended change in the rendered file -> fresh, unprimed audit of the candidate
-> comparison against the current version -> next-action decision (deterministic rules over the comparison).

Decisions:
- improvement: accept; the candidate becomes the current version and its fresh audit drives the next iteration.
- regression, mixed, tie, insufficient evidence, a failed render, an unverified change, an incomplete candidate audit or a
  version-label mismatch: keep the current version, record the lesson, exclude the failed edit and give its result to the
  selector so the next choice is a justified alternative (or nothing).
- nothing runnable left, or the budget spent: stop with an explicit reason.

The cold audit is never primed with the objective, the finding or the repair; the objective and constraints steer ranking,
selection and protected-content checks only. Every model call is a traced op with its exact prompt. The run record lists
the models, the prompt source digests, the git commit, mocked stages and manual interventions.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Literal

import weave
from pydantic import BaseModel, ConfigDict, Field

from ..audit.models import OBJECTIVES, AuditFinding, AuditReport, RepairRun
from ..audit.repairs import RepairCandidate, inspect_source_project, map_repair
from ..audit.review import run_audit
from ..audit.revise import compare_versions, interval_mappings, render_candidate, verify_change
from ..config import REPO_ROOT
from ..creative.genome import extract_genome
from ..creative.mutate import identity_plan, source_manifest
from ..creative.policy import StrategyEvidence, load_policy, save_policy
from ..domain.creative import MutationType
from ..domain.ids import new_id, sha256_file, utc_now_iso
from ..media.probe import inspect_media
from ..observability.weave_ops import current_call_ref, git_commit, set_display_name, traced
from ..providers.registry import ProviderBundle

EDIT_TYPES = ("REMOVE_BEAT", "MOVE_BEAT_EARLIER", "MOVE_BEAT_LATER", "TRIM_PAUSE", "PUNCH_IN")
POLICY_TYPE = {"REMOVE_BEAT": MutationType.REMOVE_REDUNDANT_BEAT, "MOVE_BEAT_EARLIER": MutationType.SHOT_REORDER, "MOVE_BEAT_LATER": MutationType.SHOT_REORDER,
               "TRIM_PAUSE": MutationType.SHORTEN_SHOT, "PUNCH_IN": MutationType.PATTERN_INTERRUPT}
SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}
UNCERTAINTY_RANK = {"low": 0, "medium": 1, "high": 2}
PROMPT_SOURCES = ("directorloop/audit/review.py", "directorloop/audit/repairs.py", "directorloop/audit/revise.py", "directorloop/runtime/director.py",
                  "directorloop/providers/base.py")
MAX_DURATION_MS = 180_000
RUNTIME_VERSION = "director-v1"


class RunConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_id: str = Field(pattern=r"^[a-z0-9_\-]{2,80}$")
    video_path: str
    objective: str = Field(default="Keep an unfamiliar viewer watching and understanding the video without losing what already works.", max_length=500)
    focus: str | None = None  # one of OBJECTIVES; findings about it are repaired first
    constraints: list[str] = Field(default_factory=list, max_length=12)
    allowed_edits: list[str] | None = None  # subset of EDIT_TYPES; None = all
    iteration_budget: int = Field(default=3, ge=1, le=8)
    max_attempts_per_finding: int = Field(default=2, ge=1, le=5)
    category: str = "educational_short"

    def model_post_init(self, __context: Any) -> None:
        if self.focus is not None and self.focus not in OBJECTIVES:
            raise ValueError(f"focus must be one of {OBJECTIVES}")
        if self.allowed_edits is not None and any(e not in EDIT_TYPES for e in self.allowed_edits):
            raise ValueError(f"allowed_edits must be a subset of {list(EDIT_TYPES)}")
        if any(len(c) > 200 for c in self.constraints):
            raise ValueError("each constraint must be at most 200 characters")


class ConsideredFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: str
    interval_ms: tuple[int, int]
    issue_type: str
    objective: str
    severity: str
    uncertainty: str
    weakness: str
    route: str | None = None
    runnable: bool = False
    edit: str = ""
    selection_reason: str = ""
    why_not_runnable: str | None = None
    skipped: str | None = None


class IterationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    current_version_id: str
    current_artifact_hash: str
    audit_id: str
    considered: list[ConsideredFinding] = Field(default_factory=list)
    finding_id: str | None = None
    finding: str = ""
    ranking_reason: str = ""
    selection_reason: str = ""
    repair_run_id: str | None = None
    edit: str = ""
    mutation_type: str | None = None
    change_verified: bool | None = None
    verification_checks: list[str] = Field(default_factory=list)
    candidate_version_id: str | None = None
    candidate_artifact_hash: str | None = None
    candidate_path: str | None = None
    candidate_audit_id: str | None = None
    outcome: str | None = None
    target_resolved: str | None = None
    improved: list[str] = Field(default_factory=list)
    regressed: list[str] = Field(default_factory=list)
    decision: Literal["accept", "reject_keep_current", "incomplete_keep_current", "stop"]
    reason: str
    next_action: Literal["continue_from_candidate", "try_alternative", "stop"]
    next_action_reason: str = ""
    lesson_id: str | None = None
    timings_ms: dict[str, int] = Field(default_factory=dict)


class DirectorRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    config: RunConfig
    status: Literal["running", "completed", "failed"] = "running"
    stop_reason: str = ""
    final_decision: str = ""
    created_at: str
    ended_at: str | None = None
    original_artifact_hash: str = ""
    original_path: str
    final_version_id: str = "v0"
    final_artifact_hash: str = ""
    final_path: str = ""
    iterations: list[IterationRecord] = Field(default_factory=list)
    audit_ids: list[str] = Field(default_factory=list)
    repair_run_ids: list[str] = Field(default_factory=list)
    lessons: list[dict[str, Any]] = Field(default_factory=list)
    runtime: dict[str, Any] = Field(default_factory=dict)
    mocked_stages: list[str] = Field(default_factory=list)
    manual_interventions: list[str] = Field(default_factory=list)
    annotations: list[str] = Field(default_factory=list)  # post-hoc notes added outside the run (defects found later, supersession); never decisions
    launched_via: str = "python"
    weave_url: str | None = None
    weave_call_id: str | None = None
    timings_ms: dict[str, int] = Field(default_factory=dict)
    error: str | None = None


# ----------------------------------------------------------------------------- pure decision logic


@traced("decide_next_action", kind="tool")
def decide_next_action(*, rendered: bool, verified: bool | None, labels_match: bool | None, candidate_audit_complete: bool | None, outcome: str | None,
                       target_resolved: str | None, regressed: list[str], budget_left: int) -> dict[str, str]:
    """Deterministic next step from what the loop observed. No model call: the rules are the inspectable contract."""
    more = budget_left > 0
    alt = ("try_alternative", "the failed edit is excluded and its result is given to the selector, which must justify another edit or choose none") if more else \
          ("stop", "iteration budget used")
    if not rendered:
        return {"decision": "incomplete_keep_current", "reason": "the render failed, so there is no candidate to judge", "next_action": alt[0], "next_action_reason": alt[1]}
    if not verified:
        return {"decision": "incomplete_keep_current", "reason": "the rendered file does not show the intended change, so its review would not test the repair",
                "next_action": alt[0], "next_action_reason": alt[1]}
    if labels_match is False:
        return {"decision": "incomplete_keep_current", "reason": "the audited files do not match the version labels (hash mismatch); the comparison is void",
                "next_action": alt[0], "next_action_reason": alt[1]}
    if candidate_audit_complete is False:
        return {"decision": "incomplete_keep_current", "reason": "the fresh audit of the candidate was incomplete, so the candidate is not judged on partial evidence",
                "next_action": alt[0], "next_action_reason": alt[1]}
    if outcome == "improvement":
        return {"decision": "accept", "reason": "the fresh audit and the comparisons show the target weakness resolved with no regression",
                "next_action": "continue_from_candidate" if more else "stop",
                "next_action_reason": "the accepted version's own fresh audit drives the next repair" if more else "iteration budget used"}
    why = {
        "regression": "the candidate is worse: " + "; ".join(regressed[:3]),
        "mixed": "the target moved but something else got worse: " + "; ".join(regressed[:3]),
        "tie": f"no reliable difference (target resolved: {target_resolved}); a change without a demonstrated gain is not kept",
    }.get(outcome or "", "the comparison was unstable or incomplete, so there is not enough evidence to replace the current version")
    return {"decision": "reject_keep_current", "reason": why, "next_action": alt[0], "next_action_reason": alt[1]}


def rank_options(options: list[tuple[AuditFinding, RepairCandidate, float]], focus: str | None, attempts: dict[str, int]) -> list[tuple[AuditFinding, RepairCandidate, str]]:
    def key(o: tuple[AuditFinding, RepairCandidate, float]) -> tuple:
        f, _, mean = o
        return (0 if focus and f.objective == focus else 1, SEVERITY_RANK.get(f.severity, 1), UNCERTAINTY_RANK.get(f.uncertainty, 1), attempts.get(f.id, 0), -mean, f.start_ms)

    out = []
    for f, c, mean in sorted(options, key=key):
        parts = []
        if focus:
            parts.append(f"objective {f.objective}{' matches' if f.objective == focus else ' does not match'} the run focus {focus}")
        parts.append(f"severity {f.severity}, uncertainty {f.uncertainty}")
        if attempts.get(f.id):
            parts.append(f"{attempts[f.id]} earlier attempt(s) on this finding")
        parts.append(f"policy evidence mean for {c.mutation_type} {mean:.2f}")
        out.append((f, c, "; ".join(parts)))
    return out


# ----------------------------------------------------------------------------- persistence


def runs_dir(data_dir: Path) -> Path:
    return data_dir / "runs"


def save_run(run: DirectorRun, data_dir: Path) -> None:
    d = runs_dir(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f"{run.id}.json.tmp"
    tmp.write_text(run.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(d / f"{run.id}.json")


def load_run(data_dir: Path, run_id: str) -> DirectorRun | None:
    f = runs_dir(data_dir) / f"{run_id}.json"
    return DirectorRun.model_validate_json(f.read_text(encoding="utf-8")) if f.exists() else None


def _save_audit(audit: AuditReport, data_dir: Path) -> None:
    p = data_dir / "audit" / audit.id / "audit.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(audit.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(p)


def _save_repair(rr: RepairRun, data_dir: Path) -> None:
    d = data_dir / "repairs"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rr.id}.json").write_text(rr.model_dump_json(indent=2), encoding="utf-8")


def runtime_record(providers: ProviderBundle) -> dict[str, Any]:
    digests = {}
    for rel in PROMPT_SOURCES:
        f = REPO_ROOT / rel
        digests[rel] = hashlib.sha256(f.read_bytes()).hexdigest()[:16] if f.exists() else None
    try:
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "--", *PROMPT_SOURCES], cwd=REPO_ROOT, capture_output=True, text=True, timeout=5, check=False).stdout.strip())
    except OSError:
        dirty = None
    probe, planner = providers.probe, providers.planner
    return {
        "runtime_version": RUNTIME_VERSION, "git_commit": git_commit(), "prompt_sources_uncommitted_changes": dirty, "prompt_source_sha256_16": digests,
        "reviewer": None if probe is None else {"provider": probe.capability.name, "model": probe.capability.model, "reasoning_effort": getattr(probe, "reasoning_effort", None)},
        "selector": None if planner is None else {"provider": planner.capability.name, "model": planner.capability.model, "reasoning_effort": getattr(planner, "reasoning_effort", None)},
        "decision_rules": "decide_next_action (deterministic, no model call)", "renderer": "ffmpeg from typed, validated edit plans",
        "asr": "whisper.cpp large-v3-turbo q5_0 (local)", "state": "data/runs/<run_id>.json, data/audit/<audit_id>/audit.json, data/repairs/<repair_id>.json, data/audit/lessons.jsonl",
    }


def mocked_stages(providers: ProviderBundle) -> list[str]:
    out = []
    for role, p in (("cold review, diagnosis, comparison", providers.probe), ("repair selection", providers.planner)):
        if p is not None and (p.capability.name in ("fake", "mock", "scripted") or getattr(p, "is_fake", False)):
            out.append(f"{role}: scripted test provider {p.capability.name}")
    return out


# ----------------------------------------------------------------------------- memory


@traced("update_memory", kind="tool")
def update_memory(data_dir: Path, run_id: str, config: RunConfig, finding: AuditFinding, rr: RepairRun, features: dict[str, Any]) -> dict[str, Any]:
    comp = rr.comparison
    lesson: dict[str, Any] = {
        "lesson_id": new_id("lesson"), "run_id": run_id, "repair_run_id": rr.id, "video_id": config.video_id, "category": config.category, "created_at": utc_now_iso(),
        "video_characteristics": features,
        "finding": {"issue_type": finding.issue_type, "objective": finding.objective, "severity": finding.severity, "uncertainty": finding.uncertainty,
                    "interval_ms": [finding.start_ms, finding.end_ms], "weakness": finding.weakness},
        "repair": {"route": rr.repair.route, "mutation_type": rr.repair.mutation_type, "edit": rr.repair.edit_description, "selection_reason": rr.repair.selection_reason},
        "result": {"status": rr.status, "outcome": comp.outcome if comp else None, "target_resolved": comp.target_resolved if comp else None,
                   "improved": comp.improved if comp else [], "regressed": comp.regressed if comp else [], "incomplete_reasons": rr.incomplete_reasons},
        "uncertainty": {"stability": comp.stability.model_dump() if comp and comp.stability else None,
                        "change_verified": rr.change_verification.verified if rr.change_verification else None},
        "applies_when": f"{config.category}; issue {finding.issue_type}; hook {features.get('hook_type')}; flattened MP4 with burned-in captions",
        "evidence_class": "model_eval",
        "label": "MODEL JUDGMENT lesson from one offline repair; not audience data",
    }
    policy_update = None
    mtype = POLICY_TYPE.get(rr.repair.mutation_type or "")
    outcome = {"improvement": "win", "regression": "loss", "mixed": "neutral", "tie": "neutral"}.get(comp.outcome) if comp else None
    if mtype is not None and outcome is not None and rr.status in ("accepted", "rejected"):
        path = data_dir / "creative" / "policy.json"
        policy = load_policy(path)
        strat = policy.record_outcome(mtype, config.category, StrategyEvidence(
            experiment_id=rr.id, video_id=config.video_id, arm_id=rr.candidate_version_id or "", outcome=outcome, evidence_class="model_eval",
            primary_metric="fresh cold-audience audit plus target and whole-video pairwise comparison", arm_value=comp.full_preference if comp else None,
            note=f"audit repair ({rr.repair.mutation_type}) for {finding.issue_type}: {rr.repair.edit_description[:140]}"))
        save_policy(policy, path)
        policy_update = {"strategy": strat.id, "status": strat.status, "wins": strat.wins, "losses": strat.losses, "neutral": strat.neutral, "policy_version": policy.version}
    lesson["policy_update"] = policy_update
    (data_dir / "audit").mkdir(parents=True, exist_ok=True)
    with open(data_dir / "audit" / "lessons.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(lesson) + "\n")
    return lesson


# ----------------------------------------------------------------------------- the loop


def validate_input(config: RunConfig) -> str | None:
    p = Path(config.video_path)
    if not p.is_file():
        return f"video file not found: {p.name}"
    try:
        info = inspect_media(p)
    except Exception as exc:  # noqa: BLE001
        return f"not a readable media file: {str(exc)[:160]}"
    if not info.width or not info.height:
        return "the file has no video stream; a cold-audience audit needs pictures (audio-only or transcript-only input is refused)"
    if not info.duration_ms or info.duration_ms < 1500:
        return "the video is shorter than 1.5 seconds"
    if info.duration_ms > MAX_DURATION_MS:
        return f"the video is longer than {MAX_DURATION_MS // 1000} seconds; this runtime audits short-form video only"
    return None


@traced("directorloop.run", kind="agent")
def run_director(config: RunConfig, providers: ProviderBundle, data_dir: Path, on_stage: Any = None, run_id: str | None = None, launched_via: str = "python") -> DirectorRun:
    def emit(stage: str, msg: str, data: dict | None = None) -> None:
        if on_stage:
            on_stage(stage, msg, data)

    set_display_name(f"run_{config.video_id}_budget{config.iteration_budget}")
    started = time.monotonic()
    run = DirectorRun(id=run_id or new_id("run"), config=config, created_at=utc_now_iso(), original_path=config.video_path, launched_via=launched_via,
                      runtime=runtime_record(providers), mocked_stages=mocked_stages(providers), manual_interventions=[])
    run.weave_call_id, run.weave_url = current_call_ref()
    save_run(run, data_dir)
    emit("STARTED", f"run {run.id}: {config.video_id}, budget {config.iteration_budget}, objective: {config.objective[:160]}",
         {"run_id": run.id, "weave_url": run.weave_url})

    def finish(status: str, stop_reason: str, error: str | None = None) -> DirectorRun:
        run.status, run.stop_reason, run.error = status, stop_reason, error  # type: ignore[assignment]
        run.ended_at = utc_now_iso()
        run.timings_ms["total_ms"] = int((time.monotonic() - started) * 1000)
        changed = bool(run.final_artifact_hash) and run.final_artifact_hash != run.original_artifact_hash
        attempted = [i for i in run.iterations if i.finding_id]
        if changed:
            run.final_decision = f"keep {run.final_version_id}: {sum(1 for i in attempted if i.decision == 'accept')} accepted repair(s) are in {Path(run.final_path).name}"
        elif attempted:
            run.final_decision = f"retain the original: none of {len(attempted)} rendered repair attempt(s) demonstrated an improvement"
        else:
            run.final_decision = "retain the original: no repair was attempted (see the stop reason)"
        save_run(run, data_dir)
        emit("DONE" if status == "completed" else "FAILED", f"run {run.id}: {run.final_decision}. Stop reason: {stop_reason}",
             {"run_id": run.id, "weave_url": run.weave_url, "final_version_id": run.final_version_id})
        return run

    problem = validate_input(config)
    if problem:
        return finish("failed", f"input refused: {problem}", error=problem)
    if providers.probe is None or providers.planner is None:
        return finish("failed", "no vision reviewer or no text selector is configured", error="providers missing")

    video_path = Path(config.video_path)
    run.original_artifact_hash = run.final_artifact_hash = sha256_file(video_path)
    run.final_path = str(video_path)
    project_id = f"proj_{config.video_id}"
    current_path, current_version, current_hash = video_path, "v0", run.original_artifact_hash

    emit("AUDITING", "cold-audience audit of the original (unprimed: no objective, notes or labels)")
    audit = run_audit(video_id=config.video_id, video_path=current_path, version_id=current_version, provider=providers.probe, data_dir=data_dir, on_stage=on_stage)
    run.audit_ids.append(audit.id)
    save_run(run, data_dir)
    if audit.status != "complete":
        return finish("completed", "the audit of the original was incomplete (" + "; ".join(audit.incomplete_reasons) + "); no repair is attempted on partial evidence")

    source_project = inspect_source_project(video_path)
    tried: dict[str, set[str]] = {}
    attempts: dict[str, int] = {}
    attempts_log: list[str] = []
    iteration = 0
    while True:
        budget_left = config.iteration_budget - iteration
        if budget_left <= 0:
            return finish("completed", f"iteration budget of {config.iteration_budget} used")
        t_it = time.monotonic()
        genome = extract_genome(current_path, providers.probe, data_dir / "creative" / "genomes", category=config.category)
        policy = load_policy(data_dir / "creative" / "policy.json")
        emit("SELECTING", f"iteration {iteration + 1}: mapping {len(audit.findings)} finding(s) of {current_version} to repairs that can be executed")
        considered: list[ConsideredFinding] = []
        options: list[tuple[AuditFinding, RepairCandidate, float]] = []
        open_findings = [f for f in audit.findings if attempts.get(f.id, 0) < config.max_attempts_per_finding]

        def select(f: AuditFinding, _genome: Any = genome, _path: Path = current_path, _hash: str = current_hash) -> tuple[Any, RepairCandidate | None]:
            return map_repair(f, _genome, _path, project_id, providers.planner, source_project,
                              exclude_keys=tuple(sorted(tried.get(_hash, set()))), prior_attempts=tuple(attempts_log[-6:]),
                              constraints=tuple(config.constraints), objective=config.objective,
                              allowed_types=tuple(config.allowed_edits) if config.allowed_edits is not None else None)

        with weave.ThreadPoolExecutor(max_workers=max(1, min(4, len(open_findings)))) as ex:
            mapped = dict(zip([f.id for f in open_findings], ex.map(select, open_findings), strict=True))
        for f in audit.findings:
            cf_ = ConsideredFinding(finding_id=f.id, interval_ms=(f.start_ms, f.end_ms), issue_type=f.issue_type, objective=f.objective, severity=f.severity,
                                    uncertainty=f.uncertainty, weakness=f.weakness[:240])
            if f.id not in mapped:
                cf_.skipped = f"{attempts[f.id]} attempts on this finding already failed (limit {config.max_attempts_per_finding})"
                considered.append(cf_)
                continue
            repair, cand = mapped[f.id]
            f.repair = repair
            cf_.route, cf_.runnable, cf_.edit, cf_.selection_reason, cf_.why_not_runnable = repair.route, repair.runnable, repair.edit_description, repair.selection_reason, repair.why_not_runnable
            considered.append(cf_)
            if cand is not None:
                strat = policy.lookup(POLICY_TYPE[cand.mutation_type], config.category)
                options.append((f, cand, strat.evidence_mean() if strat else 0.5))
        _save_audit(audit, data_dir)
        if not options:
            if not audit.findings:
                reason = f"the audit of {current_version} found no weaknesses to repair"
            else:
                needs = [f"{c.interval_ms[0] / 1000:.1f}-{c.interval_ms[1] / 1000:.1f}s " + (c.skipped or f"route {c.route}: {c.why_not_runnable}") for c in considered]
                reason = "no runnable existing-video repair remains: " + " | ".join(needs[:4])
            run.iterations.append(IterationRecord(index=iteration + 1, current_version_id=current_version, current_artifact_hash=current_hash, audit_id=audit.id,
                                                  considered=considered, decision="stop", reason=reason, next_action="stop", next_action_reason=reason))
            save_run(run, data_dir)
            return finish("completed", reason)

        ranked = rank_options(options, config.focus, attempts)
        finding, cand, ranking_reason = ranked[0]
        iteration += 1
        tried.setdefault(current_hash, set()).add(cand.key)
        attempts[finding.id] = attempts.get(finding.id, 0) + 1
        rec = IterationRecord(index=iteration, current_version_id=current_version, current_artifact_hash=current_hash, audit_id=audit.id, considered=considered,
                              finding_id=finding.id, finding=finding.weakness[:300], ranking_reason=ranking_reason, selection_reason=finding.repair.selection_reason if finding.repair else "",
                              edit=cand.description, mutation_type=cand.mutation_type, decision="incomplete_keep_current", reason="", next_action="stop")
        emit("REPAIRING", f"iteration {iteration}: {cand.description} | for {finding.start_ms / 1000:.1f}-{finding.end_ms / 1000:.1f}s: {finding.weakness[:140]}")
        base_plan = identity_plan(genome)
        manifest = source_manifest(project_id, current_path, genome.artifact_hash)
        beat = next((b for b in genome.beats if cand.ops and getattr(cand.ops[0], "segment_id", None) == b.id), None)
        rr = RepairRun(id=new_id("repair"), audit_id=audit.id, finding_id=finding.id, video_id=config.video_id, status="proposed",
                       hypothesis=f"{cand.description} should address: {finding.weakness}", repair=finding.repair, original_artifact_hash=current_hash,  # type: ignore[arg-type]
                       original_path=str(current_path), created_at=utc_now_iso())
        _save_repair(rr, data_dir)
        rec.repair_run_id = rr.id
        t = time.monotonic()
        rend: dict[str, Any] | None = None
        try:
            rend = render_candidate(cand.plan, manifest, current_path, data_dir)
        except Exception as exc:  # noqa: BLE001 - a failed render is a recorded outcome, not a crash
            rr.status, rr.incomplete_reasons = "incomplete", [f"render failed: {str(exc)[:200]}"]
        render_ms = int((time.monotonic() - t) * 1000)
        verified: bool | None = None
        labels_match: bool | None = None
        cand_audit: AuditReport | None = None
        if rend is not None:
            rr.candidate_path, rr.candidate_artifact_hash, rr.status = rend["path"], rend["artifact_hash"], "rendered"
            rr.candidate_version_id = f"v{iteration}-{rend['artifact_hash'][:8]}"
            rr.interval_map = interval_mappings(cand.plan, audit.findings)
            rec.candidate_version_id, rec.candidate_artifact_hash, rec.candidate_path = rr.candidate_version_id, rr.candidate_artifact_hash, rr.candidate_path
            rr.change_verification = verify_change(cand.mutation_type, cand.description, current_path, Path(rend["path"]), base_plan, cand.plan,
                                                   beat.text if beat else None, (finding.start_ms, finding.end_ms), (beat.start_ms, beat.end_ms) if beat else None)
            verified = rr.change_verification.verified
            rec.change_verified, rec.verification_checks = verified, rr.change_verification.checks
            emit("VERIFYING", "intended change " + ("verified" if verified else "NOT verified") + ": " + "; ".join(rr.change_verification.checks))
            if not verified:
                rr.status, rr.incomplete_reasons = "incomplete", ["the rendered file does not show the intended change"]
        fresh_ms = compare_ms = 0
        if verified:
            t = time.monotonic()
            emit("FRESH_REVIEW", "fresh cold-audience audit of the candidate (no access to the finding, the repair, the objective or the earlier verdict)")
            cand_audit = run_audit(video_id=config.video_id, video_path=Path(rend["path"]), version_id=rr.candidate_version_id or "candidate",  # type: ignore[index]
                                   provider=providers.probe, data_dir=data_dir, on_stage=on_stage)
            fresh_ms = int((time.monotonic() - t) * 1000)
            run.audit_ids.append(cand_audit.id)
            rr.candidate_audit_id, rr.status = cand_audit.id, "reviewed"
            rec.candidate_audit_id = cand_audit.id
            labels_match = cand_audit.artifact_hash == rend["artifact_hash"] and audit.artifact_hash == current_hash  # type: ignore[index]
            if not labels_match:
                rr.status, rr.incomplete_reasons = "incomplete", ["audited file hashes do not match the version labels"]
            elif cand_audit.status != "complete":
                rr.status, rr.incomplete_reasons = "incomplete", ["fresh audit incomplete: " + "; ".join(cand_audit.incomplete_reasons)]
            else:
                t = time.monotonic()
                emit("COMPARING", "target moment, whole video (both presentation orders), persistence of the weakness, new weaknesses, audience predictions, protected content")
                protected = list(dict.fromkeys(finding.keep_unchanged + config.constraints))[:8]
                rr.comparison = compare_versions(provider=providers.probe, original=audit, candidate=cand_audit, finding=finding, candidate_plan=cand.plan, protected_items=protected)
                compare_ms = int((time.monotonic() - t) * 1000)
                rec.outcome, rec.target_resolved = rr.comparison.outcome, rr.comparison.target_resolved
                rec.improved, rec.regressed = rr.comparison.improved[:8], rr.comparison.regressed[:8]
        d = decide_next_action(rendered=rend is not None, verified=verified, labels_match=labels_match,
                               candidate_audit_complete=None if cand_audit is None else cand_audit.status == "complete",
                               outcome=rr.comparison.outcome if rr.comparison else None, target_resolved=rr.comparison.target_resolved if rr.comparison else None,
                               regressed=rr.comparison.regressed if rr.comparison else [], budget_left=config.iteration_budget - iteration)
        rec.decision, rec.reason, rec.next_action, rec.next_action_reason = d["decision"], d["reason"], d["next_action"], d["next_action_reason"]  # type: ignore[assignment]
        if d["decision"] == "accept":
            rr.status = "accepted"
        elif d["decision"] == "reject_keep_current":
            rr.status = "rejected"
        elif not rr.incomplete_reasons:
            rr.status, rr.incomplete_reasons = "incomplete", [d["reason"]]
        rr.timings_ms = {"render_ms": render_ms, "fresh_review_ms": fresh_ms, "compare_ms": compare_ms}
        features = {"duration_ms": genome.duration_ms, "hook_type": genome.hook.hook_type.value, "beats": len(genome.beats), "shots": len(genome.shots),
                    "longest_static_span_ms": genome.longest_static_span_ms.value, "first_payoff_ms": genome.first_payoff_ms.value}
        lesson = update_memory(data_dir, run.id, config, finding, rr, features)
        rr.lesson_id = rec.lesson_id = lesson["lesson_id"]
        _save_repair(rr, data_dir)
        run.repair_run_ids.append(rr.id)
        run.lessons.append(lesson)
        result = rr.comparison.outcome if rr.comparison else rr.status
        attempts_log.append(f"{cand.key} ({cand.description}) for the weakness '{finding.weakness[:90]}' -> {result}: {d['reason'][:200]}")
        rec.timings_ms = {**rr.timings_ms, "iteration_ms": int((time.monotonic() - t_it) * 1000)}
        run.iterations.append(rec)
        emit("DECIDED", f"iteration {iteration}: {str(result).upper()} -> {d['decision']}: {d['reason'][:220]} | next: {d['next_action']}",
             {"repair_run_id": rr.id, "decision": d["decision"]})
        if d["decision"] == "accept" and cand_audit is not None and rend is not None:
            current_path, current_version, current_hash = Path(rend["path"]), rr.candidate_version_id or current_version, rend["artifact_hash"]
            audit = cand_audit
            run.final_version_id, run.final_artifact_hash, run.final_path = current_version, current_hash, str(current_path)
        save_run(run, data_dir)
