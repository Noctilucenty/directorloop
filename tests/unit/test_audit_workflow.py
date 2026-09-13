"""Offline checks that semantic audit tracing preserves cold review inputs and honest outcomes."""

from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
import weave

from directorloop.audit import review as R
from directorloop.audit.models import AuditReport
from directorloop.creative.signals import VideoSignals
from directorloop.media.frames import SampledFrame
from directorloop.media.transcribe import Transcript, TranscriptSegment
from directorloop.observability.workflow import workflow_session, workflow_stage
from directorloop.providers.base import CompletionResult, ProbeMedia, ProviderCapability, ProviderError


class ScriptedAuditProvider:
    capability = ProviderCapability(name="offline", role="probe", model="scripted")

    def __init__(self, *, fail_window: bool = False, fail_closeup: bool = False, findings: bool = True) -> None:
        self.fail_window = fail_window
        self.fail_closeup = fail_closeup
        self.findings = findings
        self.calls: list[tuple[ProbeMedia, str, dict[str, Any]]] = []
        self._lock = threading.Lock()

    def judge_json(self, media: ProbeMedia, instruction: str, schema: dict[str, Any]) -> CompletionResult:
        with self._lock:
            self.calls.append((media, instruction, schema))
        if schema is R.WINDOW_SCHEMA:
            if self.fail_window and media.duration_ms == 4000:
                raise ProviderError("scripted window unavailable")
            data: dict[str, Any] = {
                "understanding": "A bridge is being measured", "expectation": "A result", "open_question": "",
                "reaction": "neutral", "attention_risk": "medium", "cause": "Waiting for the result",
            }
        elif schema is R.DIAG_SCHEMA:
            data = {
                "findings": [{
                    "start_s": 2.0, "end_s": 4.0, "weakness": "Repeated view", "severity": "medium",
                    "uncertainty": "low", "evidence_times_s": [2.5], "observed": [{"kind": "visual", "text": "Bridge remains visible", "t_s": 2.5}],
                }] if self.findings else [],
                "strengths": [], "audience": {}, "overall_summary": "The result arrives after a repeated view.",
            }
        else:
            assert schema is R.CLOSEUP_SCHEMA
            if self.fail_closeup:
                raise ProviderError("scripted close-up unavailable")
            data = {"happens": True, "start_s": 2.0, "end_s": 4.0, "confirmed_observations": ["Bridge remains visible"], "corrected_observations": [], "note": "Confirmed"}
        return CompletionResult(data=copy.deepcopy(data), latency_ms=7, model="scripted")


def install_media(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, audio: bool = True) -> Path:
    video = tmp_path / "source.mp4"
    video.write_bytes(b"offline-video-fixture")
    monkeypatch.setattr(R, "inspect_media", lambda path: SimpleNamespace(duration_ms=6000, has_audio=audio, width=360, height=640))
    monkeypatch.setattr(R, "extract_signals", lambda *args: VideoSignals(6000, 360, 640, [2000], [], np.ones(600, dtype=np.float32)))
    monkeypatch.setattr(R, "transcribe", lambda path: Transcript(
        text="opening middle ending", has_speech=True, source="fixture", model="offline",
        segments=[TranscriptSegment(0, 1000, "opening"), TranscriptSegment(2000, 3000, "middle"), TranscriptSegment(4000, 5000, "ending")],
    ))
    monkeypatch.setattr(R, "extract_frame", lambda path, t, max_width: SampledFrame(t, b"fixture-jpeg", max_width, max_width * 2))
    return video


def audit(tmp_path: Path, video: Path, provider: ScriptedAuditProvider, **kwargs: Any) -> AuditReport:
    return R.run_audit(
        video_id="SECRET_VIDEO_LABEL", video_path=video, version_id="SECRET_VERSION_LABEL",
        provider=provider, data_dir=tmp_path / "data", **kwargs,
    )


