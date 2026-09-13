"""ARIA-informed regressions through the actual screening admission path."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from directorloop.screening.models import ScreenJudgment, ScreenReport, ScreenWindow
from directorloop.screening.runner import _asr_quote_matches, _screen_prefix, validate_evidence


def judgment_data(quote=None, timestamps=None, *, kind="asr_claim", attention="unknown"):
    return {
        "understanding": "A claim requires evidence.",
        "observations": [{"text": "An assertion from the model.", "kind": kind,
                          "frame_timestamps_ms": timestamps or [], "asr_quote": quote}],
        "attention_risk": attention, "cause_observation_indices": [0] if attention != "unknown" else [],
        "suggestion": "Check the supplied evidence.", "moment_kind": "unknown", "uncertainties": [],
    }


@pytest.mark.parametrize(("quote", "transcript"), [
    ("rice", "The price is high."), ("egg", "An eggplant is visible."),
    ("link", "Do not blink."), ("...", "Wait ... now."), ("   ", "Some words"),
    ("你好", "An unrelated English sentence."), ("s", "it's done"),
    ("t", "isn’t ready"), ("1.2", "1,2"), ("hello world", "hello, world"),
    ("cafe", "café"), ("a", "a\u0338"), ("subject", "subject_name"),
    ("1", "1.5"), ("5", "1.5"), ("1", "1,000"), ("5", "-5"),
    ("5", "−5"), ("5", "+5"), ("2", "1/2"), ("5", "1e-5"),
])
def test_partial_words_empty_quotes_and_changed_punctuation_cannot_anchor_attention(quote, transcript):
    judgment = ScreenJudgment.model_validate(judgment_data(quote, attention="high"))
    issues = validate_evidence(judgment, [], transcript)
    assert "Observation 1: quote is absent from the supplied ASR prefix" in issues
    assert "Attention label has no anchored non-unknown observation; use unknown" in issues


@pytest.mark.parametrize(("quote", "transcript"), [
    ("rice", "The rice is cooked."), ("rice", "price rice"),
    ("rice", "He said 'rice'."), ("it's done", "Now it's done."),
    ("isn’t ready", "It isn’t ready yet."), ("hello world", "hello\n  world"),
    ("café", "A cafe\u0301 is open."), ("世界", "你好，世界！"),
    ("नमस्ते", "वह नमस्ते कहता है"), ("1.2", "value 1.2 units"),
    ("-5", "value -5 units"), ("1,000", "count 1,000"),
    ("1", "1, then 2"), ("5", "value 5. Next"),
])
def test_literal_unicode_quotes_and_whitespace_variants_remain_valid(quote, transcript):
    judgment = ScreenJudgment.model_validate(judgment_data(quote, attention="low"))
    assert validate_evidence(judgment, [], transcript) == []


@pytest.mark.parametrize("timestamp", [True, False, 1.0, "1"])
def test_provider_schema_rejects_coerced_timestamps(timestamp):
    with pytest.raises(ValidationError):
        ScreenJudgment.model_validate(judgment_data(timestamps=[timestamp], kind="visible_fact"))


@pytest.mark.parametrize("timestamp", [-1, 2001, 999999])
def test_negative_or_unsupplied_frames_fail_membership(timestamp):
    judgment = ScreenJudgment.model_validate(judgment_data(timestamps=[timestamp], kind="visible_fact"))
    assert validate_evidence(judgment, [1, 1000], "") == ["Observation 1: frame citation was not supplied"]


def test_invalid_trusted_frame_inputs_cannot_legalize_a_citation():
    judgment = ScreenJudgment.model_validate(judgment_data(timestamps=[-1, 1], kind="visible_fact"))
    assert validate_evidence(judgment, [-1, True], "") == ["Observation 1: frame citation was not supplied"]


def test_claim_with_valid_citation_remains_unverified_and_cannot_authorize_edit():
    judgment = ScreenJudgment.model_validate(judgment_data("hello"))
    judgment.observations[0].text = "This invention is guaranteed to go viral."
    assert validate_evidence(judgment, [], "hello") == []
    report = ScreenReport(id="screen_control", video_id="v", artifact_path="unused", created_at="unused",
                          windows=[ScreenWindow(start_ms=0, end_ms=1000, judgment=judgment)])
    assert report.semantic_grounding_verified is False
    assert report.windows[0].semantic_grounding_verified is False
    assert report.automatic_edit_allowed is False
    assert report.review_required is True


def test_citation_failure_is_persistable_with_raw_provider_evidence():
    from types import SimpleNamespace

    from directorloop.providers.base import CompletionResult

    raw = judgment_data("rice", attention="high")

    class Provider:
        def judge_json(self, *args):
            return CompletionResult(data=raw, model="offline-test", input_tokens=1, output_tokens=1, latency_ms=1)

    window = ScreenWindow(start_ms=0, end_ms=1000, prefix_asr_text="price")
    result = _screen_prefix(Provider(), SimpleNamespace(instruction="", media=None), window, lambda: None)
    assert result.status == "needs_review"
    assert result.raw_output == raw
    assert result.judgment is not None
    assert len(result.validation_issues) == 2
    assert result.semantic_grounding_verified is False
    assert not _asr_quote_matches("rice", "price")
