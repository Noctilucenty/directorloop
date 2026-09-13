"""Reconcile an interrupted job's saved artifact without re-executing its pipeline."""
from __future__ import annotations

import fcntl
import hashlib
import json
import re
import uuid
from pathlib import Path

from .store import Job


def reconcile_interrupted_job(data_dir: Path, job: Job) -> None:
    if job.state != "FAILED" or not re.fullmatch(r"job_[0-9a-f]+_[0-9a-f]+", job.id):
        raise ValueError("recovery requires a valid failed job")
    recovery_dir = data_dir / "job-recovery"
    recovery_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = recovery_dir / f"{job.id}.json"
    with (recovery_dir / f"{job.id}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if receipt_path.exists():
            return
        suffix = job.id.removeprefix("job_")
        location = {"run": ("runs", f"run_{suffix}.json"), "abc": ("abc", f"abc_{suffix}.json"),
                    "causal": ("causal", f"causal_{suffix}.json"),
                    "screening": ("screenings", f"screen_{suffix}.json")}.get(job.kind)
        path = data_dir.joinpath(*location) if location else None
        receipt = {"job_id": job.id, "job_kind": job.kind, "reason": "worker_lease_expired",
                   "detected_at": job.ended_at, "last_heartbeat_at": job.heartbeat_at,
                   "automatic_retry": False, "artifact_path": str(path) if path else None,
                   "artifact_action": "not_created_or_no_run_artifact", "stages_closed": 0}
        if path is not None and path.exists():
            before = path.read_bytes()
            record = json.loads(before)
            if record.get("id") != path.stem:
                raise ValueError("saved interrupted artifact has a different identity")
            receipt["before_sha256"] = hashlib.sha256(before).hexdigest()
            if record.get("status") == "running":
                snapshot = recovery_dir / f"{job.id}.before.json"
                if not snapshot.exists():
                    snapshot.write_bytes(before)
                record["status"] = "failed"
                record["ended_at"] = job.ended_at
                record["error"] = "WorkerInterrupted: worker execution lease expired"
                record["stop_reason"] = "The worker stopped during execution. Completed evidence is preserved; this job was not automatically retried."
                for stage in record.get("workflow_stages", []):
                    if stage.get("status") == "running":
                        stage.update(status="failed", ended_at=job.ended_at, duration_ms=None)
                        stage.setdefault("summary", {}).update(interruption="worker_lease_expired", duration_status="unknown after interruption")
                        receipt["stages_closed"] += 1
                if job.kind == "screening":
                    for window in record.get("windows", []):
                        if window.get("status") == "pending":
                            window.update(status="failed", error="Worker interrupted; request completion and billing are unknown")
                after = json.dumps(record, indent=2).encode()
                tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
                tmp.write_bytes(after)
                tmp.replace(path)
                receipt.update(artifact_action="marked_failed", after_sha256=hashlib.sha256(after).hexdigest(),
                               before_snapshot=str(snapshot))
            else:
                receipt["artifact_action"] = "preserved_existing_terminal_artifact"
        tmp_receipt = receipt_path.with_suffix(".tmp")
        tmp_receipt.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        tmp_receipt.replace(receipt_path)
