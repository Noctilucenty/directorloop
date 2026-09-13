"""Post-run evidence health as Weave scorer feedback; not native Agent Signals.

One bounded pass over terminal saved screenings. No model calls, edits, or job
restarts. A receipt prevents repeated scoring of an unchanged report version.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import weave

from scripts.weave_trace_regression import digest

ROOT = Path(__file__).resolve().parents[1]
WINDOW_KEYS = ("start_ms", "end_ms", "is_last_prefix", "status", "judgment", "validation_issues",
               "frame_timestamps_ms", "prefix_asr_text", "attention_context")


def sanitize_report(report):
    return {"id": report["id"], "duration_ms": report["duration_ms"], "status": report["status"],
            "windows": [{key: window[key] for key in WINDOW_KEYS if key in window} for window in report["windows"]]}


def evidence_health(report):
    from directorloop.screening.attention_admission import assess_attention
    from directorloop.screening.boundary import boundary_validation_issues
    from directorloop.screening.models import ScreenReport, ScreenWindow
    from directorloop.screening.runner import validate_evidence
    from directorloop.screening.scoring import build_scorecard

    report = json.loads(json.dumps(report))
    if report["status"] not in {"complete", "needs_review", "failed", "canceled"}:
        raise ValueError("Only terminal reports can receive a post-run check")
    windows = []
    for item in report["windows"]:
        window = ScreenWindow.model_validate(item)
        if window.judgment:
            current = validate_evidence(window.judgment, window.frame_timestamps_ms, window.prefix_asr_text)
            current += boundary_validation_issues(window.judgment.model_dump(), is_last_prefix=window.is_last_prefix)
            window.validation_issues = list(dict.fromkeys([*window.validation_issues, *current]))
            if window.validation_issues and window.status == "complete":
                window.status = "needs_review"
        windows.append(window)
    saved = ScreenReport(id=report["id"], video_id="", artifact_path="", created_at="",
                         duration_ms=report["duration_ms"], status=report["status"], windows=windows)
    scores = build_scorecard(saved)
    blocked = [i for i, window in enumerate(windows) if assess_attention(window)["status"] != "supported"]
    invalid = [i for i, window in enumerate(windows) if window.validation_issues]
    missing_scores = [name for name in ("creative", "retention", "virality") if scores["metrics"][name]["score"] is None]
    cursor = 0
    full_coverage = bool(windows) and report["duration_ms"] > 0
    for window in sorted(windows, key=lambda window: window.start_ms):
        full_coverage = full_coverage and window.start_ms == cursor and window.end_ms > window.start_ms
        cursor = window.end_ms
    full_coverage = full_coverage and cursor == report["duration_ms"]
    return {"checker_version": "evidence-health-v1", "source_report_sha256": digest(report),
            "needs_review": bool(blocked or invalid or missing_scores or not full_coverage
                                 or report["status"] in {"failed", "canceled"}),
            "complete_time_coverage": full_coverage, "total_sections": len(windows),
            "blocked_attention_sections": blocked, "invalid_evidence_sections": invalid,
            "unavailable_scores": missing_scores, "new_model_calls": 0,
            "semantic_grounding_verified": False, "native_signals": False}


def safe_score_inputs(inputs):
    # The source call already holds its output. Do not duplicate local media
    # paths or raw repair attempts in the new scorer call's logged inputs.
    return {"report": inputs["report"]}


@weave.op(name="directorloop.production.evidence_health", postprocess_inputs=safe_score_inputs)
def production_evidence_health(output: dict, report: dict) -> dict:
    # `output` is supplied by Weave's scorer contract; checks use the explicitly
    # identified saved terminal report version, which can postdate a repair.
    return evidence_health(report)


async def publish_one(client, report, source_call_id):
    call = client.get_call(source_call_id)
    if call.ended_at is None:
        raise ValueError("Source trace is still active")
    result = await call.apply_scorer(production_evidence_health, additional_scorer_kwargs={"report": report})
    client._flush()
    return {"source_call_id": source_call_id, "score_call_id": result.score_call.id, "checks": result.result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--reports-dir", type=Path, default=ROOT / "data" / "screenings")
    parser.add_argument("--receipt-dir", type=Path, required=True)
    parser.add_argument("--max-reports", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.max_reports <= 10:
        parser.error("--max-reports must be between 1 and 10")
    args.receipt_dir.mkdir(parents=True, exist_ok=True)
    client = None
    results = []
    for path in sorted(args.reports_dir.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True):
        try:
            source = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue  # A concurrent atomic replacement or incomplete write is retried next pass.
        if source.get("status") not in {"complete", "needs_review", "failed", "canceled"} or not source.get("weave_call_id"):
            continue
        report = sanitize_report(source)
        receipt_key = digest({"report": report, "rule": "evidence-health-v1", "source_call_id": source["weave_call_id"]})
        receipt_path = args.receipt_dir / f"{receipt_key}.json"
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text())
            if not args.publish or receipt.get("published"):
                continue
        checks = evidence_health(report)
        result = {"report_id": source["id"], "source_call_id": source["weave_call_id"],
                  "published": False, "checks": checks}
        if args.publish:
            if client is None:
                from directorloop.config import get_settings

                settings = get_settings()
                os.environ["WANDB_API_KEY"] = settings.wandb_api_key
                client = weave.init(settings.weave_project_path())
            result.update(asyncio.run(publish_one(client, report, source["weave_call_id"])))
            result["published"] = True
        receipt_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        results.append(result)
        if len(results) >= args.max_reports:
            break
    print(json.dumps({"checked_reports": len(results), "results": results, "new_model_calls": 0}, indent=2))


if __name__ == "__main__":
    main()
