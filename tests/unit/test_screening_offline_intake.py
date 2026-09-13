"""Offline isolation and identity checks use synthetic files only."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from directorloop.backtest.admission import MANIFEST_FIELDS, inspect_admission
from directorloop.domain.ids import sha256_file
from scripts.freeze_screening_cohort import prior_identities, select_unattempted


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def test_admission_reports_exact_missing_fields_without_opening_private_labels(tmp_path, monkeypatch):
    protocol = {"selected_observations": [{"media_id": "neutral_01", "instagram_post_id": "123", "account_id": "456", "observation_id": "obs_1", "posted_at": "2026-01-01"}]}
    write(tmp_path / "protocol-v1.json", protocol)
    (tmp_path / "protocol-v1.sha256").write_text(sha256_file(tmp_path / "protocol-v1.json"))
    write(tmp_path / "published-media-manifest.json", [])
    write(tmp_path / "private-evaluation/actuals-all-checkpoints.json", [{"private": "must not open"}])
    original = Path.read_text

    def guarded(path, *args, **kwargs):
        assert "private-evaluation" not in str(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)
    report = inspect_admission(tmp_path)
    assert report["admitted_posts"] == 0 and report["selected_posts"] == 1
    assert report["outcome_values_opened"] is False and report["provider_calls"] == 0
    assert report["protocol_checksum_matches"] is True
    assert report["intake_rows"][0]["missing_manifest_fields"] == list(MANIFEST_FIELDS)
    assert report["accuracy_result_available"] is False


def test_old_blocked_run_cannot_acquire_comparison_eligibility(tmp_path):
    write(tmp_path / "protocol-v1.json", {"selected_observations": []})
    write(tmp_path / "published-media-manifest.json", [])
    write(tmp_path / "execution/backtests/backtest_old/run.json", {"status": "blocked", "persisted_predictions": 0, "outcomes_unblinded": False})
    report = inspect_admission(tmp_path)
    assert report["protocol_checksum_matches"] is False
    assert report["existing_runs"][0]["eligible_for_existing_prediction_comparison"] is False
    assert report["existing_runs"][0]["checks"]["predictions_exist"] is False


def candidates(tmp_path):
    media, rows = [], []
    for index, creator in enumerate(["seen", "newa", "newb", "newc"]):
        source = tmp_path / f"tiktok_{creator}_{index}.mp4"
        source.write_bytes(f"synthetic-{index}".encode())
        media.append({"media_id": f"media_{index}", "sha256": sha256_file(source), "duration_ms": 9000})
        rows.append({"media_id": f"media_{index}", "status": "pending"})
    return media, {"videos": rows}


def test_cohort_requires_pending_in_every_index_and_excludes_development_creators(tmp_path):
    media, index = candidates(tmp_path)
    earlier = {"videos": [dict(row) for row in index["videos"]]}
    earlier["videos"][1]["status"] = "failed"
    selected, _ = select_unattempted(media, [index, earlier], tmp_path, [{"source_path": "/x/tiktok_seen_other.mp4"}], 2)
    assert {r["creator_identity"] for r in selected} == {"tiktok_newb", "tiktok_newc"}
    assert all(r["prefix_end_ms"] == [2000, 4000, 9000] for r in selected)


def test_cohort_never_reads_sidecars_or_changes_source_bytes(tmp_path, monkeypatch):
    media, index = candidates(tmp_path)
    (tmp_path / "tiktok_seen_0.info.json").write_text("Private metrics and URLs must stay unread")
    original = Path.read_text

    def guarded(path, *args, **kwargs):
        assert not path.name.endswith(".info.json")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)
    selected, _ = select_unattempted(media, [index], tmp_path, [], 3)
    assert all(sha256_file(Path(row["source_path"])) == row["source_sha256"] for row in selected)


def test_started_pending_and_insufficient_cohort_fail_closed(tmp_path):
    media, index = candidates(tmp_path)
    index["videos"][0]["started_at"] = "2026-01-01"
    with pytest.raises(ValueError, match="Only 3"):
        select_unattempted(media, [index], tmp_path, [], 4)
    with pytest.raises(ValueError, match="At least one"):
        select_unattempted(media, [], tmp_path, [], 1)


def test_prior_pilot_staged_paths_also_exclude_creator():
    hashes, creators = prior_identities([{"sources": [{"staged_path": "/x/tiktok_development_123.mp4", "source_sha256": "a" * 64}]}])
    assert hashes == {"a" * 64} and creators == {"tiktok_development"}
