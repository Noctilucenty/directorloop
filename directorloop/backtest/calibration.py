"""Versioned views/reach baselines fitted only on the designated development partition.

The default split is chronological 60/20/20. Feature families and ridge strength are
fixed before labels are read. Calibration residuals only set intervals; held-out
targets never fit weights, scalers, imputation, baselines, or model selection.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict

import numpy as np

from .metrics import finite, interval_metrics, paired_metrics, residual_radius

MECHANICAL = ["duration_seconds", "cut_rate_per_second", "speech_words_per_second", "silence_fraction",
              "longest_static_fraction", "mean_motion", "opening_motion", "mean_loudness"]
MODEL = ["model_high_risk_fraction", "model_first_risk_fraction", "model_confusion_fraction",
         "model_continue_score", "model_share_score", "model_replay_score"]
SYSTEMS = {"account_median_v1": [], "duration_baseline_v1": ["duration_seconds"],
           "mechanical_score_v1": MECHANICAL, "model_score_v1": MODEL, "combined_score_v1": MECHANICAL + MODEL,
           "performance_score_v2_candidate": MECHANICAL + MODEL + ["static_speech_interaction", "duration_silence_interaction"]}
MINIMUM_EVALUATION_N = 20  # A reporting floor, never a claim of statistical reliability.


def feature_value(row: dict, key: str) -> float | None:
    f = row.get("features", {})
    if key == "static_speech_interaction":
        keys = ("longest_static_fraction", "speech_words_per_second")
    elif key == "duration_silence_interaction":
        keys = ("duration_seconds", "silence_fraction")
    else:
        return float(f[key]) if finite(f.get(key)) else None
    return float(f[keys[0]] * f[keys[1]]) if all(finite(f.get(k)) for k in keys) else None


def frozen_split(metadata: list[dict], *, minimum_dataset_n: int = 20) -> dict:
    """This function accepts identity/date metadata, never outcomes."""
    ids = [r["media_id"] for r in metadata]
    hashes = [r["artifact_sha256"] for r in metadata]
    if len(ids) != len(set(ids)) or len(hashes) != len(set(hashes)):
        raise ValueError("duplicate identities or exact media must be grouped before splitting")
    if any(not r.get("posted_at") for r in metadata):
        return {"mode": "unavailable", "reason": "posting dates required for chronological split", "train": [], "calibration": [], "test": [],
                "heldout_partition": False, "heldout_claim_allowed": False}
    ordered = sorted(metadata, key=lambda r: (r["posted_at"], r["media_id"]))
    ordered_ids = [r["media_id"] for r in ordered]
    if len(ordered_ids) < minimum_dataset_n:
        result = {"mode": "exploratory_walk_forward", "minimum_training_n": 3, "ordered": ordered_ids,
                  "train": [], "calibration": [], "test": [], "heldout_partition": False}
    else:
        n_train, n_cal = math.floor(len(ids) * 0.6), math.floor(len(ids) * 0.2)
        result = {"mode": "chronological_holdout", "train": ordered_ids[:n_train], "calibration": ordered_ids[n_train:n_train + n_cal],
                  "test": ordered_ids[n_train + n_cal:], "heldout_partition": True}
    # Identity-only splitting cannot establish label availability or reliability.
    result.update({"minimum_dataset_n": minimum_dataset_n, "heldout_claim_allowed": False,
                   "claim_note": "Partition identity is frozen; label-qualified evaluation and reliability must be assessed separately."})
    result["split_sha256"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    return result


def _valid_target(row: dict, target: str) -> bool:
    value = row.get(target)
    return finite(value) and value >= 0 and (target != "skip_rate" or value <= 1)


def fit_system(train: list[dict], target: str, requested_features: list[str]) -> dict | None:
    rows = [r for r in train if _valid_target(r, target)]
    if len(rows) < 3:
        return None
    by_account: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        by_account[r["account_id"]].append(float(r[target]))
    baseline = {k: float(np.median(v)) for k, v in by_account.items()}
    fallback = float(np.median([r[target] for r in rows]))
    active = [k for k in requested_features if sum(finite(feature_value(r, k)) for r in rows) >= max(3, math.ceil(0.75 * len(rows)))]
    if requested_features and not active:
        return None
    # A model-only/combined method cannot claim model evidence when every model feature is absent.
    if any(k in MODEL for k in requested_features) and not any(k in MODEL for k in active):
        return None
    medians = [float(np.median([feature_value(r, k) for r in rows if finite(feature_value(r, k))])) for k in active]
    x = np.array([[feature_value(r, k) if finite(feature_value(r, k)) else medians[j] for j, k in enumerate(active)] for r in rows], dtype=float)
    means = x.mean(axis=0) if active else np.array([])
    scales = np.maximum(x.std(axis=0), 1e-12) if active else np.array([])
    x = (x - means) / scales if active else np.zeros((len(rows), 0))
    y = np.array([math.log1p(r[target]) - math.log1p(baseline[r["account_id"]]) for r in rows])
    design = np.column_stack([np.ones(len(rows)), x])
    penalty = np.diag([0.0] + [1.0] * len(active))
    # The median baseline remains an actual median, without a fitted log intercept.
    coef = np.linalg.solve(design.T @ design + penalty, design.T @ y).tolist() if requested_features else [0.0]
    return {"n_train": len(rows), "features": active, "missing_features": [k for k in requested_features if k not in active],
            "imputation": medians, "means": means.tolist(), "scales": scales.tolist(), "coefficients": coef,
            "account_baselines": baseline, "fallback_baseline": fallback, "target": target, "ridge_alpha": 1.0,
            "label_horizon": "observed historical snapshot, not a reconstructed pre-publication forecast"}


def predict_log(model: dict, row: dict) -> float:
    baseline = model["account_baselines"].get(row["account_id"], model["fallback_baseline"])
    result = math.log1p(baseline) + model["coefficients"][0]
    for j, key in enumerate(model["features"]):
        val = feature_value(row, key)
        val = val if finite(val) else model["imputation"][j]
        result += (val - model["means"][j]) / model["scales"][j] * model["coefficients"][j + 1]
    result = max(0.0, result)
    return min(math.log(2), result) if model.get("target") == "skip_rate" else result


def _original_scale(value: float, target: str) -> float | None:
    if target == "skip_rate":
        return math.expm1(min(math.log(2), max(0.0, value)))
    return math.expm1(max(0.0, value)) if value < 700 else None


def _evaluate_fitted(model: dict, calibration: list[dict], test: list[dict], target: str) -> dict:
    cal = [r for r in calibration if _valid_target(r, target)]
    radius = residual_radius([abs(math.log1p(r[target]) - predict_log(model, r)) for r in cal])
    rows = []
    for r in test:
        if not _valid_target(r, target):
            continue
        log_p = predict_log(model, r)
        # Overflow is explicit unboundedness, never silently capped to an impressive number.
        estimate = _original_scale(log_p, target)
        lower = _original_scale(log_p - radius, target) if radius is not None else 0.0
        upper = _original_scale(log_p + radius, target) if radius is not None else (1.0 if target == "skip_rate" else None)
        base = model["account_baselines"].get(r["account_id"], model["fallback_baseline"])
        rows.append({"media_id": r["media_id"], "actual": r[target], "predicted": estimate, "lower": lower, "upper": upper,
                     "interval_level": 0.8, "account_baseline_train_only": base,
                     "interval_status": "uncalibrated_full_domain" if radius is None else "numerical_overflow" if lower is None or upper is None else "calibrated",
                     "prediction_status": "numerical_overflow" if estimate is None else "available",
                     "actual_relative_to_baseline": r[target] / base if base > 0 else None,
                     "predicted_relative_to_baseline": estimate / base if base > 0 and estimate is not None else None})
    accuracy = paired_metrics([r["predicted"] for r in rows], [r["actual"] for r in rows])
    return {"model": model, "calibration_n": len(cal), "log_residual_radius": radius, "rows": rows,
            "accuracy": accuracy,
            "evaluation_eligibility": {
                "test_partition_n": len(test), "valid_test_label_n": len(rows),
                "missing_or_invalid_test_label_n": len(test) - len(rows),
                "valid_prediction_pair_n": accuracy["n"], "estimates_available": accuracy["n"] > 0,
                "minimum_evaluation_n": MINIMUM_EVALUATION_N,
                "sample_floor_met": accuracy["n"] >= MINIMUM_EVALUATION_N,
                "calibration_partition_n": len(calibration), "valid_calibration_label_n": len(cal),
                "calibrated_intervals_available": radius is not None,
                "reliability_established": False,
                "note": "The sample floor is descriptive. Account dependence, exposure age, drift and uncertainty still require review.",
            },
            "intervals": interval_metrics(rows),
            "interpretation": "Retrospective held-out association. Time drift and mismatched metric ages can invalidate nominal interval coverage."}


def evaluate_systems(rows: list[dict], split: dict, target: str = "views") -> dict:
    by_id = {r["media_id"]: r for r in rows}
    results = {}
    for name, features in SYSTEMS.items():
        if split["mode"] == "chronological_holdout":
            train = [by_id[i] for i in split["train"] if i in by_id]
            model = fit_system(train, target, features)
            if model is None:
                results[name] = {"status": "unavailable", "reason": "insufficient training labels or required feature family absent", "n": 0}
                continue
            results[name] = _evaluate_fitted(model, [by_id[i] for i in split["calibration"] if i in by_id], [by_id[i] for i in split["test"] if i in by_id], target)
        elif split["mode"] == "exploratory_walk_forward":
            ordered = [by_id[i] for i in split["ordered"] if i in by_id]
            predictions = []
            for i in range(split["minimum_training_n"], len(ordered)):
                model = fit_system(ordered[:i], target, features)
                if model is not None:
                    predictions.extend(_evaluate_fitted(model, [], [ordered[i]], target)["rows"])
            results[name] = {"status": "exploratory_only", "rows": predictions,
                             "accuracy": paired_metrics([r["predicted"] for r in predictions], [r["actual"] for r in predictions]),
                             "warning": "No tuning or independent final test claim; insufficient data for calibrated ranges."}
        else:
            results[name] = {"status": "unavailable", "reason": split.get("reason"), "n": 0}
    return {"target": target, "split": split, "systems": results, "promoted_system": None,
            "promotion_status": "No automatic promotion. Compare development and held-out error with account and exposure confounders first."}
