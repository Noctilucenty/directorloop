from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from PIL import Image

from directorloop.jobs.worker import JobInterrupted
from directorloop.media.frames import SampledFrame
from directorloop.media.transcribe import Transcript, TranscriptSegment
from directorloop.providers.base import CompletionResult, ProviderError
from directorloop.screening import list_screenings, load_screening, run_screening, runner, save_screening
from directorloop.screening.mechanical import measure_frame_changes
from directorloop.screening.models import ScreenJudgment, ScreenWindow


def reply(timestamp=166, **overrides):
    return {"understanding": "A covered pan is being presented.",
            "observations": [{"text": "A board covers a pan.", "kind": "visible_fact", "frame_timestamps_ms": [timestamp], "asr_quote": None}],
            "attention_risk": "unknown", "cause_observation_indices": [], "suggestion": "Check the reveal before judging the loaf.",
            "moment_kind": "setup", "uncertainties": ["The loaf is concealed."], **overrides}


class Frames:
    def __init__(self, path, duration_ms):
        pass

    def many(self, times, width):
        return [SampledFrame(t, f"frame-{t}-{width}".encode(), width, width * 2) for t in times]


class Provider:
    capability = SimpleNamespace(name="wandb_inference", model="Qwen/test")
    enable_thinking = False
    max_output_tokens = 2048

    def __init__(self, behavior=None):
        self.calls = []
        self.behavior = behavior

    def judge_json(self, media, instruction, schema):
        self.calls.append((media, instruction, schema))
        data = self.behavior(len(self.calls), media) if self.behavior else reply(media.frames[-1].timestamp_ms)
        return CompletionResult(data=data, latency_ms=12, model="Qwen/test", input_tokens=100, output_tokens=50)


@pytest.fixture
def media_setup(tmp_path, monkeypatch):
    path = tmp_path / "source.mp4"
    path.write_bytes(b"immutable video")
    monkeypatch.setattr(runner, "inspect_media", lambda _: SimpleNamespace(duration_ms=6000, fps=30, width=448, height=796, has_audio=False))
    monkeypatch.setattr(runner, "FrameCache", Frames)
    return path, tmp_path / "data"


def test_three_prefixes_and_short_video_deduplication():
    assert runner.screening_windows(32000) == [(0, 2000), (2000, 4000), (30000, 32000)]
    assert runner.screening_windows(4000) == [(0, 2000), (2000, 4000)]
    assert runner.screening_windows(1500) == [(0, 1500)]
    with pytest.raises(ValueError):
        runner.screening_windows(180001)


def test_valid_anchors_never_claim_semantic_truth(media_setup):
    path, data_dir = media_setup
    provider = Provider()
    report = run_screening("video_test", path, provider, data_dir, screening_id="screen_valid")
    assert report.status == "complete"
    assert len(provider.calls) == report.model_calls == 3
    assert report.mode == "fresh"
    assert report.review_required is True
    assert report.semantic_grounding_verified is False
    assert report.automatic_edit_allowed is False
    assert all(w.semantic_grounding_verified is False and w.status == "complete" for w in report.windows)
    assert report.input_tokens == 300
    assert report.output_tokens == 150
    assert len(report.protocol_fingerprint) == 64
    assert "retention" not in report.model_dump()
    assert load_screening(data_dir, report.id).model_dump() == report.model_dump()
    assert [r.id for r in list_screenings(data_dir)] == [report.id]
    assert all(f.path.endswith(".jpg") for w in report.windows for f in w.evidence_frames)
    assert all("source.mp4" not in instruction and "video_test" not in instruction for _, instruction, _ in provider.calls)


def test_invalid_frame_and_missing_asr_quote_require_review(media_setup):
    path, data_dir = media_setup
    provider = Provider(lambda _, media: reply(observations=[
        {"text": "The loaf is visible.", "kind": "visible_fact", "frame_timestamps_ms": [999999], "asr_quote": None},
        {"text": "The audio claims perfection.", "kind": "asr_claim", "frame_timestamps_ms": [], "asr_quote": "perfect loaf"},
    ]))
    report = run_screening("video", path, provider, data_dir)
    assert report.status == "needs_review"
    assert len(provider.calls) == 3
    assert all(len(w.validation_issues) == 2 for w in report.windows)
    assert report.windows[0].raw_output["observations"][0]["frame_timestamps_ms"] == [999999]


