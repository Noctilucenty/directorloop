"""Read-only intake diagnostics. This module never opens Instagram outcome values."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from ..domain.ids import sha256_file, utc_now_iso
from .features import canonical_hash
from .runner import admit_cohort, implementation_hashes, seal_predictions

MANIFEST_FIELDS = ("media_id", "instagram_post_id", "account_id", "artifact_sha256", "local_path", "provenance_path", "provenance_sha256")
RECEIPT_FIELDS = ("media_id", "instagram_post_id", "account_id", "artifact_sha256", "source_kind", "identity_verified", "full_decode_passed", "audio_video_complete")


def inspect_admission(root: Path) -> dict:
    """Inspect identities, file receipts and prediction seals; never create predictions."""
    root = Path(root)
    protocol_path, manifest_path = root / "protocol-v1.json", root / "published-media-manifest.json"
    protocol = json.loads(protocol_path.read_text())
    manifest = json.loads(manifest_path.read_text())
    admitted, excluded = admit_cohort(protocol, manifest)
    checksum = root / "protocol-v1.sha256"
    expected = checksum.read_text().strip().split()[0] if checksum.exists() else None
    protocol_hash = sha256_file(protocol_path)
    rows_by_id = {row["media_id"]: row for row in manifest}
    missing = []
    for identity in protocol["selected_observations"]:
        row = rows_by_id.get(identity["media_id"], {})
        missing.append({"media_id": identity["media_id"], "instagram_post_id": identity.get("instagram_post_id"),
                        "account_id": identity.get("account_id"), "observation_id": identity.get("observation_id"),
                        "posted_at": identity.get("posted_at"), "checkpoint_at": identity.get("collected_at"),
                        "missing_manifest_fields": [key for key in MANIFEST_FIELDS if not row.get(key)],
                        "required_receipt_fields": list(RECEIPT_FIELDS) if not row.get("provenance_path") else [],
                        "status": "admitted" if any(item["media_id"] == identity["media_id"] for item in admitted) else "excluded"})
    runs = []
    for run_path in sorted((root / "execution" / "backtests").glob("*/run.json")):
        run = json.loads(run_path.read_text())
        frozen_path, predictions_path, seal_path = [run_path.parent / name for name in ("frozen.json", "predictions.jsonl", "prediction-seal.json")]
        checks = {"frozen_exists": frozen_path.is_file(), "predictions_exist": predictions_path.is_file(), "seal_exists": seal_path.is_file()}
        receipt = {"run_id": run.get("id", run_path.parent.name), "run_sha256": sha256_file(run_path), "status": run.get("status"),
                   "admitted_posts": run.get("admitted_posts"), "persisted_predictions": run.get("persisted_predictions"),
                   "outcomes_previously_unblinded": run.get("outcomes_unblinded"), "checks": checks, "eligible_for_existing_prediction_comparison": False}
        if all(checks.values()):
            frozen, seal = json.loads(frozen_path.read_text()), json.loads(seal_path.read_text())
            try:
                current_seal = seal_predictions(predictions_path, frozen)
                seal_valid = all(seal.get(key) == current_seal[key] for key in ("prediction_file_sha256", "frozen_sha256", "prediction_count", "outcome_file_sha256"))
                exact_cohort = canonical_hash(frozen["cohort"]) == canonical_hash(admitted)
                checks.update(seal_valid=seal_valid, current_exact_cohort_matches=exact_cohort,
                              protocol_matches=frozen.get("protocol_sha256") == protocol_hash and expected == protocol_hash,
                              implementation_matches=frozen.get("implementation_hashes") == implementation_hashes())
                receipt["eligible_for_existing_prediction_comparison"] = bool(admitted and all(checks.values()) and not run.get("outcomes_unblinded"))
            except (ValueError, KeyError, OSError):
                checks["seal_valid"] = False
        runs.append(receipt)
    return {"schema": "instagram-admission-diagnostics-v1", "created_at": utc_now_iso(), "offline": True, "provider_calls": 0,
            "outcome_values_opened": False, "new_predictions_created": 0, "protocol_sha256": protocol_hash,
            "protocol_checksum_matches": expected == protocol_hash, "published_manifest_sha256": sha256_file(manifest_path),
            "selected_posts": len(protocol["selected_observations"]), "admitted_posts": len(admitted),
            "exclusion_reasons": dict(Counter(row["reason"] for row in excluded)), "excluded": excluded,
            "intake_rows": missing, "existing_runs": runs,
            "accuracy_result_available": False, "calibration_performed": False,
            "next_action": "Acquire exact posted media and verified identity/byte receipts before any new frozen prediction run." if not admitted else "Inspect eligible sealed predictions with their frozen implementation before any outcome read.",
            "limitations": ["This checks existing evidence receipts, not the truth of unchecked receipt assertions.",
                            "No automatic unblinding or calibration. Same-post checkpoints are not independent videos.",
                            "Average watch time does not provide completion or a timestamp retention curve."]}


def intake_markdown(report: dict) -> str:
    return f"""# Instagram backtest admission

{report['admitted_posts']} of {report['selected_posts']} frozen posts are admitted. No new predictions, outcome reads, paid calls or calibration were performed.

For each excluded post, provide the complete posted video with audio, its SHA256, native Instagram post ID and account ID, and a provenance receipt. The receipt must bind the same identities and SHA256 to platform-served bytes or independently verified uploaded bytes, and record full-decode success and complete audio/video. A similar local master, filename or matching runtime is insufficient.

Manifest fields: `{', '.join(MANIFEST_FIELDS)}`.

Receipt fields: `{', '.join(RECEIPT_FIELDS)}`. `source_kind` must be `platform_served` or `verified_uploaded_bytes`; the three verification flags must be true based on checked evidence. Do not fill them speculatively.

Use the exact per-post `observation_id` and checkpoint already listed in `admission.json`. Preserve the 168-hour horizon with ±24-hour tolerance and frozen split. Do not choose metrics by performance. The two exploratory local copies remain outside the strict cohort.

Available intake receipts previously documented views/reach/watch/skip coverage for 28 selected checkpoints, but exact completion and followers-at-posting are absent. Timestamp exit validation also needs a native per-second retention curve for the same admitted video and checkpoint, with time units, metric definition/denominator, capture time and source receipt. One screenshot-estimated curve cannot validate a general exit predictor; average watch time cannot substitute for a curve.

After media admission, freeze and seal all predictions before opening matched outcomes. Report relative/range forecasts only if the frozen training/calibration split supports them. Until then, view accuracy and exit accuracy are unknown.
"""
