"""Reproducible historical evaluation with a durable prediction-before-label gate.

Acquisition identities and outcomes remain outside the content evaluator. A run
freezes the admitted cohort and implementation, persists every prediction, then
verifies the sealed log before opening the outcome file. Failed predictions never
silently shrink the cohort. Mechanical-only runs make no model-accuracy claim.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..config import get_settings
from ..domain.ids import new_id, sha256_file, utc_now_iso
from ..observability.weave_ops import current_call_ref, flush, init_weave, set_display_name, traced
from ..observability.workflow import WorkflowStage, attach_workflow, workflow_session, workflow_stage
from ..runtime.budget import BudgetedProvider, CallBudget
from .calibration import evaluate_systems, frozen_split
from .features import canonical_hash, extract_mechanical_features
from .metrics import finite, paired_metrics, retention_drop_windows, temporal_overlap
from .prediction import append_prediction, build_prediction_record

VERSION = "instagram-blind-backtest-v1"
TARGETS = ("views", "reach", "avg_watch_seconds", "skip_rate", "saves_per_1000_reach", "shares_per_1000_reach")
IDENTITY_KEYS = ("media_id", "instagram_post_id", "account_id", "artifact_sha256")
METHODS = {"mechanical_only": ["mechanical_attention_screen_v1"],
           "mechanical_and_model": ["mechanical_attention_screen_v1", "current_directorloop_v1", "model_qualitative_v1"]}


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def implementation_hashes() -> dict[str, str]:
    root = Path(__file__).parent
    names = ("runner.py", "features.py", "prediction.py", "calibration.py", "metrics.py", "../audit/review.py",
             "../audit/attention.py", "../audit/models.py", "../media/transcribe.py", "../creative/signals.py")
    return {name: sha256_file(root / name) for name in names}


class BacktestRun(BaseModel):
    id: str
    version: str = VERSION
    created_at: str = Field(default_factory=utc_now_iso)
    status: str = "running"
    mode: str
    selected_posts: int = 0
    admitted_posts: int = 0
    persisted_predictions: int = 0
    outcomes_unblinded: bool = False
    blockers: list[str] = Field(default_factory=list)
    excluded: list[dict] = Field(default_factory=list)
    weave_call_id: str | None = None
    weave_url: str | None = None
    budget: dict = Field(default_factory=dict)
    results_path: str | None = None
    workflow_stages: list[WorkflowStage] = Field(default_factory=list)


def save_run(run: BacktestRun, data_dir: Path) -> None:
    write_json(data_dir / "backtests" / run.id / "run.json", run.model_dump(mode="json"))


def admit_cohort(protocol: dict, manifest: list[dict]) -> tuple[list[dict], list[dict]]:
    """Validate post/byte receipts without consulting performance or titles."""
    selected = {r["media_id"]: r for r in protocol["selected_observations"]}
    if len(selected) != len(protocol["selected_observations"]):
        raise ValueError("Protocol contains duplicate selected posts")
    supplied = {r["media_id"]: r for r in manifest}
    if len(supplied) != len(manifest):
        raise ValueError("Manifest contains duplicate identities")
    admitted, excluded = [], []
    for media_id, metadata in selected.items():
        if not re.fullmatch(r"[a-z0-9_-]{2,80}", media_id):
            raise ValueError("Unsafe media identity")
        row = supplied.get(media_id)
        if row is None:
            excluded.append({"media_id": media_id, "reason": "complete published media not acquired"})
            continue
        try:
            if any(row.get(k) != metadata.get(k) for k in ("instagram_post_id", "account_id")):
                raise ValueError("post or account identity mismatch")
            source = Path(row["local_path"])
            if not source.is_file() or sha256_file(source) != row["artifact_sha256"]:
                raise ValueError("media hash mismatch")
            receipt_path = Path(row["provenance_path"])
            if sha256_file(receipt_path) != row["provenance_sha256"]:
                raise ValueError("acquisition receipt hash mismatch")
            receipt = json.loads(receipt_path.read_text())
            if any(receipt.get(k) != row[k] for k in IDENTITY_KEYS):
                raise ValueError("acquisition receipt identity mismatch")
            if receipt.get("source_kind") not in ("platform_served", "verified_uploaded_bytes"):
                raise ValueError("local master is not verified published content")
            if not all(receipt.get(k) is True for k in ("identity_verified", "full_decode_passed", "audio_video_complete")):
                raise ValueError("incomplete media acquisition evidence")
        except (ValueError, KeyError, OSError) as exc:
            # Errors contain field names only; never echo acquisition URLs or raw files.
            excluded.append({"media_id": media_id, "reason": str(exc) if isinstance(exc, ValueError) else type(exc).__name__})
            continue
        admitted.append({**{k: metadata[k] for k in ("media_id", "instagram_post_id", "account_id", "posted_at", "observation_id")},
                         "artifact_sha256": row["artifact_sha256"], "local_path": str(source.resolve()),
                         "provenance_sha256": row["provenance_sha256"]})
    hashes = [r["artifact_sha256"] for r in admitted]
    if len(hashes) != len(set(hashes)):
        raise ValueError("Exact content duplicates must be grouped before freezing a cohort")
    return admitted, excluded


def read_predictions(path: Path) -> list[dict]:
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    for row in records:
        body = {k: v for k, v in row.items() if k not in ("created_at", "prediction_sha256")}
        if canonical_hash(body) != row.get("prediction_sha256"):
            raise ValueError("Prediction body hash mismatch")
    return records


def seal_predictions(path: Path, frozen: dict) -> dict:
    records = read_predictions(path)
    expected = {(r["media_id"], r["artifact_sha256"], m) for r in frozen["cohort"] for m in frozen["methods"]}
    actual = [(r["media_id"], r["artifact_sha256"], r["method_version"]) for r in records]
    if not expected or len(actual) != len(set(actual)) or set(actual) != expected:
        raise ValueError("Every frozen media/method prediction must be persisted exactly once before unblinding")
    return {"sealed_at": utc_now_iso(), "prediction_file_sha256": sha256_file(path),
            "frozen_sha256": canonical_hash(frozen), "prediction_count": len(records),
            "outcome_file_sha256": frozen["outcome_file_sha256"], "gate": "all predictions persisted and read-back verified"}


def unblind_metrics(path: Path, predictions_path: Path, frozen: dict, seal: dict) -> list[dict]:
    """The only outcome-file read in this module; the gate fails before opening it."""
    current = seal_predictions(predictions_path, frozen)
    for key in ("prediction_file_sha256", "frozen_sha256", "prediction_count", "outcome_file_sha256"):
        if seal.get(key) != current[key]:
            raise ValueError("Prediction seal changed; refuse to unblind")
    if implementation_hashes() != frozen["implementation_hashes"]:
        raise ValueError("Implementation changed after the prediction freeze")
    if sha256_file(path) != seal["outcome_file_sha256"]:
        raise ValueError("Outcome source changed after preregistration")
    all_rows = json.loads(path.read_text())
    by_observation = {}
    for row in all_rows:
        key = row["observation_id"]
        if key in by_observation:
            raise ValueError("Ambiguous duplicate outcome observation")
        by_observation[key] = row
    joined = []
    for media in frozen["cohort"]:
        row = by_observation.get(media["observation_id"])
        if row is None or any(str(row.get(k)) != str(media[k]) for k in ("media_id", "instagram_post_id", "account_id", "posted_at")):
            raise ValueError("Frozen outcome observation is missing or has a different identity")
        # Explicit metric allowlist. Captions, titles, tokens and previous good/bad labels never enter features.
        actual = {k: row.get(k) for k in ("views", "reach", "avg_watch_seconds", "skip_rate", "completion_rate", "likes", "comments", "saves", "shares", "followers_at_posting")}
        for key, value in actual.items():
            if value is not None and (not finite(value) or value < 0 or key in ("skip_rate", "completion_rate") and value > 1):
                raise ValueError(f"Invalid actual metric: {key}")
        for key in ("saves", "shares"):
            actual[key + "_per_1000_reach"] = actual[key] * 1000 / actual["reach"] if finite(actual[key]) and finite(actual["reach"]) and actual["reach"] > 0 else None
        joined.append({**{k: media[k] for k in IDENTITY_KEYS}, **actual,
                       "observation_id": media["observation_id"], "posted_at": media["posted_at"],
                       "collected_at": row.get("collected_at"), "age_hours": row.get("age_hours"),
                       "metric_windows": row.get("metric_windows"), "source_kind": row.get("source_kind"),
                       "retention_points": row.get("retention_points") or [],
                       "retention_provenance": row.get("retention_provenance")})
    return joined


def compare_predictions(records: list[dict], actuals: list[dict]) -> dict:
    """Rank scores are compared as ranks, never as views or measured probabilities."""
    actual_by_id = {r["media_id"]: r for r in actuals}
    methods = sorted({r["method_version"] for r in records})
    ranking, window_results = {}, []
    for method in methods:
        selected = [r for r in records if r["method_version"] == method]
        ranking[method] = {}
        for target in TARGETS:
            # A higher content index predicts lower skip, and higher remaining outcomes.
            direction = -1 if target == "skip_rate" else 1
            metrics = paired_metrics([direction * r["ranking_score"] if finite(r.get("ranking_score")) else None for r in selected],
                                     [actual_by_id[r["media_id"]].get(target) for r in selected])
            ranking[method][target] = {k: metrics[k] for k in ("n", "spearman", "pairwise_accuracy", "pairwise_n")}
        for prediction in selected:
            actual = actual_by_id[prediction["media_id"]]
            observed = retention_drop_windows(actual["retention_points"], top_k=1)
            candidates = [w for w in prediction["window_predictions"] if finite(w.get("risk_index"))]
            top = sorted(candidates, key=lambda w: (-w["risk_index"], w["start_ms"]))[:1]
            if not observed or not top:
                continue
            overlap = temporal_overlap([(w["start_ms"], w["end_ms"]) for w in top], [(w["start_ms"], w["end_ms"]) for w in observed])
            window_results.append({"media_id": prediction["media_id"], "method_version": method, **overlap,
                                   "source_kind": actual["source_kind"], "retention_provenance": actual["retention_provenance"],
                                   "interpretation": "Ordinal risk window versus largest observed decline per second; not a per-viewer exit prediction."})
    return {"uncalibrated_rank_associations": ranking, "timestamp_window_comparison": {"n": len({r["media_id"] for r in window_results}), "rows": window_results},
            "interpretation": "Rank associations use the admitted cohort descriptively. Only the separate frozen test split supports held-out error estimates."}


def error_analysis(evaluations: dict) -> dict:
    """Expose counterexamples without tuning on their held-out labels."""
    output = {}
    for target, report in evaluations.items():
        output[target] = {}
        for method, result in report["systems"].items():
            rows = [r for r in result.get("rows", []) if finite(r.get("predicted")) and finite(r.get("actual"))]
            errors = [{"media_id": r["media_id"], "actual": r["actual"], "predicted": r["predicted"], "signed_error": r["predicted"] - r["actual"]} for r in rows]
            output[target][method] = {"n": len(rows), "largest_overestimates": sorted([r for r in errors if r["signed_error"] > 0], key=lambda r: -r["signed_error"])[:3],
                                      "largest_underestimates": sorted([r for r in errors if r["signed_error"] < 0], key=lambda r: r["signed_error"])[:3]}
    return {"results": output, "used_to_refit": False,
            "limitations": ["Error cases generate future hypotheses only; they do not identify causes.", "No calibration or policy is promoted automatically."]}


@traced("directorloop.instagram_backtest", kind="agent", display="Instagram backtest: predict, seal, compare",
        summarize=lambda r: {"id": r.id, "status": r.status, "mode": r.mode, "selected_posts": r.selected_posts,
                             "admitted_posts": r.admitted_posts, "persisted_predictions": r.persisted_predictions,
                             "outcomes_unblinded": r.outcomes_unblinded, "blockers": r.blockers, "budget": r.budget})
@workflow_session(persist=save_run)
def run_backtest(*, protocol_path: Path, media_manifest_path: Path, outcomes_path: Path, outcomes_sha256: str,
                 data_dir: Path, mode: str = "mechanical_only", providers: Any = None,
                 max_model_calls: int = 120, deadline_s: int = 1800, on_stage: Any = None) -> BacktestRun:
    if mode not in METHODS:
        raise ValueError("Unknown prediction mode")
    if max_model_calls < 1 or deadline_s < 1 or not re.fullmatch(r"[a-f0-9]{64}", outcomes_sha256):
        raise ValueError("Positive execution limits and a SHA256 outcome-source receipt are required")
    run = BacktestRun(id=new_id("backtest"), mode=mode)
    run.weave_call_id, run.weave_url = current_call_ref()
    attach_workflow(run)
    out = data_dir / "backtests" / run.id
    out.mkdir(parents=True, exist_ok=False)
    budget = CallBudget(max_calls=max_model_calls, deadline_s=deadline_s)
    try:
        with workflow_stage("backtest_ingest", "Verify published media and freeze the cohort") as stage:
            protocol_receipt = protocol_path.with_suffix(".sha256")
            if protocol_receipt.exists() and protocol_receipt.read_text().strip().split()[0] != sha256_file(protocol_path):
                raise ValueError("Preregistered protocol hash mismatch")
            protocol = json.loads(protocol_path.read_text())
            manifest = json.loads(media_manifest_path.read_text())
            cohort, run.excluded = admit_cohort(protocol, manifest)
            run.selected_posts, run.admitted_posts = len(protocol["selected_observations"]), len(cohort)
            settings = get_settings()
            frozen = {"version": VERSION, "frozen_at": utc_now_iso(), "protocol_sha256": sha256_file(protocol_path),
                      "manifest_sha256": sha256_file(media_manifest_path), "outcome_file_sha256": outcomes_sha256,
                      "implementation_hashes": implementation_hashes(), "cohort": cohort, "excluded": run.excluded,
                      "methods": METHODS[mode], "split": frozen_split(cohort), "mode": mode,
                      "mechanical_asr_language": "auto", "model_asr_language": settings.dl_asr_language if mode == "mechanical_and_model" else None,
                      "model_calls_allowed": mode == "mechanical_and_model",
                      "model_configuration": {"provider": settings.dl_probe_provider, "model": settings.dl_probe_model,
                                              "precision_enabled": settings.dl_attention_precision_enabled,
                                              "coarse_window_ms": settings.dl_attention_coarse_window_ms,
                                              "precision_window_ms": settings.dl_attention_precision_window_ms} if mode == "mechanical_and_model" else None}
            frozen["package_versions"] = {name: package_version(name) for name in ("numpy", "pydantic", "weave", "openai")}
            for name, expected_hash in frozen["implementation_hashes"].items():
                source = (Path(__file__).parent / name).resolve()
                destination = out / "implementation" / source.relative_to(Path(__file__).resolve().parents[1])
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                if sha256_file(destination) != expected_hash:
                    raise ValueError("Implementation changed while creating its source snapshot")
            write_json(out / "frozen.json", frozen)
            stage.update(selected=run.selected_posts, admitted=run.admitted_posts, excluded=len(run.excluded),
                         frozen_sha256=canonical_hash(frozen), outcome_values_read=False)
            if not cohort:
                stage.status = "incomplete"
                run.status = "blocked"
                run.blockers.append("No complete published video has a verified post/byte receipt. Metrics remain blinded.")
        if not cohort:
            with workflow_stage("backtest_metrics_unblind", "Keep Instagram results blinded") as stage:
                stage.status = "skipped"
                stage.update(reason=run.blockers[0], observed_accuracy_n=0)
            return run
        if mode == "mechanical_and_model" and (providers is None or providers.probe is None or getattr(providers.probe, "is_fake", False)):
            raise ValueError("A real configured probe provider is required for model evaluation")
        probe = BudgetedProvider(providers.probe, budget) if mode == "mechanical_and_model" else None
        predictions_path = out / "predictions.jsonl"
        for media in cohort:
            budget.check_deadline()
            with workflow_stage("blind_video_audit", "Blind content review", kind="agent",
                                inputs={"media_id": media["media_id"], "artifact_sha256": media["artifact_sha256"], "mode": mode}) as review_stage:
                neutral = out / "media" / (media["media_id"] + ".mp4")
                neutral.parent.mkdir(exist_ok=True)
                shutil.copyfile(media["local_path"], neutral)
                if sha256_file(neutral) != media["artifact_sha256"]:
                    raise ValueError("Neutral media copy changed bytes")
                with workflow_stage("backtest_mechanical", "Measure frames, motion and local speech") as stage:
                    mechanical = extract_mechanical_features(neutral, media_id=media["media_id"], expected_sha256=media["artifact_sha256"],
                                                             asr_language="auto", cache_dir=out / "preprocessing")
                    write_json(out / "features" / (media["media_id"] + ".json"), mechanical)
                    stage.update(features=mechanical["features"], missingness=mechanical.get("missingness"), model_inference=False)
                audit = None
                with workflow_stage("backtest_model", "Chronological model review", kind="agent") as stage:
                    if probe is None:
                        stage.status = "skipped"
                        stage.update(reason="Declared mechanical-only run; no remote model called.")
                    else:
                        from ..audit.review import run_audit
                        audit = run_audit(video_id=media["media_id"], video_path=neutral, version_id="neutral", provider=probe, data_dir=out, on_stage=on_stage)
                        stage.update(audit_id=audit.id, status=audit.status)
                        if audit.status != "complete":
                            raise ValueError("Incomplete model review: keep the entire cohort blinded")
                with workflow_stage("backtest_prediction", "Register content predictions"):
                    methods = ["mechanical_v1"] + (["current_directorloop_v1", "model_qualitative_v1"] if audit else [])
                    pending = [build_prediction_record(media_id=media["media_id"], artifact_sha256=media["artifact_sha256"], mechanical=mechanical,
                                                       audit=audit if m != "mechanical_v1" else None, method=m, audit_blind_verified=audit is not None) for m in methods]
                with workflow_stage("backtest_prediction_persisted", "Save and verify predictions before results") as stage:
                    receipts = [append_prediction(predictions_path, row) for row in pending]
                    run.persisted_predictions += len(receipts)
                    stage.update(prediction_hashes=[r["prediction_sha256"] for r in receipts], fsync_and_readback_verified=True)
                review_stage.update(predictions_saved=len(pending), model_inference_present=audit is not None)
            run.budget = budget.summary()
            save_run(run, data_dir)
        with workflow_stage("backtest_seal", "Seal all predictions and the evaluation split") as stage:
            seal = seal_predictions(predictions_path, frozen)
            write_json(out / "prediction-seal.json", seal)
            stage.update(**seal)
        with workflow_stage("backtest_metrics_unblind", "Open the frozen Instagram observations") as stage:
            actuals = unblind_metrics(outcomes_path, predictions_path, frozen, seal)
            run.outcomes_unblinded = True
            write_json(out / "observations.json", actuals)
            stage.update(matched_posts=len(actuals), source_sha256=outcomes_sha256, protocol_sha256=frozen["protocol_sha256"])
        records = read_predictions(predictions_path)
        with workflow_stage("backtest_comparison", "Compare ranks and observed drop windows") as stage:
            comparison = compare_predictions(records, actuals)
            write_json(out / "comparisons.json", comparison)
            stage.update(timestamp_curve_posts=comparison["timestamp_window_comparison"]["n"], methods=METHODS[mode])
        by_id = {r["media_id"]: r for r in records}
        rows = [{**actual, "features": by_id[actual["media_id"]]["features"]} for actual in actuals]
        with workflow_stage("backtest_calibration", "Fit declared baselines on development data") as stage:
            evaluations = {target: evaluate_systems(rows, frozen["split"], target) for target in TARGETS}
            stage.update(split=frozen["split"], targets=TARGETS, ridge_alpha=1.0, automatic_promotion=False)
        with workflow_stage("backtest_heldout", "Record held-out errors and interval coverage") as stage:
            write_json(out / "evaluation.json", evaluations)
            stage.update(results={target: {name: value.get("accuracy", {"n": 0}) for name, value in result["systems"].items()}
                                  for target, result in evaluations.items()}, split_mode=frozen["split"]["mode"])
        with workflow_stage("backtest_error_analysis", "Preserve errors and counterexamples") as stage:
            errors = error_analysis(evaluations)
            write_json(out / "error-analysis.json", errors)
            stage.update(used_to_refit=False, policy_promoted=False)
        run.status = "complete_mechanical_only" if mode == "mechanical_only" else "complete"
        run.results_path = str((out / "evaluation.json").resolve())
    except Exception as exc:
        run.status = "failed"
        run.blockers.append(f"{type(exc).__name__}: pipeline stopped; see the closed workflow stage. No partial cohort is silently accepted.")
        # Do not duplicate provider exception text that may contain credentials.
    finally:
        run.budget = {**budget.summary(), "billed_usage_for_failed_requests": "unknown", "model_inference_present": budget.calls > 0}
        label = {"blocked": "Blocked", "failed": "Stopped", "complete": "Evaluated",
                 "complete_mechanical_only": "Mechanical evaluation"}.get(run.status, run.status)
        set_display_name(f"Instagram backtest: {label} | {run.admitted_posts} verified videos")
        save_run(run, data_dir)
    return run


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", required=True, type=Path)
    parser.add_argument("--media-manifest", required=True, type=Path)
    parser.add_argument("--outcomes", required=True, type=Path)
    parser.add_argument("--outcomes-sha256", required=True)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--mode", choices=METHODS, default="mechanical_only")
    parser.add_argument("--max-model-calls", type=int, default=120)
    parser.add_argument("--deadline-s", type=int, default=1800)
    args = parser.parse_args()
    init_weave()
    providers = None
    if args.mode == "mechanical_and_model":
        from ..providers.registry import build_providers
        providers = build_providers()
    result = run_backtest(protocol_path=args.protocol, media_manifest_path=args.media_manifest, outcomes_path=args.outcomes,
                          outcomes_sha256=args.outcomes_sha256, data_dir=args.data_dir, mode=args.mode, providers=providers,
                          max_model_calls=args.max_model_calls, deadline_s=args.deadline_s)
    flush()
    print(json.dumps(result.model_dump(mode="json"), indent=2))
    raise SystemExit(0 if result.status.startswith("complete") else 2)


if __name__ == "__main__":
    main()
