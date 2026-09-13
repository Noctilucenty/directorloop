"""Pre-register blind content predictions before any historical metrics are read.

Qualitative risk is ordinal. No function here derives retention, exit probability,
or a raw-view estimate from it. This module never loads an outcome file.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
from pathlib import Path
from typing import Any

from ..domain.ids import sha256_file, utc_now_iso
from .features import MECHANICAL_FEATURE_NAMES, canonical_hash, overlap_ms

PREDICTION_SCHEMA = "blind-prediction-v1"
MECHANICAL_METHOD = "mechanical_attention_screen_v1"
MODEL_FEATURE_NAMES = (
    "model_high_risk_fraction", "model_first_risk_fraction", "model_any_elevated_risk",
    "model_confusion_fraction", "model_continue_score", "model_share_score", "model_replay_score",
)
ORDINAL_RISK = {"low": 0, "medium": 1, "high": 2}
ORDINAL_VERDICT = {"NO": 0, "MAYBE": 1, "YES": 2}
UNOBSERVABLE_FROM_CONTENT = [
    "distribution", "account state", "posting time", "audience mix", "caption and cover outside the video",
    "external traffic", "platform ranking", "paid spend",
]


def mechanical_window_predictions(mechanical: dict[str, Any]) -> list[dict[str, Any]]:
    """An explicitly uncalibrated screening baseline with inspectable terms.

    The formula is pre-declared, not fitted to historical outcomes. Even a high
    index can be an intentional slow/cinematic shot. It must compete with simple
    duration/cut/speech baselines before it is used for decisions.
    """
    result = []
    for w in mechanical.get("windows", []):
        contributions = []
        still, quiet, words, density = (w.get(k) for k in ("static_fraction", "silence_fraction", "word_count", "speech_words_per_second"))
        if still is not None:
            contributions.append({"feature": "static_fraction", "value": still, "weight": 1.0,
                                  "contribution": still, "reason": "Little measured visual change; this can be intentional."})
        if quiet is not None:
            contributions.append({"feature": "relative_quiet_fraction", "value": quiet, "weight": 0.5,
                                  "contribution": quiet * 0.5, "reason": "Low audio energy relative to the prefix; music and meaning are not judged."})
        if still is not None and words is not None:
            interaction = still if words == 0 else 0.0
            contributions.append({"feature": "static_without_transcribed_words", "value": interaction, "weight": 0.5,
                                  "contribution": interaction * 0.5, "reason": "Static image with no completed ASR words in this window; speech may be missed."})
        if density is not None:
            overload = min(1.0, max(0.0, (density - 4.0) / 4.0))
            contributions.append({"feature": "speech_density_above_4_words_per_second", "value": overload, "weight": 0.5,
                                  "contribution": overload * 0.5, "reason": "An unvalidated load proxy; fast speech can be effective."})
        index = sum(c["contribution"] for c in contributions) if contributions else None
        missing = [name for name in ("static_fraction", "silence_fraction", "word_count", "speech_words_per_second") if w.get(name) is None]
        # A partial sum is retained diagnostically, but is not ranked as low risk.
        comparable_index = index if not missing else None
        risk = None if comparable_index is None else "high" if comparable_index >= 1.25 else "medium" if comparable_index >= 0.5 else "low"
        result.append({"start_ms": w["start_ms"], "end_ms": w["end_ms"], "method_version": MECHANICAL_METHOD,
                       "evidence_type": "MECHANICAL SIGNAL", "attention_risk": risk,
                       "risk_index": comparable_index, "partial_feature_sum": index,
                       "risk_index_scale": "0 to 2.5, uncalibrated screening index; not a probability",
                       "exit_probability": None, "confidence": None, "uncertainty": "high; no outcome calibration",
                       "contributions": contributions, "missing_features": missing})
    return result


def model_features_from_audit(audit: Any, *, expected_sha256: str, blind_verified: bool = False) -> dict[str, Any]:
    """Convert a saved audit without interpreting ordinal values as probabilities.

    Hash/priming checks are necessary but cannot establish whether a previous
    reviewer saw outcomes elsewhere. The caller must verify the blind provenance.
    """
    obj = audit.model_dump(mode="json") if hasattr(audit, "model_dump") else dict(audit)
    if obj.get("artifact_hash") != expected_sha256:
        raise ValueError("Model audit does not match the exact artifact")
    if obj.get("primed_with"):
        raise ValueError("A primed audit cannot be registered as a blind prediction")
    if not blind_verified:
        raise ValueError("Independent blind provenance must be explicitly verified")
    if obj.get("status") != "complete":
        raise ValueError("Incomplete model audits cannot supply complete blind predictions")
    duration = int(obj.get("duration_ms", 0))
    if duration <= 0:
        raise ValueError("Invalid model audit duration")
    attention = obj.get("attention") or {}
    segments = sorted(attention.get("segments", []), key=lambda s: (s["start_ms"], s["end_ms"]))
    previous_end = 0
    for s in segments:
        if not 0 <= s["start_ms"] < s["end_ms"] <= duration or s["start_ms"] < previous_end:
            raise ValueError("Effective model timeline overlaps or lies outside the artifact")
        previous_end = s["end_ms"]
    covered = overlap_ms([(s["start_ms"], s["end_ms"]) for s in segments], 0, duration)
    known = all(s.get("attention_risk") in ORDINAL_RISK for s in segments) and covered == duration
    reactions_known = covered == duration and all(s.get("reaction") in ("engaged", "neutral", "losing_interest", "confused") for s in segments)
    elevated = [s for s in segments if s.get("attention_risk") in ("medium", "high")]
    first = min((s["start_ms"] for s in elevated), default=None)
    audience = obj.get("audience") or {}

    def verdict(name):
        return ORDINAL_VERDICT.get((audience.get(name) or {}).get("verdict"))

    features = {
        "model_high_risk_fraction": sum(s["end_ms"] - s["start_ms"] for s in segments if s.get("attention_risk") == "high") / duration if known else None,
        "model_first_risk_fraction": first / duration if first is not None and known else None,
        "model_any_elevated_risk": int(bool(elevated)) if known else None,
        "model_confusion_fraction": sum(s["end_ms"] - s["start_ms"] for s in segments if s.get("reaction") == "confused") / duration if reactions_known else None,
        "model_continue_score": verdict("continue_watching"), "model_share_score": verdict("send"), "model_replay_score": verdict("save_or_replay"),
    }
    reactions = obj.get("window_reactions", []) + obj.get("precision_reactions", [])
    windows = []
    for s in segments:
        matches = [r for r in reactions if r.get("scan", "coarse") == s.get("scan") and r["start_ms"] < s["end_ms"] and r["end_ms"] > s["start_ms"]]
        reading = max(matches, key=lambda r: min(r["end_ms"], s["end_ms"]) - max(r["start_ms"], s["start_ms"]), default={})
        windows.append({"start_ms": s["start_ms"], "end_ms": s["end_ms"], "scan": s.get("scan"),
                        "evidence_type": "MODEL PREDICTION", "attention_risk": s.get("attention_risk"),
                        "risk_index": ORDINAL_RISK.get(s.get("attention_risk")), "risk_index_scale": "ordinal 0/1/2; not calibrated probability",
                        "exit_probability": None, "confidence": None, "explanation": reading.get("cause"),
                        "contributions": [{"feature": "model_reported_reason", "reason": reading.get("cause"), "contribution": None}],
                        "note": "The model reason is an explanation, not an identified causal or numerical feature contribution."})
    weighted_risk = sum((s["end_ms"] - s["start_ms"]) * ORDINAL_RISK[s["attention_risk"]] for s in segments) / duration if known else None
    return {"features": features, "window_predictions": windows, "mean_ordinal_risk": weighted_risk,
            "source_audit_id": obj.get("id"), "source_audit_created_at": obj.get("created_at"),
            "weave_url": obj.get("weave_url"), "evaluator_identity": attention.get("evaluator"),
            "coverage": obj.get("coverage"), "qualitative_audience": audience,
            "strengths": obj.get("strengths", []), "findings": obj.get("findings", []),
            "missingness": {k: "not available from the verified audit" for k, v in features.items() if v is None},
            "blind_provenance_verified_by_caller": True}


def build_prediction_record(*, media_id: str, artifact_sha256: str, mechanical: dict[str, Any],
                            audit: Any = None, method: str = "mechanical_v1",
                            audit_blind_verified: bool = False) -> dict[str, Any]:
    if mechanical.get("artifact_sha256") != artifact_sha256 or mechanical.get("media_id") != media_id:
        raise ValueError("Mechanical identity differs from prediction identity")
    supplied = mechanical.get("features", {})
    if set(supplied) - set(MECHANICAL_FEATURE_NAMES):
        raise ValueError("Unexpected feature names; outcome variables are never prediction inputs")
    features = {k: supplied.get(k) for k in MECHANICAL_FEATURE_NAMES}
    features.update({k: None for k in MODEL_FEATURE_NAMES})
    missingness = dict(mechanical.get("missingness", {}))
    model = None
    if audit is not None:
        model = model_features_from_audit(audit, expected_sha256=artifact_sha256, blind_verified=audit_blind_verified)
        features.update(model["features"])
        missingness.update(model["missingness"])
    else:
        missingness.update({k: "no verified blind model audit; remote model not called" for k in MODEL_FEATURE_NAMES})
    mechanical_windows = mechanical_window_predictions(mechanical)
    duration = int(mechanical["duration_ms"])
    if duration <= 0:
        raise ValueError("A positive measured duration is required")
    complete_mechanical = all(w["risk_index"] is not None for w in mechanical_windows) and overlap_ms(
        [(w["start_ms"], w["end_ms"]) for w in mechanical_windows], 0, duration) == duration
    mechanical_index = sum((w["end_ms"] - w["start_ms"]) * w["risk_index"] for w in mechanical_windows) / duration if complete_mechanical else None
    if method == "mechanical_v1":
        windows = mechanical_windows
        ranking_score = -mechanical_index if mechanical_index is not None else None
        method_version = MECHANICAL_METHOD
    elif method in ("current_directorloop_v1", "model_qualitative_v1"):
        if model is None:
            raise ValueError("This prediction method requires a verified blind model audit")
        windows = model["window_predictions"]
        if method == "current_directorloop_v1":
            ranking_score = -model["mean_ordinal_risk"] if model["mean_ordinal_risk"] is not None else None
        else:
            ordinal = [features[k] for k in ("model_continue_score", "model_share_score", "model_replay_score")]
            ranking_score = sum(ordinal) / len(ordinal) if all(v is not None for v in ordinal) else None
        method_version = method
    else:
        raise ValueError("Unknown pre-registered prediction method")
    if any(value is not None and not math.isfinite(value) for value in features.values()):
        raise ValueError("Non-finite prediction feature")
    record = {"schema_version": PREDICTION_SCHEMA, "created_at": utc_now_iso(), "media_id": media_id,
              "artifact_sha256": artifact_sha256, "method_version": method_version, "features": features,
              "window_predictions": windows, "missingness": missingness,
              "ranking_score": ranking_score, "ranking_score_label": "uncalibrated content-only ordering index; higher is favored by this declared method",
              "source_audit_id": model["source_audit_id"] if model else None,
              "source_audit_created_at": model["source_audit_created_at"] if model else None,
              "evaluator_identity": model["evaluator_identity"] if model else None,
              "weave_url": model["weave_url"] if model else None,
              "model_inference_present": model is not None,
              "extractor_fingerprint": mechanical["extractor_fingerprint"],
              "prediction_module_sha256": sha256_file(Path(__file__)),
              "prediction_ranges": {k: None for k in ("skip_rate", "average_watch_percentage", "completion_rate", "relative_views_percentile", "raw_views")},
              "calibration_status": "not calibrated; no outcomes read by this module",
              "unobservable_factors": UNOBSERVABLE_FROM_CONTENT,
              "evidence_types": ["MECHANICAL SIGNAL", "LOCAL ASR DERIVED SIGNAL"] + (["MODEL PREDICTION"] if model else []),
              "limitations": mechanical.get("limitations", []) + [
                  "A fraction of timeline flagged high risk is not a fraction of viewers who leave.",
                  "Ordinal NO/MAYBE/YES scores are 0/1/2 ordering features, not response probabilities.",
                  "First spoken word is not first meaningful information; semantic timing remains unmeasured without a verified model audit.",
                  "No observed Instagram metric, human response, calibrated retention, or raw-view estimate is produced here."]}
    record["prediction_sha256"] = canonical_hash({k: v for k, v in record.items() if k != "created_at"})
    return record


def append_prediction(path: str | Path, record: dict[str, Any]) -> dict[str, Any]:
    """Append, fsync, and read back before the caller may unblind labels.

    Existing identities are idempotent only when the semantic prediction hash
    agrees. A revised prediction requires a new method version or another file.
    """
    expected = canonical_hash({k: v for k, v in record.items() if k not in ("created_at", "prediction_sha256")})
    if expected != record.get("prediction_sha256"):
        raise ValueError("Prediction payload changed after hashing")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    identity_keys = ("media_id", "artifact_sha256", "method_version")
    identity = tuple(record[k] for k in identity_keys)
    with path.open("a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        existing = handle.read()
        if existing and not existing.endswith(b"\n"):
            raise ValueError("Prediction log has a truncated line; refusing to overwrite evidence")
        rows = existing.splitlines()
        line_number = len(rows) + 1
        offset = len(existing)
        reused = False
        for index, raw in enumerate(rows):
            prior = json.loads(raw)
            prior_hash = canonical_hash({k: v for k, v in prior.items() if k not in ("created_at", "prediction_sha256")})
            if prior_hash != prior.get("prediction_sha256"):
                raise ValueError("Existing prediction log contains a tampered record")
            if tuple(prior.get(k) for k in identity_keys) == identity:
                if prior.get("prediction_sha256") != expected:
                    raise ValueError("Conflicting prediction already persisted for this method and artifact")
                line_number = index + 1
                offset = sum(len(x) + 1 for x in rows[:index])
                reused = True
                break
        if not reused:
            raw = json.dumps(record, sort_keys=True, allow_nan=False).encode() + b"\n"
            handle.seek(0, os.SEEK_END)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(offset)
        saved = json.loads(handle.readline())
        saved_hash = canonical_hash({k: v for k, v in saved.items() if k not in ("created_at", "prediction_sha256")})
        if saved.get("prediction_sha256") != expected or saved_hash != expected:
            raise RuntimeError("Persisted prediction failed read-back verification")
        return {"path": str(path.resolve()), "line_number": line_number, "byte_offset": offset,
                "prediction_sha256": expected, "file_sha256": sha256_file(path),
                "persisted_at": saved["created_at"], "verified_at": utc_now_iso(), "reused_existing": reused}