def test_caption_claim_is_not_hidden_object_proof(media_setup):
    path, data_dir = media_setup
    provider = Provider(lambda _, media: reply(media.frames[0].timestamp_ms, observations=[
        {"text": "Caption says the bread came out perfect.", "kind": "caption_claim", "frame_timestamps_ms": [media.frames[0].timestamp_ms], "asr_quote": None},
        {"text": "The bread is concealed by the board.", "kind": "unknown", "frame_timestamps_ms": [media.frames[-1].timestamp_ms], "asr_quote": None},
    ]))
    report = run_screening("video", path, provider, data_dir)
    assert report.status == "complete"
    assert report.windows[0].judgment.observations[0].kind == "caption_claim"
    assert report.windows[0].judgment.observations[1].kind == "unknown"
    assert "Opaque containers, covers and occlusion hide contents" in provider.calls[0][1]
    assert "loaf of bread" not in provider.calls[0][1]
    assert report.windows[0].semantic_grounding_verified is False


def test_asr_quote_must_be_in_supplied_prefix_not_future():
    data = reply(observations=[{"text": "Transcript says hello.", "kind": "asr_claim", "frame_timestamps_ms": [], "asr_quote": "hello world"}])
    judgment = ScreenJudgment.model_validate(data)
    assert runner.validate_evidence(judgment, [166], "hello") == ["Observation 1: quote is absent from the supplied ASR prefix"]
    assert runner.validate_evidence(judgment, [166], "hello world") == []


def test_failed_provider_stops_and_preserves_prior_prefix(media_setup):
    path, data_dir = media_setup

    def fail_second(index, media):
        if index == 2:
            raise ProviderError("provider credits exhausted; no automatic retry")
        return reply(media.frames[0].timestamp_ms)

    provider = Provider(fail_second)
    report = run_screening("video", path, provider, data_dir)
    assert report.status == "failed"
    assert [w.status for w in report.windows] == ["complete", "failed", "not_attempted"]
    assert len(provider.calls) == 2
    assert "credits exhausted" in report.error
    assert report.input_tokens is None
    assert load_screening(data_dir, report.id).windows[0].raw_output


def test_bad_schema_retains_raw_and_does_not_trigger_edit(media_setup):
    path, data_dir = media_setup
    report = run_screening("video", path, Provider(lambda *_: {"understanding": "A pan", "unexpected": 1}), data_dir)
    assert report.status == "needs_review"
    assert report.windows[0].raw_output["unexpected"] == 1
    assert report.windows[0].judgment is None
    assert report.automatic_edit_allowed is False


def test_last_endcard_high_risk_is_separate_from_content_suggestion(media_setup):
    path, data_dir = media_setup
    provider = Provider(lambda _, media: reply(media.frames[0].timestamp_ms, moment_kind="endcard", attention_risk="high", cause_observation_indices=[0]))
    report = run_screening("video", path, provider, data_dir)
    assert [w.attention_context for w in report.windows] == ["content", "content", "last_endcard"]
    assert report.automatic_edit_allowed is False


def test_cancel_after_first_reply_preserves_it_and_stops(media_setup):
    path, data_dir = media_setup
    provider = Provider()
    report = run_screening("video", path, provider, data_dir, is_cancelled=lambda: len(provider.calls) >= 1)
    assert report.status == "canceled"
    assert len(provider.calls) == 1
    assert [w.status for w in report.windows] == ["complete", "not_attempted", "not_attempted"]


def test_lease_interruption_callback_prevents_provider_dispatch(media_setup):
    path, data_dir = media_setup
    provider = Provider()

    def on_stage(stage, *_):
        if stage == "SCREENING_PREFIX":
            raise JobInterrupted("lease lost")

    report = run_screening("video", path, provider, data_dir, on_stage=on_stage)
    assert report.status == "failed"
    assert report.error == "worker execution interrupted; not retried"
    assert provider.calls == []


def test_duplicate_id_never_replays_paid_calls(media_setup):
    path, data_dir = media_setup
    provider = Provider()
    run_screening("video", path, provider, data_dir, screening_id="screen_same")
    with pytest.raises(FileExistsError):
        run_screening("video", path, provider, data_dir, screening_id="screen_same")
    assert len(provider.calls) == 3


def test_source_hash_and_protocol_change_are_visible(media_setup, monkeypatch):
    path, data_dir = media_setup
    one = run_screening("video", path, Provider(), data_dir)
    monkeypatch.setattr(runner, "SCREENING_VERSION", "grounded-screening-v-next")
    two = run_screening("video", path, Provider(), data_dir)
    assert one.artifact_hash == two.artifact_hash
    assert one.protocol_fingerprint != two.protocol_fingerprint
    two.status = "failed"
    save_screening(data_dir, two)
    assert load_screening(data_dir, two.id).status == "failed"


