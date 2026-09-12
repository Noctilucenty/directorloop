"""Build and evaluate the ordinary one-shot baseline for a pack."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from ..domain.ids import new_id
from ..domain.pack import DemoPack
from ..evals import evaluate_version, run_no_media_control
from ..media import RenderError, render_plan, validate_plan
from ..observability.weave_ops import current_call_ref, set_display_name, traced
from ..planning import analyze_asset_coverage, declared_coverage
from ..providers.registry import ProviderBundle
from .state import ProjectState, VersionRecord, state_path

StageCallback = Callable[[str, str, dict | None], None]


def _emit(cb: StageCallback | None, stage: str, message: str, data: dict | None = None) -> None:
    if cb:
        cb(stage, message, data)


@traced("build_baseline", kind="agent")
def build_baseline(
    *,
    pack: DemoPack,
    state: ProjectState,
    providers: ProviderBundle,
    data_dir: Path,
    trials: int,
    allow_cache: bool = True,
    run_leakage_control: bool = True,
    on_stage: StageCallback | None = None,
    job_id: str | None = None,
) -> VersionRecord:
    set_display_name(f"build_baseline_{pack.project_id}")
    asset_paths = {a.id: pack.asset_path(a) for a in pack.manifest.assets}
    cache_dir = data_dir / "cache" / pack.project_id
    timings: dict[str, int] = {}

    _emit(on_stage, "PREFLIGHT", "validating baseline plan against the brief and manifest")
    vr = validate_plan(pack.baseline_plan, pack.manifest, pack.brief)
    if not vr.ok:
        raise RenderError("baseline plan invalid: " + "; ".join(vr.errors))

    _emit(on_stage, "RENDERING", "rendering the baseline with FFmpeg")
    res = render_plan(pack.baseline_plan, pack.manifest, asset_paths, publish_dir=data_dir / "renders", work_dir=data_dir / "work")
    timings["render_ms"] = res.render_ms + res.verify_ms
    existing = state.baseline()
    if existing and existing.artifact_hash == res.artifact_hash and existing.plan_hash == pack.baseline_plan.content_hash():
        version = existing
        _emit(on_stage, "RENDERING", f"baseline unchanged (artifact {res.artifact_hash[:12]}); reusing V0 record")
    else:
        version = VersionRecord(
            id=new_id("ver"),
            project_id=pack.project_id,
            index=0 if existing is None else state.next_index(),
            parent_version_id=None,
            role="baseline",
            status="baseline",
            plan=pack.baseline_plan,
            plan_hash=pack.baseline_plan.content_hash(),
            artifact_hash=res.artifact_hash,
            artifact_path=str(res.path),
            duration_ms=res.duration_ms,
            width=res.width,
            height=res.height,
            job_id=job_id,
            render_ms=res.render_ms,
        )
        state.versions.append(version)
        state.best_version_id = version.id
    _emit(on_stage, "RENDERING", f"baseline ready in {timings['render_ms']} ms: {res.path.name[:16]}", {"artifact_hash": res.artifact_hash, "duration_ms": res.duration_ms})

    if providers.probe is None:
        raise RuntimeError("no probe provider configured; cannot evaluate the baseline")

    _emit(on_stage, "ANALYZING", "checking which claims each clip visibly shows (cached per asset hash)")
    t1 = time.monotonic()
    if state.coverage is None or state.coverage_model != providers.probe.capability.model:
        try:
            state.coverage = analyze_asset_coverage(pack.manifest, asset_paths, pack.truth, providers.probe, cache_dir)
            state.coverage_model = providers.probe.capability.model
        except Exception as exc:  # noqa: BLE001
            state.coverage = declared_coverage(pack.manifest, pack.truth)
            state.coverage_model = None
            _emit(on_stage, "ANALYZING", f"coverage analysis failed; using creator-declared coverage only ({str(exc)[:80]})")
    timings["coverage_ms"] = int((time.monotonic() - t1) * 1000)

    _emit(on_stage, "EVALUATING", f"evaluating the baseline file with {providers.probe.capability.name}:{providers.probe.capability.model}, {trials} trials")
    t2 = time.monotonic()
    run = evaluate_version(
        version_id=version.id,
        version_label=version.label,
        artifact_path=res.path,
        artifact_hash=res.artifact_hash,
        plan=pack.baseline_plan,
        baseline_plan=None,
        manifest=pack.manifest,
        brief=pack.brief,
        suite=pack.suite,
        provider=providers.probe,
        trials=trials,
        cache_dir=cache_dir,
        allow_cache=allow_cache,
        approved_texts=[c.text for c in pack.truth.claims],
    )
    timings["evaluate_ms"] = int((time.monotonic() - t2) * 1000)
    state.evaluations[run.id] = run
    version.evaluation_run_id = run.id
    call_id, url = current_call_ref()
    version.weave_url = run.weave_url or url
    _emit(
        on_stage,
        "EVALUATING",
        f"baseline: {run.questions_passed}/{run.questions_total} probe questions pass ({run.mode}, {run.probe_modality}), "
        f"mechanical {'pass' if run.mechanical_passed else 'FAIL'}, constraints {'pass' if run.constraints_passed else 'FAIL'}",
        {"evaluation_run_id": run.id, "questions_passed": run.questions_passed, "questions_total": run.questions_total, "mode": run.mode},
    )

    if run_leakage_control and state.leakage is None:
        _emit(on_stage, "EVALUATING", "no-media control: can the model answer without the video?")
        try:
            state.leakage = run_no_media_control(providers.probe, pack.suite, trials=min(3, trials))
            weak = state.leakage.weak_question_ids
            _emit(on_stage, "EVALUATING", f"no-media control done: {len(weak)} weak question(s)" + (f" ({', '.join(weak)})" if weak else ""))
        except Exception as exc:  # noqa: BLE001
            _emit(on_stage, "EVALUATING", f"no-media control failed: {str(exc)[:100]}")
    state.save(state_path(data_dir, pack.project_id))
    _emit(on_stage, "DONE", "baseline stored", {"version_id": version.id, "timings_ms": timings})
    return version
