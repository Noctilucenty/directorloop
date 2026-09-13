"""Operator recovery of an existing incomplete review without repeating its first pass."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ..domain.ids import sha256_file, sha256_json, utc_now_iso
from ..observability.weave_ops import current_call_ref, traced
from ..observability.workflow import workflow_session
from .models import ScreenWindow
from .repair_loop import repair_selected_windows, saved_payload
from .runner import _persist, load_screening


@traced("directorloop.complete_saved_review", kind="agent", display="Complete saved video review | recheck missing evidence")
@workflow_session(persist=_persist)
def complete_saved_review(screening_id: str, provider: Any, data_dir: Path, *, on_stage=None, focused_upgrade: bool = False):
    """Explicit one-time repair, retaining an immutable prior report and all calls."""
    data_dir = Path(data_dir)
    report = load_screening(data_dir, screening_id)
    if report.status not in {"complete", "needs_review"} or report.protocol.get("coverage") != "full":
        raise ValueError("Only a terminal whole-video review can be corrected")
    if not focused_upgrade and (report.review_repair is not None or any(w.review_attempts for w in report.windows)):
        raise ValueError("This review already received its bounded correction pass")
    if focused_upgrade and (not report.review_repair or any(a["phase"] == "focused_repair" for w in report.windows for a in w.review_attempts)):
        raise ValueError("Focused repair upgrade is unavailable or was already attempted")
    if sha256_file(Path(report.artifact_path)) != report.artifact_hash:
        raise ValueError("Source media changed; saved review cannot be corrected")
    payloads = [saved_payload(w) for w in report.windows]
    revision_dir = data_dir / "screening_revisions" / screening_id
    revision_dir.mkdir(parents=True, exist_ok=True)
    source = data_dir / "screenings" / f"{screening_id}.json"
    # Exclusive creation is also the durable no-replay gate.
    with (revision_dir / ("before-focused-upgrade.json" if focused_upgrade else "before-evidence-repair.json")).open("xb") as handle:
        handle.write(source.read_bytes())
    original_hash = sha256_file(source)
    original_trace = report.weave_url
    started, original_elapsed = time.monotonic(), report.elapsed_ms
    report.status, report.error, report.ended_at = "running", None, None
    report.weave_call_id, report.weave_url = current_call_ref()

    def checkpoint():
        report.elapsed_ms = original_elapsed + int((time.monotonic() - started) * 1000)
        if report.review_repair:
            report.review_repair.update(source_report_sha256=original_hash, source_weave_url=original_trace)
        guard = getattr(provider, "spend_guard", None)
        if guard:
            report.spend_guard = guard.ledger.summary()
        _persist(report, data_dir)

    checkpoint()
    try:
        repair_selected_windows(report, payloads, provider, checkpoint, lambda: None, on_stage, upgrade=focused_upgrade)
        if sha256_file(Path(report.artifact_path)) != report.artifact_hash:
            raise ValueError("Source media changed during review correction")
        report.status = "complete" if all(w.status == "complete" for w in report.windows) else "needs_review"
        report.protocol["evidence_repair"] = {"version": "focused-evidence-repair-v2", "max_calls_per_section": 1, "source_report_sha256": original_hash, "legacy_upgrade": focused_upgrade}
        report.protocol_fingerprint = sha256_json(report.protocol)
    except BaseException:
        report.status = "needs_review"
        if report.review_repair:
            report.review_repair["status"] = "failed"
        raise
    finally:
        attempts = [ScreenWindow.model_validate(a["window"]) for w in report.windows if w.review_attempts for a in w.review_attempts]
        attempts.extend(w for w in report.windows if not w.review_attempts)
        report.input_tokens = sum(w.input_tokens for w in attempts) if all(w.input_tokens is not None for w in attempts) else None
        report.output_tokens = sum(w.output_tokens for w in attempts) if all(w.output_tokens is not None for w in attempts) else None
        report.ended_at = utc_now_iso()
        checkpoint()
    return report
