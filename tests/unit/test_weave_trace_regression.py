"""Every normal pytest run replays frozen failed-trace admission cases offline."""
import json
from collections import Counter

import pytest

from scripts.weave_trace_regression import (
    digest,
    evaluate_case,
    load_fixture,
    local_results,
    replay_frozen_case,
)


@pytest.mark.parametrize("row", load_fixture()["rows"], ids=lambda row: row["case_id"])
def test_frozen_trace_and_aria_case(row, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("A deterministic regression attempted a network call")

    monkeypatch.setattr("socket.socket.connect", no_network)
    assert evaluate_case(row["case"]) is row["expected"], row["case_id"]


def test_frozen_dataset_labels_provenance_and_public_sanitization():
    fixture = load_fixture()
    # Changing labels requires an explicit reviewed fixture version, not a new
    # expected value calculated from the implementation under test.
    assert digest(fixture) == "3588d55419bb8008f44e64a5abeff74ded146c53bb474f35ac7b76408e537afc"
    assert Counter(row["source_kind"] for row in fixture["rows"]) == {
        "aria_artifact": 43, "recorded_trace": 11,
    }
    assert {row["expected"] for row in fixture["rows"]} == {True, False}
    text = json.dumps(fixture)
    for forbidden in ("/Users/", "artifact_path", "raw_output", "WANDB_API_KEY", "OPENAI_API_KEY"):
        assert forbidden not in text
    for row in fixture["rows"]:
        if row["source_kind"] == "recorded_trace":
            assert row["source_trace_url"].endswith(row["source_call_id"])
            assert len(row["source_record_sha256"]) == 64
            assert len(row["source_window_sha256"]) == 64
    assert local_results(fixture) == {
        "cases": 54, "passed": 54, "failures": [], "new_model_calls": 0,
        "scope": "Mechanical regression on frozen development examples, not semantic or audience accuracy.",
    }


@pytest.mark.parametrize("change", ["input", "label", "source"])
def test_tampered_case_label_or_provenance_fails_closed(tmp_path, change):
    fixture = load_fixture()
    row = fixture["rows"][0]
    if change == "input":
        row["case"]["input"]["transcript"] = "changed"
    elif change == "label":
        row["expected"] = not row["expected"]
    else:
        row["source_artifact"] = "different-artifact"
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(fixture))
    with pytest.raises(ValueError, match="Frozen regression"):
        load_fixture(path)


def test_unknown_rule_is_not_counted_as_passing():
    row = next(row for row in load_fixture()["rows"] if row["source_kind"] == "recorded_trace")
    with pytest.raises(ValueError, match="Unknown frozen regression rule"):
        evaluate_case({**row["case"], "rule": "unimplemented"})


def test_weave_dataset_boxed_numbers_preserve_strict_original_json_types():
    from weave.trace.box import BoxedInt

    row = load_fixture()["rows"][0]
    row["case"]["input"]["timestamps"] = [BoxedInt(t) for t in row["case"]["input"]["timestamps"]]
    assert replay_frozen_case(row["case"])["actual"] is True
    row["case"]["input"]["timestamps"] = [True]
    assert replay_frozen_case(row["case"])["actual"] is False
