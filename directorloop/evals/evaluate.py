"""Evaluate one rendered version under a frozen suite.

Cache key covers artifact hash, suite hash, provider, model, trials, modality, frame
count and prompt version. A cache hit is returned with mode="cached" and is never
presented as a fresh run.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ..config import get_settings
from ..domain.assets import AssetManifest
from ..domain.brief import CreativeBrief
from ..domain.edit_plan import EditPlan
from ..domain.evaluation import EvaluationRun, ProbeModality
from ..domain.ids import new_id, sha256_json, utc_now_iso
from ..domain.truth import EvaluationSuite
from ..media.transcribe import Transcript, transcribe
from ..observability.weave_ops import current_call_ref, set_display_name, traced
from ..providers.base import MediaProbeProvider
from .mechanical import run_constraint_checks, run_mechanical_checks
from .probes import build_probe_media, run_probe_trials
from .scoring import score_answers


def evaluation_cache_key(artifact_hash: str, suite_hash: str, provider: MediaProbeProvider, trials: int, frame_count: int, prompt_version: str, approved_texts: list[str] | None = None) -> str:
    return sha256_json(
        {
            "artifact": artifact_hash,
            "suite": suite_hash,
            "provider": provider.capability.name,
            "model": provider.capability.model,
            "trials": trials,
            "modalities": sorted(provider.capability.modalities),
            "frames": frame_count,
            "prompt_version": prompt_version,
            "approved_texts": sorted(approved_texts or []),
        }
    )


@traced("transcribe_rendered_audio", kind="tool")
def transcribe_cached(artifact_path: Path, artifact_hash: str, cache_dir: Path) -> Transcript:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"transcript_{artifact_hash}.json"
    if cached.exists():
        data = json.loads(cached.read_text(encoding="utf-8"))
        from ..media.transcribe import TranscriptSegment, TranscriptToken

        return Transcript(
            text=data["text"],
            segments=[
                TranscriptSegment(
                    start_ms=s["start_ms"], end_ms=s["end_ms"], text=s["text"],
                    tokens=tuple(TranscriptToken(**t) for t in s.get("tokens", [])),
                )
                for s in data.get("segments", [])
            ],
            source=data.get("source", "whisper.cpp"),
            model=data.get("model", ""),
            has_speech=data.get("has_speech", bool(data["text"])),
            note=data.get("note", ""),
        )
    t = transcribe(artifact_path)
    cached.write_text(json.dumps(t.to_dict()), encoding="utf-8")
    return t


@traced("evaluate_version", kind="agent")
def evaluate_version(
    *,
    version_id: str,
    version_label: str,
    artifact_path: Path,
    artifact_hash: str,
    plan: EditPlan,
    baseline_plan: EditPlan | None,
    manifest: AssetManifest,
    brief: CreativeBrief,
    suite: EvaluationSuite,
    provider: MediaProbeProvider,
    trials: int,
    cache_dir: Path,
    allow_cache: bool = True,
    frame_count: int | None = None,
    approved_texts: list[str] | None = None,
) -> EvaluationRun:
    settings = get_settings()
    frame_count = frame_count or settings.dl_probe_frames
    prompt_version = settings.dl_probe_prompt_version
    set_display_name(f"evaluate_{version_label}")
    suite_hash = suite.content_hash()
    key = evaluation_cache_key(artifact_hash, suite_hash, provider, trials, frame_count, prompt_version, approved_texts)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"eval_{key}.json"
    if allow_cache and cache_file.exists():
        run = EvaluationRun.model_validate(json.loads(cache_file.read_text(encoding="utf-8")))
        run.mode = "cached"
        run.id = new_id("eval")
        run.version_id = version_id
        call_id, url = current_call_ref()
        run.weave_call_id, run.weave_url = call_id, url
        run.notes = [n for n in run.notes if not n.startswith("cache")] + ["cache hit: same artifact hash, suite, provider, model, trials and prompt version"]
        return run

    started = utc_now_iso()
    t0 = time.monotonic()
    duration_ms = plan.timeline_duration_ms()
    mechanical = run_mechanical_checks(artifact_path, plan, manifest, brief)
    constraints = run_constraint_checks(plan, baseline_plan, brief, manifest, approved_texts=approved_texts)

    transcript: Transcript | None = None
    notes: list[str] = []
    needs_transcript = "video" not in provider.capability.modalities
    if needs_transcript:
        try:
            transcript = transcribe_cached(artifact_path, artifact_hash, cache_dir)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"transcription failed: {str(exc)[:160]}")
            transcript = Transcript(text="", has_speech=False, note="transcription failed")

    media, modality, frames = build_probe_media(artifact_path, duration_ms, provider, transcript, frame_count=frame_count)
    if modality == ProbeModality.TRANSCRIPT_ONLY:
        notes.append("transcript-only evaluation: the model did not see any pictures")
    t_probe = time.monotonic()
    answers = run_probe_trials(provider, media, suite, trials=trials)
    probe_ms = int((time.monotonic() - t_probe) * 1000)
    results, summaries, totals = score_answers(answers, suite)
    if totals.trials_errored:
        notes.append(f"{totals.trials_errored} probe trial answers were missing or invalid and were excluded from denominators")

    call_id, url = current_call_ref()
    run = EvaluationRun(
        id=new_id("eval"),
        version_id=version_id,
        artifact_hash=artifact_hash,
        suite_id=suite.id,
        suite_hash=suite_hash,
        mode="fresh",
        probe_modality=modality,
        provider=provider.capability.name,
        model=provider.capability.model,
        trials=trials,
        frames_sampled=len(frames) if frames else None,
        frame_timestamps_ms=[f.timestamp_ms for f in frames],
        transcript_source=(transcript.source if transcript and transcript.text else None),
        transcript_text=(transcript.text if transcript else None),
        results=results,
        question_summaries=summaries,
        mechanical=mechanical,
        constraints=constraints,
        questions_passed=totals.questions_passed,
        questions_total=totals.questions_total,
        trials_correct=totals.trials_correct,
        trials_valid=totals.trials_valid,
        trials_errored=totals.trials_errored,
        score=totals.score,
        mechanical_passed=all(m.passed for m in mechanical if m.severity == "critical"),
        constraints_passed=all(c.passed for c in constraints),
        started_at=started,
        ended_at=utc_now_iso(),
        latency_ms=int((time.monotonic() - t0) * 1000),
        probe_latency_ms=probe_ms,
        weave_call_id=call_id,
        weave_url=url,
        cache_key=key,
        notes=notes,
    )
    cache_file.write_text(json.dumps(run.model_dump(mode="json")), encoding="utf-8")
    return run
