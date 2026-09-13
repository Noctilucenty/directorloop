"""The outcome gate is tested with explicit synthetic fixtures, never user labels."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from directorloop.backtest import runner
from directorloop.backtest.features import canonical_hash
from directorloop.backtest.prediction import append_prediction, build_prediction_record
from directorloop.backtest.runner import (
    admit_cohort,
    compare_predictions,
    implementation_hashes,
    run_backtest,
    seal_predictions,
    unblind_metrics,
    write_json,
)
from directorloop.domain.ids import sha256_file


def fixture(tmp_path):
    source = tmp_path / "synthetic-fixture.mp4"
    source.write_bytes(b"synthetic gate fixture; no real-media evaluation")
    sha = sha256_file(source)
    identity = {"media_id": "neutral_01", "instagram_post_id": "123", "account_id": "456", "artifact_sha256": sha}
    receipt = {**identity, "source_kind": "platform_served", "identity_verified": True,
               "full_decode_passed": True, "audio_video_complete": True}
    write_json(tmp_path / "receipt.json", receipt)
    row = {**identity, "local_path": str(source), "provenance_path": str(tmp_path / "receipt.json"), "provenance_sha256": sha256_file(tmp_path / "receipt.json")}
    selected = {k: v for k, v in identity.items() if k != "artifact_sha256"}
    selected.update(posted_at="2026-07-01T00:00:00Z", observation_id="snapshot_1")
    protocol = {"selected_observations": [selected]}
    mechanical = {"media_id": identity["media_id"], "artifact_sha256": sha, "duration_ms": 2000,
                  "features": {"duration_seconds": 2.0}, "windows": [], "extractor_fingerprint": "synthetic-test", "missingness": {}}
    prediction = build_prediction_record(media_id=identity["media_id"], artifact_sha256=sha, mechanical=mechanical)
    actual = {**selected, "views": 25, "reach": 20, "avg_watch_seconds": 1.4, "skip_rate": 0.5, "saves": None, "shares": 0}
    outcome_path = tmp_path / "outcomes.json"
    write_json(outcome_path, [actual])
    cohort, _ = admit_cohort(protocol, [row])
    frozen = {"cohort": cohort, "methods": [prediction["method_version"]], "outcome_file_sha256": sha256_file(outcome_path),
              "implementation_hashes": implementation_hashes()}
    return protocol, row, prediction, frozen, outcome_path


def test_near_match_local_master_is_excluded_and_exact_hash_checked(tmp_path):
    protocol, row, _, _, _ = fixture(tmp_path)
    path = Path(row["provenance_path"])
    receipt = json.loads(path.read_text())
    receipt["source_kind"] = "local_master_title_runtime_match"
    write_json(path, receipt)
    row["provenance_sha256"] = sha256_file(path)
    admitted, excluded = admit_cohort(protocol, [row])
    assert admitted == [] and "not verified published" in excluded[0]["reason"]
    Path(row["local_path"]).write_bytes(b"changed")
    assert "media hash mismatch" in admit_cohort(protocol, [row])[1][0]["reason"]


def test_post_identity_mismatch_rejected(tmp_path):
    protocol, row, _, _, _ = fixture(tmp_path)
    row["instagram_post_id"] = "789"
    assert not admit_cohort(protocol, [row])[0]


def test_prediction_missing_one_method_cannot_unblind(tmp_path):
    _, _, prediction, frozen, _ = fixture(tmp_path)
    path = tmp_path / "predictions.jsonl"
    append_prediction(path, prediction)
    frozen["methods"].append("current_directorloop_v1")
    with pytest.raises(ValueError, match="Every frozen"):
        seal_predictions(path, frozen)


def test_tampered_prediction_is_rejected_before_outcome_open(tmp_path, monkeypatch):
    _, _, prediction, frozen, outcome_path = fixture(tmp_path)
    path = tmp_path / "predictions.jsonl"
    append_prediction(path, prediction)
    seal = seal_predictions(path, frozen)
    altered = copy.deepcopy(prediction)
    altered["ranking_score"] = 999
    path.write_text(json.dumps(altered) + "\n")
    original = Path.read_text

    def guarded_read(self, *args, **kwargs):
        assert self != outcome_path, "outcomes must stay unread"
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read)
    with pytest.raises(ValueError, match="body hash mismatch"):
        unblind_metrics(outcome_path, path, frozen, seal)


def test_changed_actuals_or_implementation_refuses_unblind(tmp_path):
    _, _, prediction, frozen, outcome_path = fixture(tmp_path)
    path = tmp_path / "predictions.jsonl"
    append_prediction(path, prediction)
    seal = seal_predictions(path, frozen)
    outcome_path.write_text("[]")
    with pytest.raises(ValueError, match="Outcome source changed"):
        unblind_metrics(outcome_path, path, frozen, seal)
    frozen["implementation_hashes"]["runner.py"] = "changed"
    seal["frozen_sha256"] = canonical_hash(frozen)
    with pytest.raises(ValueError, match="Implementation changed"):
        unblind_metrics(outcome_path, path, frozen, seal)


def test_exact_observation_join_keeps_missing_distinct_from_zero(tmp_path):
    _, _, prediction, frozen, outcome_path = fixture(tmp_path)
    path = tmp_path / "predictions.jsonl"
    append_prediction(path, prediction)
    actual = unblind_metrics(outcome_path, path, frozen, seal_predictions(path, frozen))[0]
    assert actual["views"] == 25
    assert actual["saves_per_1000_reach"] is None and actual["shares_per_1000_reach"] == 0
    assert actual["completion_rate"] is None


def test_no_verified_media_preserves_blocked_run_and_trace_stages_without_reading_labels(tmp_path, monkeypatch):
    protocol, _, _, _, outcome_path = fixture(tmp_path)
    write_json(tmp_path / "protocol.json", protocol)
    write_json(tmp_path / "manifest.json", [])
    original = Path.read_text

    def guarded_read(self, *args, **kwargs):
        assert self != outcome_path, "empty cohort must not open outcomes"
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read)
    run = run_backtest(protocol_path=tmp_path / "protocol.json", media_manifest_path=tmp_path / "manifest.json",
                       outcomes_path=outcome_path, outcomes_sha256="0" * 64, data_dir=tmp_path / "data")
    assert run.status == "blocked" and not run.outcomes_unblinded
    assert run.selected_posts == 1 and run.admitted_posts == 0
    assert [s.status for s in run.workflow_stages] == ["incomplete", "skipped"]
    assert run.budget["model_calls"] == 0
    saved = json.loads((tmp_path / "data" / "backtests" / run.id / "run.json").read_text())
    assert all(s["ended_at"] for s in saved["workflow_stages"])


def test_rank_score_does_not_acquire_fake_view_mae_or_retention_curve():
    predictions = [{"media_id": "a", "method_version": "screen", "ranking_score": 1, "window_predictions": []}]
    actuals = [{"media_id": "a", "views": 100, "retention_points": [], "source_kind": "synthetic"}]
    result = compare_predictions(predictions, actuals)
    assert "mae" not in result["uncalibrated_rank_associations"]["screen"]["views"]
    assert result["timestamp_window_comparison"]["n"] == 0


def test_full_orchestration_seals_predictions_before_opening_outcomes(tmp_path, monkeypatch):
    protocol, media, prediction, _, outcome_path = fixture(tmp_path)
    write_json(tmp_path / "protocol.json", protocol)
    write_json(tmp_path / "manifest.json", [media])
    measurements = {"media_id": media["media_id"], "artifact_sha256": media["artifact_sha256"], "duration_ms": 2000,
                    "features": prediction["features"], "windows": [], "extractor_fingerprint": "synthetic-test", "missingness": {}}
    # This is a synthetic pipeline contract test; real FFmpeg extraction has separate integration coverage.
    measurements["features"] = {"duration_seconds": 2.0}
    monkeypatch.setattr(runner, "extract_mechanical_features", lambda *a, **kw: measurements)
    original = Path.read_text
    opened = []

    def guarded_read(self, *args, **kwargs):
        if self == outcome_path:
            seals = list((tmp_path / "data" / "backtests").glob("*/prediction-seal.json"))
            assert len(seals) == 1
            seal = json.loads(original(seals[0]))
            assert seal["prediction_count"] == 1
            opened.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read)
    run = run_backtest(protocol_path=tmp_path / "protocol.json", media_manifest_path=tmp_path / "manifest.json",
                       outcomes_path=outcome_path, outcomes_sha256=sha256_file(outcome_path), data_dir=tmp_path / "data")
    assert run.status == "complete_mechanical_only" and run.outcomes_unblinded
    assert opened == [outcome_path] and run.budget["model_calls"] == 0
    result = json.loads(Path(run.results_path).read_text())
    assert result["views"]["systems"]["account_median_v1"]["accuracy"]["n"] == 0
    assert all(s.ended_at for s in run.workflow_stages)


def test_extraction_failure_never_unblinds_a_smaller_cohort(tmp_path, monkeypatch):
    protocol, media, _, _, outcome_path = fixture(tmp_path)
    write_json(tmp_path / "protocol.json", protocol)
    write_json(tmp_path / "manifest.json", [media])

    def unavailable(*args, **kwargs):
        raise RuntimeError("synthetic extraction failure")

    monkeypatch.setattr(runner, "extract_mechanical_features", unavailable)
    run = run_backtest(protocol_path=tmp_path / "protocol.json", media_manifest_path=tmp_path / "manifest.json",
                       outcomes_path=outcome_path, outcomes_sha256=sha256_file(outcome_path), data_dir=tmp_path / "data")
    assert run.status == "failed" and run.admitted_posts == 1 and not run.outcomes_unblinded
    assert run.persisted_predictions == 0
    assert all(s.ended_at for s in run.workflow_stages)
