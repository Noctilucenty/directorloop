"""Offline contracts for past-only evidence and stable declarative review inputs.

These checks prove input boundaries and serialization, not model compliance or
semantic truth. The provider below is a local stub and never performs inference.
"""

from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from directorloop.domain.ids import sha256_json
from directorloop.media.transcribe import Transcript, TranscriptSegment
from directorloop.screening import runner
from tests.unit.test_screening import Frames, Provider, reply


@pytest.mark.parametrize("prefix", [
    "if you have time for that video do you have time to read this verse with me as well",
    "John 3:16; version 1.10 costs $1,000.50, not 100050.",
    "Don't change this—don't add punctuation, or capitals.",
    "Café déjà vu; cafe\u0301 stays the same. 日本語の字幕。 हिन्दी शब्द।",
    "hello\tworld\nthis   is\r\nverbatim",
])
def test_quote_options_keep_exact_words_case_punctuation_numbers_and_unicode(prefix):
    options = runner._quote_options(prefix)
    normalized_prefix = " ".join(prefix.split())
    assert options
    assert all(option in normalized_prefix for option in options)
    assert all(runner._asr_quote_matches(option, prefix) for option in options)
    if len(normalized_prefix) <= 160:
        assert options == [normalized_prefix]


@pytest.mark.parametrize("prefix", ["", " ", "\n\t\r\n", "!!! …"])
def test_no_transcript_allows_only_null_quote(prefix):
    assert runner._quote_options(prefix) == []
    schema = runner._full_response_schema(prefix)
    assert schema["$defs"]["ScreenObservation"]["properties"]["asr_quote"]["enum"] == [None]


def test_long_transcript_quotes_are_bounded_recent_complete_contiguous_excerpts():
    prefix = " ".join(f"word{index:04}" for index in range(240))
    options = runner._quote_options(prefix)
    assert len(options) == 6
    assert len(options) == len(set(options))
    assert all(0 < len(option) <= 160 for option in options)
    assert all(runner._asr_quote_matches(option, prefix) for option in options)
    assert "word0000" not in " ".join(options)
    assert options[-1].endswith("word0239")


def test_oversized_word_is_omitted_without_splicing_unadjacent_words():
    oversized = "x" * 161
    prefix = f"before {oversized} after"
    assert runner._quote_options(prefix) == ["before", "after"]
    assert all(runner._asr_quote_matches(option, prefix) for option in runner._quote_options(prefix))


def test_request_schemas_are_independent_and_do_not_leak_other_prefixes():
    base = copy.deepcopy(runner.FULL_SCREENING_SCHEMA)
    with ThreadPoolExecutor(max_workers=3) as executor:
        schemas = list(executor.map(runner._full_response_schema, ["earlier", "middle", "later"]))
    assert [schema["$defs"]["ScreenObservation"]["properties"]["asr_quote"]["enum"]
            for schema in schemas] == [[None, "earlier"], [None, "middle"], [None, "later"]]
    schemas[0]["$defs"]["ScreenObservation"]["properties"]["asr_quote"]["enum"].append("mutation")
    assert "mutation" not in json.dumps(schemas[1:])
    assert runner.FULL_SCREENING_SCHEMA == base


def test_observations_precede_the_indices_that_reference_them_in_both_schemas():
    for schema in [runner.SCREENING_SCHEMA, runner.FULL_SCREENING_SCHEMA, runner._full_response_schema("words")]:
        fields = list(schema["properties"])
        assert fields[0] == "observations"
        assert fields.index("observations") < fields.index("cause_observation_indices")
        assert fields.index("review_checks") > fields.index("observations")
        assert fields[-1] == "potential_scores"


def test_full_runner_binds_exact_quote_schema_to_each_past_only_window(tmp_path, monkeypatch):
    source, data_dir = tmp_path / "source.mp4", tmp_path / "data"
    source.write_bytes(b"immutable synthetic fixture")
    monkeypatch.setattr(runner, "inspect_media", lambda _: SimpleNamespace(
        duration_ms=7000, fps=30, width=448, height=796, has_audio=False))
    monkeypatch.setattr(runner, "FrameCache", Frames)
    transcript = Transcript(text="earlier midpoint FUTURE_ONLY", segments=[
        TranscriptSegment(0, 1000, "earlier"),
        TranscriptSegment(2200, 3000, "midpoint"),
        TranscriptSegment(5100, 6100, "FUTURE_ONLY"),
    ])
    monkeypatch.setattr(runner, "_transcript", lambda *_: (transcript, "cached", {"synthetic": True}))

    class ContractProvider(Provider):
        def judge_json(self, media, instruction, schema):
            persisted = json.loads((data_dir / "screenings" / "screen_contract.json").read_text())
            window = next(item for item in persisted["windows"] if item["end_ms"] == media.duration_ms)
            # The exact schema is durably fingerprinted before dispatch.
            assert window["response_schema_sha256"] == sha256_json(schema)
            assert window["instruction_sha256"] and window["payload_sha256"]
            assert schema == runner._full_response_schema(window["prefix_asr_text"])
            options = schema["$defs"]["ScreenObservation"]["properties"]["asr_quote"]["enum"]
            assert all(option is None or runner._asr_quote_matches(option, window["prefix_asr_text"])
                       for option in options)
            assert all(frame.timestamp_ms < media.duration_ms for frame in media.frames)
            if media.duration_ms < 6100:
                assert "FUTURE_ONLY" not in instruction
                assert "FUTURE_ONLY" not in json.dumps(schema)
            assert "never a question" in instruction
            assert "complete declarative sentence" in instruction
            return super().judge_json(media, instruction, schema)

    provider = ContractProvider(lambda _, media: reply(media.frames[-1].timestamp_ms, review_checks=[{
        "aspect": aspect, "status": "unknown", "reason": "Necessary evidence is unavailable.",
        "observation_indices": [],
    } for aspect in sorted(runner.FULL_REVIEW_ASPECTS)]))
    report = runner.run_screening("synthetic", source, provider, data_dir,
                                  screening_id="screen_contract", coverage="full")
    assert report.status == "complete", report.error
    assert report.model_calls == len(provider.calls) == 3
    assert [window.prefix_asr_text for window in report.windows] == [
        "earlier", "earlier midpoint", "earlier midpoint FUTURE_ONLY",
    ]
    assert len({window.response_schema_sha256 for window in report.windows}) == 3
    assert report.protocol["asr_quote_options_version"] == "exact-recent-chunks-v1"
    assert report.protocol["schema_sha256"] == sha256_json(runner.FULL_SCREENING_SCHEMA)
    assert report.semantic_grounding_verified is False
    assert report.automatic_edit_allowed is False
