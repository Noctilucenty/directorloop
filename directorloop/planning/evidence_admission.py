"""Pure completion checks for learning; rejecting a risky edit needs less evidence than learning its effect."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any


def comparison_learning_blockers(comparison: dict[str, Any] | None, protected: list[str] | None = None) -> list[str]:
    if not comparison:
        return ["comparison is missing"]
    blockers = []
    preference = comparison.get("full_preference")
    if not isinstance(preference, int | float) or isinstance(preference, bool) or not math.isfinite(preference):
        blockers.append("whole-video comparison has no valid preference result")
    if comparison.get("outcome") == "insufficient_evidence":
        blockers.append("comparison failed or remained dependent on presentation order")
    if comparison.get("protected_unchecked"):
        blockers.append("protected items were not checked: " + "; ".join(comparison["protected_unchecked"]))
    requested = list(dict.fromkeys([*(protected or []), *comparison.get("protected_requested", [])]))
    checks = comparison.get("protected_checks") or []

    def words(value: str) -> str:
        return " ".join(re.findall(r"[a-z0-9]+", value.lower()))

    # A provider exception often returns a row with status unclear. A returned row is not a completed check.
    complete = [words(str(c.get("item", ""))) for c in checks if c.get("status") in {"kept", "lost", "respected", "violated"}]
    for item in requested:
        normalized = words(item)
        if not normalized or not any(v and (normalized in v or v in normalized) for v in complete):
            blockers.append(f"protected check missing, failed or unclear: {item}")
    if any(c.get("status") not in {"kept", "lost", "respected", "violated"} for c in checks):
        blockers.append("one or more protected checks did not establish a result")
    return list(dict.fromkeys(blockers))


def arm_learning_blockers(arm: dict[str, Any]) -> list[str]:
    blockers = []
    if not arm.get("render_hash") or arm.get("verified") is not True:
        blockers.append("a verified rendered artifact is missing")
    if not arm.get("candidate_audit_id"):
        blockers.append("the candidate audit is missing")
    if arm.get("evaluator_differences"):
        blockers.append("the evaluator changed between original and candidate")
    blockers.extend(comparison_learning_blockers(arm.get("comparison"), arm.get("protected_items")))
    return blockers


def legacy_evaluation_receipt(record: dict[str, Any], data_dir: Path) -> dict[str, Any]:
    """Validate legacy completion against saved run/audit evidence, without editing either historical record."""
    receipt: dict[str, Any] = {"complete": False, "blockers": [], "method": "saved-run-and-candidate-audit-v1", "evidence_sha256": {}}

    def load(name: str, normal: Path) -> dict[str, Any] | None:
        if not re.fullmatch(r"[a-zA-Z0-9_-]+\.json", name):
            return None
        path = normal
        if not path.is_file():
            path = data_dir / "policy-evidence" / name
            manifest_path = path.parent / "manifest.json"
            if not path.is_file() or not manifest_path.is_file():
                return None
            try:
                manifest = json.loads(manifest_path.read_text())
                expected = manifest["files"][name]["sha256"]
            except (ValueError, KeyError, TypeError):
                return None
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                return None
        try:
            raw = path.read_bytes()
            value = json.loads(raw)
            if not isinstance(value, dict):
                return None
            receipt["evidence_sha256"][name] = hashlib.sha256(raw).hexdigest()
            return value
        except (OSError, ValueError):
            return None

    run_id = str(record.get("run_id", ""))
    run = load(f"{run_id}.json", data_dir / "causal" / f"{run_id}.json")
    if not run or run.get("id") != run_id or run.get("artifact_hash") != record.get("artifact_hash"):
        receipt["blockers"] = ["matching source causal run evidence is unavailable"]
        return receipt
    arm = next((a for a in run.get("arms", []) if a.get("experiment_id") == record.get("experiment_id") and a.get("policy_record_id") == record.get("id")), None)
    if not arm or arm.get("render_hash") != record.get("variant_artifact_hash") or arm.get("plan_hash") != record.get("plan_hash") or arm.get("verdict") != record.get("outcome"):
        receipt["blockers"] = ["policy record does not match its saved intervention and outcome"]
        return receipt
    receipt["blockers"].extend(arm_learning_blockers(arm))
    aid = str(arm.get("candidate_audit_id", ""))
    audit = load(f"{aid}.json", data_dir / "audit" / aid / "audit.json")
    if not audit or audit.get("id") != aid or audit.get("status") != "complete" or audit.get("artifact_hash") != arm.get("render_hash"):
        receipt["blockers"].append("a complete candidate audit matching the rendered artifact is unavailable")
    receipt["complete"] = not receipt["blockers"]
    return receipt
