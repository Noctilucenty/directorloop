import copy
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from directorloop.backtest import features as f
from directorloop.backtest import prediction as p
from directorloop.domain.ids import sha256_file
from directorloop.media.probe import FFMPEG
from directorloop.media.transcribe import Transcript, TranscriptSegment

SHA = "a" * 64


def mechanical():
    value = f.summarize_measurements(duration_ms=4000, cuts_ms=[2000],
        motion=[(t, 0.02) for t in range(100, 4001, 100)], rms=np.full(400, 0.1),
        transcript=Transcript(text="one two", segments=[TranscriptSegment(0, 2000, "one two")]))
    return {**value, "media_id": "blind_001", "artifact_sha256": SHA, "duration_ms": 4000,
            "extractor_fingerprint": "fixture-v1", "limitations": []}


def audit():
    return {"artifact_hash": SHA, "id": "audit_fixture", "status": "complete", "primed_with": [],
            "duration_ms": 4000, "created_at": "2026-09-13T00:00:00Z",
            "attention": {"evaluator": {"model": "fixture", "asr_language": "auto"}, "segments": [
                {"start_ms": 0, "end_ms": 2000, "scan": "coarse", "attention_risk": "low", "reaction": "engaged"},
                {"start_ms": 2000, "end_ms": 2500, "scan": "precision", "attention_risk": "high", "reaction": "confused"},
                {"start_ms": 2500, "end_ms": 4000, "scan": "coarse", "attention_risk": "medium", "reaction": "neutral"}]},
            "window_reactions": [], "precision_reactions": [],
            "audience": {"continue_watching": {"verdict": "YES"}, "send": {"verdict": "MAYBE"}, "save_or_replay": {"verdict": "NO"}}}


def prediction():
    return p.build_prediction_record(media_id="blind_001", artifact_sha256=SHA, mechanical=mechanical())


def test_wrong_media_hash_stops_before_extraction(tmp_path, monkeypatch):
    path = tmp_path / "neutral.mp4"
    path.write_bytes(b"changed bytes")
    monkeypatch.setattr(f, "inspect_media", lambda _: pytest.fail("must not probe mismatched media"))
    with pytest.raises(ValueError, match="hash mismatch"):
        f.extract_mechanical_features(path, media_id="blind_001", expected_sha256=SHA)


def test_missing_audio_and_asr_stay_unknown():
    value = f.summarize_measurements(duration_ms=2000, cuts_ms=[], motion=[(100, 0.1)], rms=None, transcript=None)
    assert value["features"]["cut_rate_per_second"] == 0
    assert value["features"]["speech_words_per_second"] is None
    assert value["features"]["silence_fraction"] is None
    missing_asr = f.summarize_measurements(duration_ms=2000, cuts_ms=[], motion=[], rms=None,
        transcript=Transcript(text="", has_speech=False, note="whisper.cpp unavailable; transcript missing"))
    assert missing_asr["features"]["speech_words_per_second"] is None


def test_prefix_audio_measurement_cannot_see_future_loudness():
    initial = np.array([0.01] * 100 + [0.1] * 100 + [0.1] * 200)
    changed_future = initial.copy()
    changed_future[200:] = 1.0
    args = dict(duration_ms=4000, cuts_ms=[], motion=[], transcript=None)
    first = f.summarize_measurements(rms=initial, **args)["windows"][0]
    second = f.summarize_measurements(rms=changed_future, **args)["windows"][0]
    assert first["silence_fraction"] == second["silence_fraction"] == 0.5


def test_word_window_uses_completed_word_boundary():
    transcript = Transcript(text="one two", segments=[TranscriptSegment(0, 3000, "one two")])
    value = f.summarize_measurements(duration_ms=4000, cuts_ms=[], motion=[], rms=None, transcript=transcript)
    assert [w["word_count"] for w in value["windows"]] == [1, 1]
    assert f.overlap_ms([(0, 2000), (1500, 3500)], 0, 4000) == 3500


def test_unknown_signals_do_not_become_low_risk():
    value = mechanical()
    value["windows"][0]["silence_fraction"] = None
    result = p.mechanical_window_predictions(value)[0]
    assert result["risk_index"] is None
    assert result["attention_risk"] is None
    assert result["exit_probability"] is None
    assert result["partial_feature_sum"] is not None


def test_model_requires_hash_unprimed_and_explicit_blind_provenance():
    with pytest.raises(ValueError, match="provenance"):
        p.model_features_from_audit(audit(), expected_sha256=SHA)
    wrong = audit()
    wrong["artifact_hash"] = "b" * 64
    with pytest.raises(ValueError, match="exact artifact"):
        p.model_features_from_audit(wrong, expected_sha256=SHA, blind_verified=True)
    primed = audit()
    primed["primed_with"] = ["historical performance"]
    with pytest.raises(ValueError, match="primed"):
        p.model_features_from_audit(primed, expected_sha256=SHA, blind_verified=True)


