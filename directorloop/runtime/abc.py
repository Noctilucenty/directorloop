"""A/B-to-C: two edits of the same idea -> independent cold audits -> beat-aligned comparison -> a directed C -> real render
-> verification -> fresh audit of C -> consistent comparison of C against A and B -> accept, reject or report uncertainty.

Order of operations matters for honesty:
1. The rubric (dimensions, questions, sampling, verdict rules, declared context, models) is frozen and hashed first.
2. A and B are re-rendered through the same pipeline C will use, so every reviewer compares first-generation encodes.
3. A and B are audited independently: neither reviewer sees the other edit, the objective or any comparison.
4. The comparison and both audits drive the choice of C among executable, validated options only.
5. C is verified as rendered (duration, loudness envelope, picture provenance, words at their planned places) before any review.
6. C is audited fresh and judged against A and B with the frozen rubric; the decision rule is deterministic (decide_c).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import weave
from pydantic import BaseModel, ConfigDict, Field

from ..audit.models import AuditReport
from ..audit.review import run_audit, words_from_transcript
from ..audit.revise import check_protected
from ..compare.align import align_beats, text_similarity
from ..compare.judge import (
    CORE_DIMENSIONS,
    WHOLE_DIMENSIONS,
    compare_pair,
    region_media,
    rubric_record,
    whole_media,
)
from ..compare.models import (
    ABComparison,
    ABCRun,
    CAttempt,
    CEvaluation,
    COption,
    CProposal,
    DeclaredContext,
    PairComparison,
    VersionRef,
)
from ..compare.recombine import (
    ASSET_ID,
    VersionMaterial,
    build_c_options,
    timeline_spans,
    verify_c_render,
    version_plan,
)
from ..creative.genome import GENOME_VERSION, extract_genome
from ..creative.signals import audio_rms
from ..domain.ids import new_id, sha256_file, utc_now_iso
from ..media import render_plan
from ..media.probe import inspect_media
from ..media.transcribe import transcribe
from ..observability.weave_ops import current_call_ref, set_display_name, traced
from ..providers.base import ProviderError
from ..providers.registry import ProviderBundle
from .budget import BudgetedProvider, BudgetExceeded, CallBudget
from .director import mocked_stages, runtime_record, validate_video_path


class ABCConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    a_video_id: str = Field(pattern=r"^[a-z0-9_\-]{2,80}$")
    a_path: str
    b_video_id: str = Field(pattern=r"^[a-z0-9_\-]{2,80}$")
    b_path: str
    context: DeclaredContext
    constraints: list[str] = Field(default_factory=list, max_length=12)
    iteration_budget: int = Field(default=2, ge=1, le=5)
    max_model_calls: int | None = Field(default=260, ge=20, le=2000)
    deadline_s: int | None = Field(default=1500, ge=60, le=7200)
    category: str = "educational_short"


def abc_dir(data_dir: Path) -> Path:
    return data_dir / "abc"


def save_abc(run: ABCRun, data_dir: Path) -> None:
    d = abc_dir(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f"{run.id}.json.tmp"
    tmp.write_text(run.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(d / f"{run.id}.json")


def load_abc(data_dir: Path, abc_id: str) -> ABCRun | None:
    f = abc_dir(data_dir) / f"{abc_id}.json"
    return ABCRun.model_validate_json(f.read_text(encoding="utf-8")) if f.exists() else None


# ----------------------------------------------------------------------------- decisions (pure)


@traced("decide_c", kind="tool")
def decide_c(*, vs_base: PairComparison | None, vs_other: PairComparison | None, target: PairComparison | None, target_dimensions: list[str], base_label: str,
             other_label: str, protected: list[dict[str, str]], new_weaknesses: list[str]) -> dict[str, Any]:
    """Deterministic outcome for C from the frozen-rubric comparisons. Returns outcome, reason and the dimension lists."""
    improved, regressed, unchanged, uncertain = [], [], [], []
    if vs_base is None:
        return {"outcome": "insufficient_evidence", "reason": "C could not be compared with its base", "improved": [], "regressed": [], "unchanged": [], "uncertain": []}
    for d in vs_base.dimensions:
        text = f"{d.dimension} vs {base_label}"
        if d.verdict == "C":
            improved.append(text)
        elif d.verdict == base_label:
            regressed.append(text)
        elif d.verdict == "same":
            unchanged.append(text)
        else:
            uncertain.append(f"{text}: {d.verdict}")
    if vs_other is not None:
        for d in vs_other.dimensions:
            if d.verdict == other_label:
                regressed.append(f"{d.dimension} vs {other_label}")
            elif d.verdict == "C":
                improved.append(f"{d.dimension} vs {other_label}")
    lost = [f"{p.get('kind', 'item')} {p.get('status')}: {p.get('item')}" for p in protected if p.get("status") in ("lost", "violated")]
    regressed += lost
    regressed += [f"new weakness: {w}" for w in new_weaknesses]
    overall = vs_base.overall.verdict
    target_hits = [d for d in target_dimensions if vs_base.verdict(d) == "C"]
    target_losses = [d for d in target_dimensions if vs_base.verdict(d) == base_label]
    if target is not None and target.overall.verdict == "C":
        target_hits.append("the changed stretch (overall)")
    if target is not None and target.overall.verdict == base_label:
        target_losses.append("the changed stretch (overall)")
    core_losses = [d for d in CORE_DIMENSIONS if vs_base.verdict(d) == base_label]
    other_over = vs_other is not None and vs_other.overall.verdict == other_label
    all_unclear = overall in ("unstable", "unclear") and all(vs_base.verdict(d) in ("unstable", "unclear") for d in target_dimensions) and \
        (target is None or target.overall.verdict in ("unstable", "unclear"))
    if lost:
        outcome, reason = "regression", "protected content or a constraint was broken: " + "; ".join(lost[:3])
    elif overall == base_label:
        outcome, reason = "regression", f"{base_label} was preferred over C as a whole in both presentation orders"
    elif core_losses and not target_hits:
        outcome, reason = "regression", f"C lost on {', '.join(core_losses)} without gaining on its target"
    elif target_hits and not target_losses and not core_losses and not new_weaknesses and overall in ("C", "same") and not other_over:
        outcome, reason = "improvement", f"C gained on its target ({', '.join(target_hits)}) with no loss on comprehension or payoff, no new weakness, and was not beaten by {other_label}"
    elif target_hits:
        why = []
        if target_losses:
            why.append("lost on " + ", ".join(target_losses))
        if core_losses:
            why.append("lost on " + ", ".join(core_losses))
        if new_weaknesses:
            why.append(f"{len(new_weaknesses)} new weakness(es)")
        if overall not in ("C", "same"):
            why.append(f"overall vs {base_label} was {overall}")
        if other_over:
            why.append(f"{other_label} was preferred over C")
        outcome, reason = "mixed", f"C gained on {', '.join(target_hits)} but " + "; ".join(why)
    elif all_unclear:
        outcome, reason = "insufficient_evidence", "the presentation orders disagreed or the judge could not tell on the target and overall"
    else:
        outcome, reason = "tie", f"no reliable gain on the target dimensions ({', '.join(target_dimensions)}); overall vs {base_label}: {overall}"
    return {"outcome": outcome, "reason": reason, "improved": improved, "regressed": regressed, "unchanged": unchanged, "uncertain": uncertain}


# ----------------------------------------------------------------------------- selection


SELECT_SYSTEM = ("You direct a third version (C) of a short video from two existing edits (A and B) of the same idea. You may only choose one of "
                 "the executable options listed. Choose the smallest change that the evidence says should help the declared viewer and objective, "
                 "keep what already works, and name the tradeoffs bluntly, including what C will lose. Model judgments in the evidence are predictions, not audience data. Choosing "
                 "nothing is acceptable when no option is supported by the evidence.")

SELECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "choice": {"type": ["string", "null"]},
        "target_dimensions": {"type": "array", "items": {"type": "string", "enum": list(WHOLE_DIMENSIONS)}},
        "expected_improvement": {"type": "string"},
        "protected_strengths": {"type": "array", "items": {"type": "object", "properties": {"source": {"type": "string", "enum": ["A", "B"]}, "item": {"type": "string"}}, "required": ["source", "item"]}},
        "tradeoffs": {"type": "array", "items": {"type": "string"}},
        "why_smallest": {"type": "string"},
        "reason": {"type": "string"},
        "needed_capability": {"type": "string"},
    },
    "required": ["choice", "target_dimensions", "expected_improvement", "protected_strengths", "tradeoffs", "why_smallest", "reason", "needed_capability"],
}


def _fmt_dims(pc: PairComparison | None) -> str:
    if pc is None:
        return "(not available)"
    lines = []
    for d in pc.dimensions + [pc.overall]:
        reason = next((r for r in d.reasons if "call failed" not in r), "")
        lines.append(f"  {d.dimension}: {d.verdict} (votes {d.votes}) {reason[:150]}")
    return "\n".join(lines)


@traced("select_c", kind="agent")
def select_c(planner: Any, config: ABCConfig, comparison: ABComparison, audits: dict[str, AuditReport], options: list[tuple[COption, Any]],
             prior_attempts: list[str]) -> tuple[CProposal | None, str]:
    ctx = config.context
    units = "\n".join(
        f"part {u.index + 1} [{u.kind}{'' if u.similarity is None else f', wording similarity {u.similarity:.2f}'}] "
        f"A {'-' if u.a_start_ms is None else f'{u.a_start_ms / 1000:.1f}-{u.a_end_ms / 1000:.1f}s'}: \"{u.text_a[:140]}\" | "
        f"B {'-' if u.b_start_ms is None else f'{u.b_start_ms / 1000:.1f}-{u.b_end_ms / 1000:.1f}s'}: \"{u.text_b[:140]}\""
        for u in comparison.alignment)

    def audit_lines(label: str) -> str:
        a = audits.get(label)
        if a is None:
            return f"{label}: no audit"
        f_lines = [f"  weakness {f.start_ms / 1000:.1f}-{f.end_ms / 1000:.1f}s [{f.issue_type}, {f.objective}, severity {f.severity}, uncertainty {f.uncertainty}]: {f.weakness[:200]}"
                   for f in a.findings] or ["  (no weaknesses found)"]
        s_lines = [f"  strength {s.start_ms / 1000:.1f}-{s.end_ms / 1000:.1f}s: {s.what[:160]} ({s.why[:120]})" for s in a.strengths] or ["  (no strengths listed)"]
        return f"{label} audit summary: {a.overall_summary[:300]}\n" + "\n".join(f_lines + s_lines)

    regions = "\n".join(f"part {int((r.region or {}).get('unit_index', -1)) + 1}:\n{_fmt_dims(r)}" for r in comparison.regions) or "(no per-part comparisons)"
    opts = "\n".join(f"- {o.key}: {o.description} | changes: {'; '.join(o.changes)} | side effects: {'; '.join(o.side_effects)} | duration {o.duration_ms / 1000:.1f}s"
                     for o, _ in options)
    user = (
        f"Declared context, the same for A, B and C: {ctx.creative_type}; viewer: {ctx.audience}; objective: {ctx.objective}; "
        f"expected payoff: {ctx.expected_payoff or 'not stated'}; encounter: {ctx.encounter}.\n"
        + (f"Constraints: {'; '.join(config.constraints)}\n" if config.constraints else "")
        + (f"Comparability notes: {'; '.join(comparison.confounds)}\n" if comparison.confounds else "")
        + f"\nStory parts aligned between A and B:\n{units}\n\n"
        f"Independent cold-audience audits (neither reviewer saw the other edit):\n{audit_lines('A')}\n{audit_lines('B')}\n\n"
        f"A vs B, whole video, both presentation orders ('unstable' = the orders disagreed):\n{_fmt_dims(comparison.whole)}\n\n"
        f"A vs B by part:\n{regions}\n\n"
        + ("Earlier C attempts in this run and what the evaluation found:\n" + "\n".join(f"- {p}" for p in prior_attempts) + "\n"
           "Do not repeat a change that already failed unless the evidence says the failure was unrelated to it.\n\n" if prior_attempts else "")
        + f"Executable options (validated against both source files; nothing else can be rendered):\n{opts}\n\n"
        "Pick one option key or null. Give 1-3 target_dimensions this change should improve, the specific strengths of A or B that must survive in C, "
        "the tradeoffs, and why this is the smallest defensible change. If null, name the capability a better C would need."
    )
    try:
        res = planner.complete_json(SELECT_SYSTEM, user, SELECT_SCHEMA)
    except ProviderError as exc:
        return None, f"the selector failed: {str(exc)[:160]}"
    d = res.data
    chosen = next(((o, p) for o, p in options if o.key == d.get("choice")), None)
    if chosen is None and d.get("choice"):
        return None, f"the selector chose '{str(d.get('choice'))[:60]}', which is not an available option, so no C was rendered: " + str(d.get("reason", ""))[:240]
    if chosen is None:
        return None, "no C chosen: " + str(d.get("reason", ""))[:300] + (f" Needed: {d.get('needed_capability')}" if d.get("needed_capability") else "")
    opt = chosen[0]
    dims = [x for x in d.get("target_dimensions", []) if x in WHOLE_DIMENSIONS][:3]
    note = "" if dims else " (the selector named no valid target dimension; the evaluation uses the overall verdict only)"
    return CProposal(option_key=opt.key, base=opt.base, donor=opt.donor, description=opt.description, target_dimensions=dims,
                     expected_improvement=str(d.get("expected_improvement", ""))[:400],
                     protected_strengths=[{"source": str(x.get("source")), "item": str(x.get("item", ""))[:200]} for x in d.get("protected_strengths", [])
                                          if isinstance(x, dict) and x.get("source") in ("A", "B")][:6],
                     tradeoffs=[str(x)[:200] for x in d.get("tradeoffs", [])][:5], why_smallest=str(d.get("why_smallest", ""))[:300],
                     selector_reason=str(d.get("reason", ""))[:500] + note, options_offered=[o.key for o, _ in options]), ""


# ----------------------------------------------------------------------------- the run


@traced("render_c", kind="tool")
def render_c(plan: Any, manifest: Any, materials: dict[str, VersionMaterial], data_dir: Path) -> dict[str, Any]:
    r = render_plan(plan, manifest, {ASSET_ID[k]: m.path for k, m in materials.items()}, publish_dir=data_dir / "renders", work_dir=data_dir / "work")
    return {"path": str(r.path), "artifact_hash": r.artifact_hash, "duration_ms": r.duration_ms, "render_ms": r.render_ms + r.verify_ms}


def _new_weaknesses(c_audit: AuditReport, plan: Any, audits: dict[str, AuditReport], changed_region_ms: tuple[int, int]) -> tuple[list[str], list[str]]:
    """Medium/high findings in C, split into (new weaknesses, reviewer variance).

    A finding counts as new only if it touches what the edit changed (the changed stretch or a join between sources) and the
    source moment carried no finding in the version it came from. A finding that lies entirely on untouched material is the
    fresh reviewer disagreeing with the earlier one about identical content: it is reported as uncertainty, never as a
    regression caused by the edit."""
    spans = timeline_spans(plan)
    by_asset = {v: k for k, v in ASSET_ID.items()}
    joins = [spans[s.id][0] for i, s in enumerate(plan.segments) if i > 0 and
             not (s.asset_id == plan.segments[i - 1].asset_id and s.source_in_ms == plan.segments[i - 1].source_out_ms)]
    new, variance = [], []
    for f in c_audit.findings:
        if f.severity not in ("medium", "high"):
            continue
        sources = []
        for s in plan.segments:
            t0, t1 = spans[s.id]
            a, b = max(f.start_ms, t0), min(f.end_ms, t1)
            if b > a:
                sources.append((by_asset[s.asset_id], s.source_in_ms + (a - t0), s.source_in_ms + (b - t0)))
        at_join = any(f.start_ms - 250 <= j <= f.end_ms + 250 for j in joins)
        in_changed = max(f.start_ms, changed_region_ms[0]) < min(f.end_ms, changed_region_ms[1])
        known = any(any(max(g.start_ms, s0) < min(g.end_ms, s1) for g in audits[lab].findings) for lab, s0, s1 in sources if lab in audits)
        text = f"{f.start_ms / 1000:.1f}-{f.end_ms / 1000:.1f}s [{f.issue_type}, {f.severity}]{' at a join' if at_join else ''}: {f.weakness[:160]}"
        if known:
            continue
        if at_join or in_changed:
            new.append(text)
        else:
            variance.append(f"reviewer variance on unchanged material (the earlier audit did not flag this identical content): {text}")
    return new, variance


@traced("directorloop.abc_run", kind="agent")
def run_abc(config: ABCConfig, providers: ProviderBundle, data_dir: Path, on_stage: Any = None, abc_id: str | None = None, launched_via: str = "python") -> ABCRun:
    def emit(stage: str, msg: str, data: dict | None = None) -> None:
        if on_stage:
            on_stage(stage, msg, data)

    set_display_name(f"abc_{config.a_video_id}_vs_{config.b_video_id}")
    started = time.monotonic()
    budget = CallBudget(config.max_model_calls, config.deadline_s)
    probe = BudgetedProvider(providers.probe, budget) if providers.probe is not None else None
    planner = BudgetedProvider(providers.planner, budget) if providers.planner is not None else None
    reviewer = f"{providers.probe.capability.name}:{providers.probe.capability.model}" if providers.probe else "none"
    selector = f"{providers.planner.capability.name}:{providers.planner.capability.model}" if providers.planner else "none"
    run = ABCRun(id=abc_id or new_id("abc"), created_at=utc_now_iso(), context=config.context, constraints=config.constraints,
                 limits={"iteration_budget": config.iteration_budget, "max_model_calls": config.max_model_calls, "deadline_s": config.deadline_s},
                 rubric=rubric_record(config.context, reviewer, selector), runtime=runtime_record(providers), mocked_stages=mocked_stages(providers),
                 launched_via=launched_via)
    run.weave_call_id, run.weave_url = current_call_ref()
    save_abc(run, data_dir)
    emit("STARTED", f"A/B-to-C run {run.id}: rubric {run.rubric['version']} frozen (sha256 {run.rubric['sha256'][:12]})", {"abc_id": run.id, "weave_url": run.weave_url})

    def finish(status: str, stop_reason: str, error: str | None = None) -> ABCRun:
        run.status, run.stop_reason, run.error = status, stop_reason, error  # type: ignore[assignment]
        run.ended_at = utc_now_iso()
        run.usage = budget.summary()
        run.timings_ms["total_ms"] = int((time.monotonic() - started) * 1000)
        accepted = next((a for a in run.attempts if a.decision == "accept"), None)
        if accepted is not None:
            run.final_version = "C"
            run.final_decision = f"keep C: {accepted.reason}"
        elif run.comparison is not None and run.comparison.best_supported:
            run.final_version = run.comparison.best_supported
            run.final_decision = (f"keep {run.comparison.best_supported}: no C demonstrated an improvement; {run.comparison.best_supported} was preferred over "
                                  f"{'B' if run.comparison.best_supported == 'A' else 'A'} as a whole")
        else:
            run.final_version = ""
            run.final_decision = "keep both inputs: no C demonstrated an improvement and A vs B gave no reliable overall preference"
        save_abc(run, data_dir)
        emit("DONE" if status == "completed" else "FAILED", f"{run.id}: {run.final_decision}. Stop reason: {stop_reason}",
             {"abc_id": run.id, "weave_url": run.weave_url, "final_version": run.final_version})
        return run

    for label, path in (("A", config.a_path), ("B", config.b_path)):
        problem = validate_video_path(Path(path))
        if problem:
            return finish("failed", f"input {label} refused: {problem}", error=problem)
    if probe is None or planner is None:
        return finish("failed", "no vision reviewer or no text selector is configured", error="providers missing")

    try:
        # ---- materials: genomes, same-pipeline renders, words, loudness --------------------------------------------------
        emit("PREPARING", "sentence beats for A and B; re-rendering both through the pipeline C will use")
        mats: dict[str, VersionMaterial] = {}
        genome_dir = data_dir / "creative" / "genomes"

        def prepare(label: str) -> VersionMaterial:
            src = Path(config.a_path if label == "A" else config.b_path)
            src_hash = sha256_file(src)
            cached = any(genome_dir.glob(f"genome_{src_hash[:24]}_{GENOME_VERSION}_*.json"))
            genome = extract_genome(src, probe, genome_dir, category=config.category)
            info = inspect_media(src)
            m = VersionMaterial(label=label, path=src, artifact_hash=src_hash, genome=genome, words=words_from_transcript(transcribe(src)), audit=None,
                                width=int(info.width or 0), height=int(info.height or 0), duration_ms=int(info.duration_ms or 0), rms=audio_rms(src))
            m.genome_cached = cached  # type: ignore[attr-defined]
            return m

        with weave.ThreadPoolExecutor(max_workers=2) as ex:
            for m in ex.map(prepare, ["A", "B"]):
                mats[m.label] = m
        from ..compare.recombine import output_profile, two_asset_manifest

        out_profile = output_profile(mats["A"], mats["B"])
        manifest = two_asset_manifest(f"abc_{run.id}", mats["A"], mats["B"])
        evaluated: dict[str, dict[str, Any]] = {}
        for label in ("A", "B"):
            evaluated[label] = render_c(version_plan(mats[label], out_profile), manifest, mats, data_dir)
            run.versions[label] = VersionRef(label=label, video_id=config.a_video_id if label == "A" else config.b_video_id, role="input",  # type: ignore[arg-type]
                                             original_path=str(mats[label].path), original_hash=mats[label].artifact_hash, evaluated_path=evaluated[label]["path"],
                                             evaluated_hash=evaluated[label]["artifact_hash"], duration_ms=evaluated[label]["duration_ms"],
                                             genome_cached=getattr(mats[label], "genome_cached", None),
                                             notes=["evaluated file is the original re-rendered through the same pipeline as C (identical timeline)"])
        save_abc(run, data_dir)

        # ---- independent audits ---------------------------------------------------------------------------------------------
        emit("AUDITING", "independent cold-audience audits of A and B (neither reviewer sees the other edit or the objective)")
        audits: dict[str, AuditReport] = {}

        def audit(label: str) -> AuditReport:
            return run_audit(video_id=run.versions[label].video_id, video_path=Path(evaluated[label]["path"]), version_id=label, provider=probe, data_dir=data_dir)

        with weave.ThreadPoolExecutor(max_workers=2) as ex:
            for label, rep in zip(["A", "B"], ex.map(audit, ["A", "B"]), strict=True):
                audits[label] = rep
                mats[label].audit = rep
                run.versions[label].audit_id, run.versions[label].audit_status = rep.id, rep.status
        save_abc(run, data_dir)
        emit("AUDITS_DONE", f"A: {len(audits['A'].findings)} findings, {len(audits['A'].strengths)} strengths; B: {len(audits['B'].findings)} findings, "
             f"{len(audits['B'].strengths)} strengths", {"audit_ids": {k: v.id for k, v in audits.items()}})
        incomplete = [k for k, v in audits.items() if v.status != "complete"]
        if incomplete:
            return finish("completed", f"the audit of {', '.join(incomplete)} was incomplete; A and B are not compared on partial evidence")

        # ---- alignment and A vs B comparison ----------------------------------------------------------------------------------
        emit("ALIGNING", "aligning equivalent story parts of A and B")
        units = align_beats(mats["A"].genome.beats, mats["B"].genome.beats)
        sim = text_similarity(" ".join(w.text for w in mats["A"].words), " ".join(w.text for w in mats["B"].words))
        confounds = []
        if sim < 0.6:
            confounds.append(f"the narration differs substantially (wording similarity {sim:.2f}); differences may come from content, not editing")
        if (mats["A"].width, mats["A"].height) != (mats["B"].width, mats["B"].height):
            confounds.append("the two edits have different frame sizes; both are scaled to the same output")
        comparison = ABComparison(comparable=sim >= 0.6, confounds=confounds, transcript_similarity=round(sim, 3), alignment=units)
        run.comparison = comparison
        emit("COMPARING_AB", f"{len(units)} aligned parts (wording similarity {sim:.2f}); whole-video comparison in both orders")
        a_media = whole_media(Path(evaluated["A"]["path"]), evaluated["A"]["duration_ms"], mats["A"].words)
        b_media = whole_media(Path(evaluated["B"]["path"]), evaluated["B"]["duration_ms"], mats["B"].words)
        comparison.whole = compare_pair(probe, "A", a_media, "B", b_media, config.context, "whole")
        differing = [u for u in units if u.a_start_ms is not None and u.b_start_ms is not None
                     and (u.kind != "shared" or abs((u.a_end_ms - u.a_start_ms) - (u.b_end_ms - u.b_start_ms)) > 300)][:4]  # type: ignore[operator]

        def region(u: Any) -> PairComparison:
            ra = region_media(Path(evaluated["A"]["path"]), u.a_start_ms, u.a_end_ms, mats["A"].words)
            rb = region_media(Path(evaluated["B"]["path"]), u.b_start_ms, u.b_end_ms, mats["B"].words)
            return compare_pair(probe, "A", ra, "B", rb, config.context, "region", region={"unit_index": u.index, "a_ms": [u.a_start_ms, u.a_end_ms], "b_ms": [u.b_start_ms, u.b_end_ms]})

        with weave.ThreadPoolExecutor(max_workers=4) as ex:
            comparison.regions = list(ex.map(region, differing))
        for label in ("A", "B"):
            for f in audits[label].findings:
                for u in units:
                    s0, s1 = (u.a_start_ms, u.a_end_ms) if label == "A" else (u.b_start_ms, u.b_end_ms)
                    if s0 is not None and max(f.start_ms, s0) < min(f.end_ms, s1):  # type: ignore[type-var]
                        comparison.findings_by_unit.setdefault(f"{label}:{u.index}", []).append(f"{f.start_ms / 1000:.1f}-{f.end_ms / 1000:.1f}s {f.weakness[:140]}")
            for st in audits[label].strengths:
                for u in units:
                    s0, s1 = (u.a_start_ms, u.a_end_ms) if label == "A" else (u.b_start_ms, u.b_end_ms)
                    if s0 is not None and max(st.start_ms, s0) < min(st.end_ms, s1):  # type: ignore[type-var]
                        comparison.strengths_by_unit.setdefault(f"{label}:{u.index}", []).append(f"{st.what[:140]}")
        ov = comparison.whole.overall.verdict
        if ov in ("A", "B"):
            comparison.best_supported, comparison.best_supported_reason = ov, f"{ov} was preferred as a whole in both presentation orders"
        else:
            comparison.best_supported_reason = f"no reliable overall preference between A and B ({ov})"
        save_abc(run, data_dir)
        emit("COMPARED_AB", "A vs B: " + ", ".join(f"{d.dimension} {d.verdict}" for d in comparison.whole.dimensions) + f"; overall {ov}")

        # ---- C attempts -----------------------------------------------------------------------------------------------------------
        exclude: set[str] = set()
        prior: list[str] = []
        for i in range(config.iteration_budget):
            t_it = time.monotonic()
            left = config.iteration_budget - i - 1
            emit("DIRECTING", f"attempt {i + 1}: building executable C options and choosing one")
            options, manifest, out_profile = build_c_options(mats, units, f"abc_{run.id}", config.context.objective, exclude)
            if not options:
                run.attempts.append(CAttempt(index=i + 1, decision="stop", reason="no executable C option remains", next_action="stop", next_action_reason="no executable C option remains"))
                return finish("completed", "no executable C option remains")
            proposal, why_none = select_c(planner, config, comparison, audits, options, prior)
            if proposal is None:
                run.attempts.append(CAttempt(index=i + 1, decision="stop", reason=why_none, next_action="stop", next_action_reason=why_none))
                return finish("completed", why_none)
            opt, plan = next((o, p) for o, p in options if o.key == proposal.option_key)
            exclude.add(opt.key)
            att = CAttempt(index=i + 1, proposal=proposal, decision="incomplete", reason="", next_action="stop")
            run.attempts.append(att)
            save_abc(run, data_dir)
            emit("RENDERING_C", f"attempt {i + 1}: {opt.description}", {"option": opt.key, "target_dimensions": proposal.target_dimensions})
            t = time.monotonic()
            try:
                rend = render_c(plan, manifest, mats, data_dir)
            except Exception as exc:  # noqa: BLE001
                att.reason = f"render failed: {str(exc)[:200]}"
                att.next_action, att.next_action_reason = ("try_another_c", "another option may still render") if left else ("stop", "attempt budget used")
                prior.append(f"{opt.key}: render failed")
                continue
            att.render_path, att.render_hash = rend["path"], rend["artifact_hash"]
            att.version_id = f"C{i + 1}-{rend['artifact_hash'][:8]}"
            evaluation = CEvaluation()
            att.evaluation = evaluation
            evaluation.change_verification = verify_c_render(opt, plan, Path(rend["path"]), mats)
            render_ms = int((time.monotonic() - t) * 1000)
            emit("VERIFYING_C", ("C implements the plan: " if evaluation.change_verification.verified else "C does NOT implement the plan: ")
                 + "; ".join(evaluation.change_verification.checks))
            if not evaluation.change_verification.verified:
                att.reason = "the rendered C does not implement the planned change, so it is not reviewed"
                att.next_action, att.next_action_reason = ("try_another_c", "another option may render correctly") if left else ("stop", "attempt budget used")
                prior.append(f"{opt.key}: render did not match the plan")
                save_abc(run, data_dir)
                continue
            t = time.monotonic()
            emit("REVIEWING_C", "fresh cold-audience audit of C (no access to A, B, the comparison, the proposal or the objective)")
            c_audit = run_audit(video_id=f"{config.a_video_id}-{config.b_video_id}-c", video_path=Path(rend["path"]), version_id=att.version_id, provider=probe, data_dir=data_dir)
            evaluation.candidate_audit_id, evaluation.candidate_audit_status = c_audit.id, c_audit.status
            run.versions[f"C{i + 1}"] = VersionRef(label="C", video_id=att.version_id.lower(), role="director", evaluated_path=rend["path"], evaluated_hash=rend["artifact_hash"],
                                                   duration_ms=rend["duration_ms"], audit_id=c_audit.id, audit_status=c_audit.status,
                                                   notes=[f"rendered from the original A and B files by plan option {opt.key}"])
            if c_audit.status != "complete":
                att.reason = "the fresh audit of C was incomplete; C is not judged on partial evidence"
                att.next_action, att.next_action_reason = ("try_another_c", "retry with another option") if left else ("stop", "attempt budget used")
                prior.append(f"{opt.key}: fresh audit incomplete")
                save_abc(run, data_dir)
                continue
            emit("JUDGING_C", f"C vs {opt.base} and C vs {'B' if opt.base == 'A' else 'A'} with the frozen rubric, both presentation orders; the changed stretch; protected strengths")
            base_label, other_label = opt.base, "B" if opt.base == "A" else "A"
            from ..compare.recombine import planned_words_multi

            c_words = planned_words_multi(plan, {ASSET_ID[k]: m.words for k, m in mats.items()})
            c_media = whole_media(Path(rend["path"]), rend["duration_ms"], c_words)
            media = {"A": a_media, "B": b_media}
            by_source: dict[str, list[str]] = {}
            for ps in proposal.protected_strengths:
                by_source.setdefault(ps["source"], []).append(ps["item"])
            by_source[base_label] = by_source.get(base_label, []) + list(config.constraints)
            results = judge_c(probe, config.context, c_media, media, base_label, other_label, Path(rend["path"]), c_words, opt,
                              Path(evaluated[base_label]["path"]), mats[base_label].words, {k: v for k, v in by_source.items() if v})
            evaluation.vs_base, evaluation.vs_other, evaluation.target_region = results["vs_base"], results["vs_other"], results["target"]
            evaluation.protected = results["protected"]
            evaluation.new_weaknesses, variance = _new_weaknesses(c_audit, plan, audits, opt.candidate_region_ms)
            verdict = decide_c(vs_base=evaluation.vs_base, vs_other=evaluation.vs_other, target=evaluation.target_region, target_dimensions=proposal.target_dimensions,
                               base_label=base_label, other_label=other_label, protected=evaluation.protected, new_weaknesses=evaluation.new_weaknesses)
            evaluation.outcome, evaluation.outcome_reason = verdict["outcome"], verdict["reason"]
            evaluation.improved, evaluation.regressed, evaluation.unchanged = verdict["improved"], verdict["regressed"], verdict["unchanged"]
            evaluation.uncertain = verdict["uncertain"] + variance
            att.decision = "accept" if verdict["outcome"] == "improvement" else "reject_keep_inputs"
            att.reason = verdict["reason"]
            if att.decision == "accept":
                att.next_action, att.next_action_reason = "stop", "C demonstrated an improvement under the frozen rubric"
            elif left:
                att.next_action, att.next_action_reason = "try_another_c", "the failed option is excluded and its result is given to the selector"
            else:
                att.next_action, att.next_action_reason = "stop", "attempt budget used"
            att.timings_ms = {"render_and_verify_ms": render_ms, "review_and_judge_ms": int((time.monotonic() - t) * 1000), "attempt_ms": int((time.monotonic() - t_it) * 1000)}
            lesson = _record_lesson(data_dir, run, config, opt, proposal, evaluation)
            att.lesson_id = lesson["lesson_id"]
            prior.append(f"{opt.key} ({opt.description[:120]}) targeting {', '.join(proposal.target_dimensions) or 'overall'} -> {verdict['outcome']}: {verdict['reason'][:200]}")
            save_abc(run, data_dir)
            emit("DECIDED_C", f"attempt {i + 1}: {verdict['outcome'].upper()} -> {att.decision}: {verdict['reason'][:240]}", {"attempt": i + 1, "decision": att.decision})
            if att.decision == "accept":
                return finish("completed", "C accepted")
        return finish("completed", f"attempt budget of {config.iteration_budget} used")
    except BudgetExceeded as exc:
        return finish("completed", f"stopped by a run limit: {exc}")


def judge_c(probe: Any, context: DeclaredContext, c_media: Any, media: dict[str, Any], base_label: str, other_label: str, c_path: Path, c_words: list[Any],
            opt: COption, base_eval_path: Path, base_words: list[Any], protected_by_source: dict[str, list[str]]) -> dict[str, Any]:
    """C against its base and the other input (whole video), the changed stretch against the base, and protected items, concurrently."""
    def vs(label: str) -> PairComparison:
        return compare_pair(probe, "C", c_media, label, media[label], context, "whole")

    def target() -> PairComparison:
        return compare_pair(probe, "C", region_media(c_path, opt.candidate_region_ms[0], opt.candidate_region_ms[1], c_words), base_label,
                            region_media(base_eval_path, opt.base_region_ms[0], opt.base_region_ms[1], base_words), context, "region",
                            region={"c_ms": list(opt.candidate_region_ms), f"{base_label.lower()}_ms": list(opt.base_region_ms)})

    def protected(src: str) -> list[dict[str, str]]:
        return [dict(x, source=src) for x in check_protected(probe, media[src], c_media, protected_by_source[src][:8])]

    with weave.ThreadPoolExecutor(max_workers=3 + len(protected_by_source)) as ex:
        f_base, f_other, f_target = ex.submit(vs, base_label), ex.submit(vs, other_label), ex.submit(target)
        f_prot = [ex.submit(protected, src) for src in protected_by_source]
        return {"vs_base": f_base.result(), "vs_other": f_other.result(), "target": f_target.result(), "protected": [x for f in f_prot for x in f.result()]}


@traced("record_lesson", kind="tool")
def _record_lesson(data_dir: Path, run: ABCRun, config: ABCConfig, opt: COption, proposal: CProposal, ev: CEvaluation) -> dict[str, Any]:
    plain = {
        "improvement": f"{opt.description} improved {', '.join(proposal.target_dimensions) or 'the video'} for this viewer without a detected regression.",
        "regression": f"{opt.description} made the video worse under the rubric ({ev.outcome_reason}).",
        "mixed": f"{opt.description} helped in one respect but cost another ({ev.outcome_reason}).",
        "tie": f"{opt.description} made no reliable difference ({ev.outcome_reason}).",
        "insufficient_evidence": f"The evaluation could not tell whether {opt.description} helped ({ev.outcome_reason}).",
    }[ev.outcome]
    lesson = {
        "lesson_id": new_id("lesson"), "abc_id": run.id, "created_at": utc_now_iso(), "plain": plain,
        "context": config.context.model_dump(), "intervention": {"kind": opt.kind, "base": opt.base, "donor": opt.donor, "key": opt.key, "changes": opt.changes},
        "target_dimensions": proposal.target_dimensions, "outcome": ev.outcome, "reason": ev.outcome_reason, "improved": ev.improved, "regressed": ev.regressed,
        "uncertain": ev.uncertain, "change_verified": ev.change_verification.verified if ev.change_verification else None,
        "applies_when": f"{config.context.creative_type}; {config.context.encounter}; {opt.kind}",
        "label": "MODEL JUDGMENT lesson from one A/B/C comparison: tentative and local, not audience data",
    }
    abc_dir(data_dir).mkdir(parents=True, exist_ok=True)
    with open(abc_dir(data_dir) / "lessons.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(lesson) + "\n")
    return lesson