def test_audit_hierarchy_preserves_prefix_coverage_and_persists_graph(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    video = install_media(monkeypatch, tmp_path)
    provider = ScriptedAuditProvider()
    report = audit(tmp_path, video, provider)

    assert report.status == "complete"
    assert report.model_calls == 5 == len(provider.calls)
    assert report.primed_with == []
    assert [(r.start_ms, r.end_ms) for r in report.window_reactions] == [(0, 2000), (2000, 4000), (4000, 6000)]
    assert report.coverage.covered_ms() == 6000
    assert report.findings[0].verified_closeup is True
    assert report.findings[0].weakness == "Repeated view"
    assert all("SECRET_VIDEO_LABEL" not in instruction and "SECRET_VERSION_LABEL" not in instruction for _, instruction, _ in provider.calls)
    prefix_calls = sorted((c for c in provider.calls if c[2] is R.WINDOW_SCHEMA), key=lambda c: c[0].duration_ms)
    for media, instruction, _ in prefix_calls:
        assert media.transcript is None
        assert media.kind == "frames"
        assert all(frame.timestamp_ms < media.duration_ms for frame in media.frames)
        assert "opening" in instruction
        assert ("middle" in instruction) is (media.duration_ms > 2000)
        assert ("ending" in instruction) is (media.duration_ms > 4000)
    assert all(c[2] is R.WINDOW_SCHEMA for c in provider.calls[:3])
    assert provider.calls[3][2] is R.DIAG_SCHEMA
    assert provider.calls[4][2] is R.CLOSEUP_SCHEMA

    stages = report.workflow_stages
    assert stages
    assert {s.key for s in stages} >= {"ingest", "inspect_media", "measure_signals", "transcribe_audio", "cold_judge", "review_window", "diagnose", "closeup_checks"}
    windows = [s for s in stages if s.key == "review_window"]
    assert len(windows) == 3
    cold = next(s for s in stages if s.key == "cold_judge")
    assert all(s.parent_id == cold.id for s in windows)
    assert all(s.status == "completed" for s in stages)
    persisted = json.loads((tmp_path / "data" / "audit" / report.id / "audit.json").read_text())
    assert persisted["workflow_stages"] == [s.model_dump(mode="json") for s in stages]


def test_failed_prefix_is_incomplete_and_closeup_failure_is_not_hidden(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    video = install_media(monkeypatch, tmp_path)
    report = audit(tmp_path, video, ScriptedAuditProvider(fail_window=True, fail_closeup=True))

    assert report.status == "incomplete"
    assert report.window_reactions[1].error == "ProviderError: operation failed; inspect saved job stages"
    assert report.findings[0].verified_closeup is False
    assert report.findings[0].closeup_note == "closer inspection failed (provider error)"
    assert report.model_calls == 5
    assert next(s for s in report.workflow_stages if s.key == "cold_judge").status == "incomplete"
    assert next(s for s in report.workflow_stages if s.key == "closeup_checks").status == "incomplete"
    assert len([s for s in report.workflow_stages if s.key == "review_window" and s.status == "incomplete"]) == 1


def test_no_audio_or_findings_does_not_invent_execution_stages(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    video = install_media(monkeypatch, tmp_path, audio=False)

    def no_transcription(path: Path) -> Transcript:
        pytest.fail("A silent file must not be transcribed")

    monkeypatch.setattr(R, "transcribe", no_transcription)
    report = audit(tmp_path, video, ScriptedAuditProvider(findings=False))

    assert report.model_calls == 4
    assert report.findings == []
    assert "no audio stream: speech and sound could not be reviewed" in report.incomplete_reasons
    assert {s.key for s in report.workflow_stages}.isdisjoint({"transcribe_audio", "closeup_checks"})
    assert all(not r.error for r in report.window_reactions)


def test_old_audit_records_load_without_workflow_fields(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    video = install_media(monkeypatch, tmp_path)
    report = audit(tmp_path, video, ScriptedAuditProvider(findings=False))
    old = report.model_dump()
    old.pop("workflow_stages")
    assert AuditReport.model_validate(old).workflow_stages == []


def test_parallel_nested_audits_keep_their_own_stage_evidence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    video = install_media(monkeypatch, tmp_path)

    @workflow_session
    def pair() -> SimpleNamespace:
        with workflow_stage("audit_pair", "Audit both source edits", kind="agent"):
            with weave.ThreadPoolExecutor(max_workers=2) as executor:
                reports = list(executor.map(
                    lambda label: R.run_audit(video_id=label, video_path=video, version_id=label,
                                             provider=ScriptedAuditProvider(), data_dir=tmp_path / "data"),
                    ["source-a", "source-b"],
                ))
        return SimpleNamespace(audits=reports, workflow_stages=[])

    pair_result = pair()
    a, b = pair_result.audits
    a_ids = {s.id for s in a.workflow_stages}
    b_ids = {s.id for s in b.workflow_stages}
    assert a_ids.isdisjoint(b_ids)
    for report in (a, b):
        assert len([s for s in report.workflow_stages if s.key == "review_window"]) == 3
        assert [s.inputs["video_id"] for s in report.workflow_stages if s.key == "ingest"] == [report.video_id]
    assert a_ids | b_ids < {s.id for s in pair_result.workflow_stages}
