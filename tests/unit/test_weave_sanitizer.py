"""Trace inputs must be JSON-safe primitives with secrets and answers removed."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from directorloop.domain.creative import EvidenceClass, EvidenceItem
from directorloop.observability.weave_ops import redact_inputs, redact_output


@dataclass
class Wrapper:
    items: list[EvidenceItem]
    path: Path


def _walk(obj: object) -> None:
    assert obj is None or isinstance(obj, bool | int | float | str | list | dict), type(obj)
    if isinstance(obj, list):
        for v in obj:
            _walk(v)
    if isinstance(obj, dict):
        for v in obj.values():
            _walk(v)


def test_nested_models_become_plain_data_and_secrets_are_removed() -> None:
    ev = EvidenceItem(kind=EvidenceClass.REFERENCE, text="t", ref="pattern_1")
    out = redact_inputs({"hypotheses": [[ev]], "wrapped": Wrapper(items=[ev], path=Path("/tmp/x")), "api_key": "sk-secret", "provider": object(),
                         "nested": {"correct_option_id": "o2", "authorization": "Bearer abc"}})
    _walk(out)
    assert "provider" not in out
    assert out["hypotheses"][0][0]["ref"] == "pattern_1"
    assert out["wrapped"]["path"] == "/tmp/x"
    assert out["api_key"] == "[redacted]" and out["nested"]["correct_option_id"] == "[redacted]" and out["nested"]["authorization"] == "[redacted]"
    json.dumps(out)
    _walk(redact_output({"x": [ev], "y": (1, 2)}))
