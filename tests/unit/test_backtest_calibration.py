"""Label separation, missingness, tied ranks and temporal scoring regressions."""
from __future__ import annotations

import copy

import pytest

from directorloop.backtest.calibration import (
    _evaluate_fitted,
    evaluate_systems,
    fit_system,
    frozen_split,
    predict_log,
)
from directorloop.backtest.metrics import (
    paired_metrics,
    ranks,
    residual_radius,
    retention_drop_windows,
    temporal_overlap,
)


def records(n=30):
    return [{"media_id": f"m{i:02}", "artifact_sha256": f"{i:064x}", "account_id": "same-account",
             "posted_at": f"2026-08-{i + 1:02}T12:00:00Z", "views": 100 + i * 10,
             "features": {"duration_seconds": 10 + i, "speech_words_per_second": 2.0}} for i in range(n)]


def test_missing_is_not_zero_and_rank_ties_are_averaged():
    assert ranks([4, 1, 4, 2]) == [3.5, 1.0, 3.5, 2.0]
    result = paired_metrics([None, 1, 2, 3], [9000, 2, 4, 6])
    assert result["n"] == 3 and result["mae"] == 2
    assert result["spearman"] == 1 and result["pairwise_accuracy"] == 1
    assert paired_metrics([1, 1, 1], [2, 3, 4])["spearman"] is None
    assert paired_metrics([1, 1, 1], [2, 3, 4])["pairwise_accuracy"] == 0.5


def test_too_few_calibration_examples_does_not_invent_narrow_interval():
    assert residual_radius([1, 2, 3], 0.9) is None
    assert residual_radius([1, 2, 3, 4], 0.8) == 4


def test_overlapping_windows_do_not_double_count():
    result = temporal_overlap([(0, 2000), (1000, 3000)], [(1000, 2500)])
    assert result["intersection_ms"] == 1500
    assert result["iou"] == 0.5 and result["observed_window_coverage"] == 1


def test_retention_drop_uses_rate_and_does_not_infer_curve_from_average():
    assert retention_drop_windows([]) == []
    result = retention_drop_windows([{"timestamp_ms": 0, "retained_fraction": 1},
                                    {"timestamp_ms": 1000, "retained_fraction": 0.8},
                                    {"timestamp_ms": 10000, "retained_fraction": 0.5}])
    assert result[0]["start_ms"] == 0 and result[0]["end_ms"] == 1000


def test_split_is_label_independent_and_exact_duplicates_rejected():
    data = records()
    before = frozen_split(data)
    for r in data:
        r["views"] = 999999999
    assert frozen_split(data) == before
    data[-1]["artifact_sha256"] = data[0]["artifact_sha256"]
    with pytest.raises(ValueError, match="duplicate"):
        frozen_split(data)


def test_heldout_targets_cannot_change_predictions_weights_or_ranges():
    data = records()
    split = frozen_split(data)
    a = evaluate_systems(data, split)["systems"]["duration_baseline_v1"]
    changed = copy.deepcopy(data)
    for r in changed:
        if r["media_id"] in split["test"]:
            r["views"] *= 1000000
    b = evaluate_systems(changed, split)["systems"]["duration_baseline_v1"]
    assert a["model"] == b["model"]
    assert a["log_residual_radius"] == b["log_residual_radius"]
    assert [(r["predicted"], r["lower"], r["upper"]) for r in a["rows"]] == [(r["predicted"], r["lower"], r["upper"]) for r in b["rows"]]
    assert a["accuracy"]["mae"] != b["accuracy"]["mae"]


def test_no_model_features_means_no_model_or_combined_benchmark_claim():
    report = evaluate_systems(records(), frozen_split(records()))
    for name in ["model_score_v1", "combined_score_v1", "performance_score_v2_candidate"]:
        assert report["systems"][name]["status"] == "unavailable"


def test_account_baseline_and_imputation_are_train_only():
    train = records(5)
    model = fit_system(train, "views", ["duration_seconds"])
    before = copy.deepcopy(model)
    unseen = {"media_id": "later", "account_id": "other", "views": 100000000,
              "features": {"duration_seconds": None}}
    assert predict_log(model, unseen) >= 0
    assert model == before and model["fallback_baseline"] == 120


def test_tiny_dataset_keeps_accuracy_unproven():
    report = evaluate_systems(records(2), frozen_split(records(2)))
    assert report["split"]["heldout_claim_allowed"] is False
    assert report["systems"]["account_median_v1"]["accuracy"]["n"] == 0
    assert report["promoted_system"] is None


def test_partition_protocol_does_not_claim_adequate_or_missing_test_labels():
    data = records(20)
    split = frozen_split(data)
    assert split["mode"] == "chronological_holdout"
    assert [len(split[k]) for k in ("train", "calibration", "test")] == [12, 4, 4]
    assert split["heldout_partition"] is True
    assert split["heldout_claim_allowed"] is False
    result = evaluate_systems(data, split)["systems"]["account_median_v1"]
    eligibility = result["evaluation_eligibility"]
    assert eligibility["valid_prediction_pair_n"] == 4
    assert eligibility["sample_floor_met"] is False
    assert eligibility["reliability_established"] is False
    for row in data:
        if row["media_id"] in split["test"]:
            row["views"] = None
    assert frozen_split(data) == split
    eligibility = evaluate_systems(data, split)["systems"]["account_median_v1"]["evaluation_eligibility"]
    assert eligibility["test_partition_n"] == eligibility["missing_or_invalid_test_label_n"] == 4
    assert eligibility["valid_test_label_n"] == eligibility["valid_prediction_pair_n"] == 0
    assert eligibility["estimates_available"] is False


def test_extreme_extrapolation_reports_overflow_instead_of_crashing_interval():
    model = {"account_baselines": {"one": 100}, "fallback_baseline": 100,
             "coefficients": [0, 1000], "features": ["duration_seconds"], "imputation": [0],
             "means": [0], "scales": [1], "target": "views"}
    cal = [{"media_id": f"c{i}", "account_id": "one", "views": 100,
            "features": {"duration_seconds": 0}} for i in range(4)]
    test = [{"media_id": "t", "account_id": "one", "views": 100, "features": {"duration_seconds": 1}}]
    report = _evaluate_fitted(model, cal, test, "views")
    row = report["rows"][0]
    assert row["predicted"] is row["lower"] is row["upper"] is None
    assert row["prediction_status"] == row["interval_status"] == "numerical_overflow"
    assert report["accuracy"]["n"] == 0
    assert report["evaluation_eligibility"]["valid_test_label_n"] == 1


def test_skip_rate_has_bounded_predictions_but_replay_watch_fraction_does_not():
    for target in ("skip_rate", "average_watch_fraction"):
        train = [{"media_id": f"m{i}", "account_id": "one", target: 0.2 + i * 0.1,
                  "features": {"duration_seconds": i}} for i in range(5)]
        invalid = {"media_id": "invalid", "account_id": "one", target: 2.0,
                   "features": {"duration_seconds": 5}}
        model = fit_system(train + [invalid], target, ["duration_seconds"])
        assert model["n_train"] == (5 if target == "skip_rate" else 6)
        test = [{"media_id": "test", "account_id": "one", target: 0.8,
                 "features": {"duration_seconds": 100}}]
        row = _evaluate_fitted(model, [], test, target)["rows"][0]
        if target == "skip_rate":
            assert 0 <= row["predicted"] <= 1
            assert row["lower"] == 0 and row["upper"] == 1
            assert row["interval_status"] == "uncalibrated_full_domain"
        else:
            assert row["predicted"] > 1
            assert row["upper"] is None