def test_effective_timeline_duration_weights_without_double_counting():
    result = p.model_features_from_audit(audit(), expected_sha256=SHA, blind_verified=True)
    assert result["features"]["model_high_risk_fraction"] == 0.125
    assert result["features"]["model_confusion_fraction"] == 0.125
    assert result["features"]["model_first_risk_fraction"] == 0.5
    assert result["features"]["model_continue_score"] == 2
    assert result["features"]["model_share_score"] == 1
    assert result["features"]["model_replay_score"] == 0
    assert all(w["exit_probability"] is None for w in result["window_predictions"])
    bad = audit()
    bad["attention"]["segments"][1]["start_ms"] = 1900
    with pytest.raises(ValueError, match="overlaps"):
        p.model_features_from_audit(bad, expected_sha256=SHA, blind_verified=True)


def test_unavailable_models_and_performance_ranges_stay_null():
    record = prediction()
    assert record["source_audit_id"] is None
    assert not record["model_inference_present"]
    assert all(record["features"][k] is None for k in p.MODEL_FEATURE_NAMES)
    assert all(value is None for value in record["prediction_ranges"].values())
    forbidden = mechanical()
    forbidden["features"]["instagram_views"] = 999
    with pytest.raises(ValueError, match="Unexpected feature"):
        p.build_prediction_record(media_id="blind_001", artifact_sha256=SHA, mechanical=forbidden)


def test_unknown_reaction_is_not_zero_confusion():
    value = audit()
    for segment in value["attention"]["segments"]:
        segment["reaction"] = "unknown"
    result = p.model_features_from_audit(value, expected_sha256=SHA, blind_verified=True)
    assert result["features"]["model_confusion_fraction"] is None
    assert result["features"]["model_high_risk_fraction"] == 0.125


def test_append_readback_idempotence_and_tamper_rejection(tmp_path):
    target = tmp_path / "predictions.jsonl"
    record = prediction()
    first = p.append_prediction(target, record)
    second = p.append_prediction(target, record)
    assert first["prediction_sha256"] == second["prediction_sha256"]
    assert second["reused_existing"]
    assert len(target.read_text().splitlines()) == 1
    tampered = copy.deepcopy(record)
    tampered["ranking_score"] = 123
    with pytest.raises(ValueError, match="changed after hashing"):
        p.append_prediction(target, tampered)
    revised_mechanics = mechanical()
    revised_mechanics["features"]["mean_motion"] = .9
    revised = p.build_prediction_record(media_id="blind_001", artifact_sha256=SHA, mechanical=revised_mechanics)
    with pytest.raises(ValueError, match="Conflicting"):
        p.append_prediction(target, revised)


def test_concurrent_prediction_append_preserves_records(tmp_path):
    target = tmp_path / "predictions.jsonl"
    def save(index):
        value = mechanical()
        value["media_id"] = f"blind_{index:03}"
        rec = p.build_prediction_record(media_id=value["media_id"], artifact_sha256=SHA, mechanical=value)
        return p.append_prediction(target, rec)
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(save, range(8)))
    rows = [json.loads(line) for line in target.read_text().splitlines()]
    assert len(rows) == len(receipts) == 8
    assert len({r["media_id"] for r in rows}) == 8


def test_truncated_prediction_file_is_preserved(tmp_path):
    path = tmp_path / "predictions.jsonl"
    path.write_text('{"interrupted":')
    with pytest.raises(ValueError, match="truncated"):
        p.append_prediction(path, prediction())
    assert path.read_text() == '{"interrupted":'


def test_existing_prediction_body_is_rehashed_before_reuse(tmp_path):
    path = tmp_path / "predictions.jsonl"
    record = prediction()
    p.append_prediction(path, record)
    tampered = json.loads(path.read_text())
    tampered["ranking_score"] = 99
    path.write_text(json.dumps(tampered) + "\n")
    with pytest.raises(ValueError, match="tampered"):
        p.append_prediction(path, record)


def test_actual_video_extraction_without_audio(tmp_path):
    video = tmp_path / "neutral.mp4"
    subprocess.run([FFMPEG, "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=blue:s=64x96:r=10:d=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)], check=True, capture_output=True)
    result = f.extract_mechanical_features(video, media_id="blind_001", expected_sha256=sha256_file(video))
    assert result["features"]["duration_seconds"] == 1
    assert result["features"]["cut_rate_per_second"] == 0
    assert result["features"]["mean_motion"] == 0
    assert result["features"]["speech_words_per_second"] is None
    assert result["features"]["mean_loudness"] is None
    assert "neutral.mp4" not in json.dumps(result)
