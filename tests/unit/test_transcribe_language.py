"""Keep original-language speech and record the ASR setting in evaluator identity."""
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from directorloop.audit.models import EvaluatorRecord
from directorloop.runtime.causal import evaluator_differences

T = importlib.import_module("directorloop.media.transcribe")


@pytest.mark.parametrize("language,detected", [("auto", "pl"), ("en", "en")])
def test_transcriber_passes_language_without_translation_and_preserves_receipt(monkeypatch, language, detected):
    calls = []
    monkeypatch.setattr(T, "whisper_available", lambda: True)
    monkeypatch.setattr(T, "extract_wav16k", lambda *_: None)

    def run(cmd, **kwargs):
        calls.append(cmd)
        Path(cmd[cmd.index("-of") + 1] + ".json").write_text(json.dumps({
            "result": {"language": detected},
            "transcription": [{"text": "Cześć", "offsets": {"from": 0, "to": 400}}],
        }))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(T.subprocess, "run", run)
    result = T.transcribe("source.mp4", language=language)
    assert calls[0][calls[0].index("-l") + 1] == language
    assert "--translate" not in calls[0] and "-tr" not in calls[0]
    assert result.text == "Cześć"
    assert result.to_dict()["requested_language"] == language
    assert result.to_dict()["detected_language"] == detected


def test_configured_language_used_and_missing_asr_stays_unknown(monkeypatch):
    monkeypatch.setattr(T, "get_settings", lambda: SimpleNamespace(dl_asr_language="auto"))
    monkeypatch.setattr(T, "whisper_available", lambda: False)
    result = T.transcribe("source.mp4")
    assert result.requested_language == "auto" and result.detected_language is None
    assert not result.has_speech and "missing" in result.note


def test_asr_language_change_invalidates_frozen_comparison():
    original = EvaluatorRecord(cold_viewer_prompt_version="cold-viewer-v2", coarse_window_ms=2000,
                               precision_enabled=True, precision_window_ms=500, precision_lead_ms=500,
                               max_precision_regions=2, max_precision_windows=10)
    assert original.asr_language == "en"  # historical records used the fixed English command
    candidate = original.model_copy(update={"asr_language": "auto"})
    assert evaluator_differences(original, candidate) == ["asr_language: en for the original, auto for the variant"]