def test_prefix_excludes_words_that_finish_after_boundary(media_setup, monkeypatch):
    path, data_dir = media_setup
    transcript = Transcript(text="hello future", segments=[TranscriptSegment(0, 1000, "hello"), TranscriptSegment(2100, 2500, "future")])
    monkeypatch.setattr(runner, "_transcript", lambda *_: (transcript, "cached", {"test": True}))
    provider = Provider()
    report = run_screening("video", path, provider, data_dir)
    assert report.windows[0].prefix_asr_text == "hello"
    assert report.windows[1].prefix_asr_text == "hello future"
    assert "future" not in provider.calls[0][1].split("This is a screening suggestion")[0]
    assert report.asr_cache_mode == "cached"


def image_frame(timestamp, value, width=64):
    buffer = io.BytesIO()
    Image.new("L", (width, width), value).save(buffer, "JPEG")
    return SampledFrame(timestamp, buffer.getvalue(), width, width)


def test_pixel_measurement_retains_real_gaps_and_does_not_measure_attention():
    result = measure_frame_changes([image_frame(0, 0), image_frame(333, 0), image_frame(1999, 255)], 0, 2000)
    assert result.status == "measured"
    assert [p.gap_ms for p in result.pairs] == [333, 1666]
    assert [p.mean_absolute_luma_delta for p in result.pairs] == [0, 255]
    assert [p.changed_pixel_fraction for p in result.pairs] == [0, 1]
    assert result.mean_absolute_luma_delta == 127.5
    assert result.evidence_level == "mechanical"
    assert "camera" in result.interpretation.lower()
    assert "risk" not in result.model_dump()


def test_pixel_comparison_excludes_context_and_future_and_deduplicates_resolution():
    result = measure_frame_changes([image_frame(166, 255), image_frame(2100, 255, 32), image_frame(2100, 0, 64),
                                    image_frame(2500, 0), image_frame(4000, 255)], 2000, 4000)
    assert result.sample_timestamps_ms == [2100, 2500]
    assert result.mean_absolute_luma_delta == 0
    assert len(result.sample_sha256) == 2


def test_pixel_missing_or_corrupt_images_are_unknown_not_zero():
    one = measure_frame_changes([image_frame(166, 0)], 0, 2000)
    assert one.status == "insufficient_frames" and one.mean_absolute_luma_delta is None
    bad = measure_frame_changes([image_frame(166, 0), SampledFrame(500, b"bad", 64, 64)], 0, 2000)
    assert bad.status == "unavailable" and bad.mean_changed_pixel_fraction is None
    assert bad.pairs == []


def test_old_window_without_mechanical_field_remains_loadable():
    assert ScreenWindow.model_validate({"start_ms": 0, "end_ms": 2000}).mechanical_visual is None


def test_unknown_and_uncited_inferences_cannot_alone_support_specific_attention():
    for kind in ("unknown", "inference"):
        data = reply(observations=[{"text": "Contents are unclear.", "kind": kind, "frame_timestamps_ms": [], "asr_quote": None}],
                     attention_risk="high", cause_observation_indices=[0])
        assert "Attention label has no anchored non-unknown observation; use unknown" in runner.validate_evidence(ScreenJudgment.model_validate(data), [166], "")


def test_claim_source_mixing_and_duplicate_cause_references_are_flagged():
    data = reply(observations=[{"text": "A pan is visible and the narrator calls it perfect.", "kind": "visible_fact", "frame_timestamps_ms": [166, 166], "asr_quote": "perfect"}],
                 attention_risk="high", cause_observation_indices=[0, 0])
    issues = runner.validate_evidence(ScreenJudgment.model_validate(data), [166], "perfect")
    assert len(issues) == 3
    assert any("split image and transcript" in issue for issue in issues)
    assert any("duplicated" in issue for issue in issues)
    assert any("repeats" in issue for issue in issues)


def test_true_anchor_does_not_claim_semantic_atomicity_or_truth(media_setup):
    path, data_dir = media_setup
    report = run_screening("video", path, Provider(), data_dir)
    assert report.protocol["version"] == "grounded-screening-v3"
    assert report.protocol["evidence_validation"]["version"] == "aria-informed-citation-guards-v1"
    assert report.protocol["mechanical_visual"]["attention_or_retention_measurement"] is False
    assert all(w.mechanical_visual is not None for w in report.windows)
    assert report.semantic_grounding_verified is False
