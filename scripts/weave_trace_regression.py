"""Frozen failed-trace regression with optional native Weave publication.

No model inference, automatic edits, billing resets, or source-record mutations.
Run without --publish for an entirely local check; pytest also runs every row.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from pathlib import Path

import weave

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "trace_regression_v1.json"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def load_fixture(path=FIXTURE):
    data = json.loads(Path(path).read_text())
    if data.get("schema") != "directorloop-failed-trace-regression-v1":
        raise ValueError("Unexpected regression dataset schema")
    ids = [row["case_id"] for row in data["rows"]]
    if len(ids) != len(set(ids)) or not ids:
        raise ValueError("Regression cases must have unique, nonempty identifiers")
    for row in data["rows"]:
        if type(row["expected"]) is not bool or digest(row["case"]) != row["case_sha256"]:
            raise ValueError("Frozen regression case or label is invalid")
        if digest({key: value for key, value in row.items() if key != "row_sha256"}) != row["row_sha256"]:
            raise ValueError("Frozen regression label or provenance changed")
    return data


def aria_citation_after(case):
    """Exact mechanical rule from the independently replayed ARIA v0 artifact.

    This historical rule is intentionally distinct from the stricter current
    engine contract. Its frozen labels are never relabeled to fit a new rule.
    """
    quote = case.get("quote")
    if quote is not None:
        if not isinstance(quote, str) or not quote.strip():
            return False
        def tokenize(value):
            return re.findall(r"[^\W_]+(?:['’][^\W_]+)?", value.casefold(), flags=re.UNICODE)
        q, text = tokenize(quote), tokenize(case.get("transcript") or "")
        if not q or not any(text[i:i + len(q)] == q for i in range(len(text) - len(q) + 1)):
            return False
    frames = set(case["evidence_frames"])
    return all(type(t) is int and 0 <= t <= case["duration_ms"] and t in frames for t in case.get("timestamps", []))


def evaluate_case(case):
    """Replay a frozen rule input, not the video or its original model call."""
    rule = case["rule"]
    if rule == "aria_citation_after":
        return aria_citation_after(case["input"])
    from directorloop.screening.attention_admission import assess_attention
    from directorloop.screening.boundary import boundary_validation_issues
    from directorloop.screening.models import ScreenWindow
    from directorloop.screening.runner import validate_evidence
    from directorloop.screening.scoring import _anchored, _score_statement, _summary_reason

    window = ScreenWindow.model_validate(case["window"])
    if not window.judgment:
        raise ValueError("The frozen trace case must retain its original judgment")
    boundary = boundary_validation_issues(window.judgment.model_dump(), is_last_prefix=window.is_last_prefix)
    issues = validate_evidence(window.judgment, window.frame_timestamps_ms, window.prefix_asr_text)
    window.validation_issues = list(dict.fromkeys([*window.validation_issues, *issues, *boundary]))
    window.status = "needs_review" if window.validation_issues else "complete"
    if rule == "pending_payoff_detected":
        return bool(boundary)
    if rule == "attention_supported":
        return assess_attention(window)["status"] == "supported"
    if rule == "validation_issue_detected":
        return any(issue.startswith(case["issue_prefix"]) for issue in window.validation_issues)
    if rule == "score_sources_admitted":
        score = next(item for item in window.judgment.potential_scores if item.dimension == case["dimension"])
        return score.rating is not None and _score_statement(score.reason) and _anchored(window, score.observation_indices)
    if rule == "public_score_reason_admitted":
        score = next(item for item in window.judgment.potential_scores if item.dimension == case["dimension"])
        return _summary_reason(score.reason, window, score.observation_indices)
    raise ValueError("Unknown frozen regression rule")


def local_results(fixture):
    rows = [{"case_id": row["case_id"], "expected": row["expected"], "actual": evaluate_case(row["case"])}
            for row in fixture["rows"]]
    return {"cases": len(rows), "passed": sum(row["actual"] == row["expected"] for row in rows),
            "failures": [row for row in rows if row["actual"] != row["expected"]],
            "new_model_calls": 0, "scope": "Mechanical regression on frozen development examples, not semantic or audience accuracy."}


@weave.op(name="directorloop.regression.replay_frozen_case")
def replay_frozen_case(case: dict) -> dict:
    # Weave boxes JSON integers when reading a Dataset. Restore their original
    # JSON types before strict historical citation guards; preserve bool vs int.
    plain_case = json.loads(json.dumps(case, ensure_ascii=False))
    return {"actual": evaluate_case(plain_case), "new_model_calls": 0}


@weave.op(name="directorloop.regression.frozen_label_match")
def frozen_label_match(output: dict, expected: bool) -> dict:
    return {"passed": output["actual"] == expected, "expected": expected, "actual": output["actual"]}


async def publish(fixture, receipt_path):
    from weave.trace_server import trace_server_interface as tsi

    from directorloop.config import get_settings

    settings = get_settings()
    os.environ["WANDB_API_KEY"] = settings.wandb_api_key
    project = settings.weave_project_path()
    client = weave.init(project)
    # Verify that today's frozen records really point to stored calls. These
    # metadata reads never invoke the original ops or any model provider.
    call_ids = sorted({row["source_call_id"] for row in fixture["rows"] if row.get("source_call_id")})
    found = list(client.get_calls(filter={"call_ids": call_ids}, columns=["id", "ended_at"], limit=len(call_ids)))
    if {call.id for call in found} != set(call_ids) or any(call.ended_at is None for call in found):
        raise RuntimeError("A frozen failed-trace source is missing or unfinished in Weave")
    dataset = weave.Dataset(name="directorloop-failed-trace-regression-v1", rows=fixture["rows"],
                            description="43 immutable ARIA mechanical cases plus real failed review records; no human retention labels.")
    dataset_ref = weave.publish(dataset)
    evaluation = weave.Evaluation(name="directorloop-mechanical-regression-v1", dataset=dataset,
                                  scorers=[frozen_label_match], trials=1,
                                  metadata={"mode": "deterministic_replay", "new_model_calls": 0,
                                            "fixture_sha256": digest(fixture), "source_aria_artifact": fixture["aria_artifact"],
                                            "semantic_grounding_verified": False, "human_outcomes_measured": False})
    evaluation_ref = weave.publish(evaluation)
    # SDK 0.53 exposes .call on the unbound op, so pass self explicitly.
    result, call = await evaluation.evaluate.call(evaluation, replay_frozen_case)
    client._flush()
    stored_call = client.get_call(call.id, columns=["id", "ended_at", "exception", "summary", "output"])
    objects = client.server.objs_query(tsi.ObjQueryReq(project_id=project,
        filter=tsi.ObjectVersionFilter(object_ids=[dataset.name, evaluation.name], latest_only=True), metadata_only=True))
    receipt = {"project": project, "dataset_ref": dataset_ref.uri(), "evaluation_ref": evaluation_ref.uri(),
               "evaluation_call_id": call.id, "trace_url": f"https://wandb.ai/{project}/r/call/{call.id}",
               "evaluations_url": f"https://wandb.ai/{project}/weave/evaluations",
               "published_objects": [obj.object_id for obj in objects.objs],
               "remote_completed": stored_call.ended_at is not None, "remote_exception": stored_call.exception,
               "fixture_sha256": digest(fixture), "verified_source_calls": call_ids,
               "local": local_results(fixture), "weave_result": result,
               "signals_native_config_verified": False, "new_model_calls": 0}
    Path(receipt_path).write_text(json.dumps(receipt, indent=2, ensure_ascii=False, default=str) + "\n")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--receipt", type=Path, default=ROOT / "data" / "trace-regression-publication.json")
    args = parser.parse_args()
    fixture = load_fixture()
    results = local_results(fixture)
    if args.publish:
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        result = asyncio.run(publish(fixture, args.receipt))
        print(json.dumps({key: result[key] for key in ("trace_url", "evaluations_url", "local", "remote_completed", "new_model_calls")}, indent=2))
    else:
        print(json.dumps(results, indent=2))
    passed = results["passed"] == results["cases"]
    if args.publish:
        native_score = result["weave_result"]["directorloop.regression.frozen_label_match"]["passed"]
        passed = passed and native_score["true_count"] == results["cases"] and result["remote_exception"] is None
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
