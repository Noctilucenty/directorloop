import copy
import json

import pytest

from scripts.weave_online_checks import evidence_health, safe_score_inputs, sanitize_report
from scripts.weave_trace_regression import load_fixture


def sample_report():
    row = next(row for row in load_fixture()["rows"] if row["case_id"] == "trace:original:00:attention_supported")
    window = copy.deepcopy(row["case"]["window"])
    return {"id": "regression-example", "status": "complete", "duration_ms": 2000, "windows": [window]}


def test_valid_report_can_pass_mechanical_health_without_claiming_semantics():
    report = sample_report()
    before = copy.deepcopy(report)
    result = evidence_health(report)
    assert result["needs_review"] is False
    assert result["complete_time_coverage"] is True
    assert result["unavailable_scores"] == []
    assert result["native_signals"] is False
    assert result["semantic_grounding_verified"] is False
    assert result["new_model_calls"] == 0
    assert report == before


def test_missing_middle_is_not_counted_as_full_coverage():
    report = sample_report()
    report["duration_ms"] = 10000
    result = evidence_health(report)
    assert result["complete_time_coverage"] is False
    assert result["needs_review"] is True
    assert result["unavailable_scores"] == ["creative", "retention", "virality"]


def test_inference_citation_is_reported_as_blocked_attention():
    report = sample_report()
    report["windows"][0]["judgment"]["observations"][0]["kind"] = "inference"
    report["windows"][0]["judgment"]["cause_observation_indices"] = [0]
    result = evidence_health(report)
    assert result["blocked_attention_sections"] == [0]
    assert result["needs_review"] is True


def test_running_report_is_never_scored():
    report = sample_report()
    report["status"] = "running"
    with pytest.raises(ValueError, match="Only terminal"):
        evidence_health(report)


def test_report_redaction_strips_paths_and_raw_repair_attempts():
    report = sample_report()
    report["artifact_path"] = "/private/video.mp4"
    report["windows"][0].update({"raw_output": {"private": "payload"}, "review_attempts": ["payload"],
                                  "evidence_frames": [{"path": "/private/frame.jpg"}]})
    text = json.dumps(sanitize_report(report))
    for marker in ("/private/", "raw_output", "review_attempts", "evidence_frames"):
        assert marker not in text


def test_scorer_logging_does_not_duplicate_original_private_output():
    report = sanitize_report(sample_report())
    assert safe_score_inputs({"output": {"artifact_path": "/private/video.mp4", "review_attempts": []},
                              "report": report}) == {"report": report}
