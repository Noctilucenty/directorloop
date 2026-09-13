"""Bounded chronological video screening, with durable partial evidence."""

from __future__ import annotations

import concurrent.futures as cf
import copy
import fcntl
import json
import math
import os
import re
import tempfile
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Lock
from typing import Any

import numpy as np
import weave
from pydantic import ValidationError

from ..audit.models import InspectedWindow
from ..audit.review import (
    WINDOW_INSTRUCTION,
    FrameCache,
    ViewerPayload,
    _audio_lines,
    build_viewer_payload,
    viewer_frame_times,
    words_from_transcript,
    words_heard_by,
)
from ..config import get_settings
from ..creative.signals import VideoSignals, audio_rms
from ..domain.ids import new_id, sha256_bytes, sha256_file, sha256_json, utc_now_iso
from ..evals.evaluate import transcribe_cached
from ..jobs.worker import JobCanceled, JobInterrupted, safe_failure
from ..media.probe import inspect_media
from ..media.transcribe import DEFAULT_MODEL, Transcript, TranscriptSegment, TranscriptToken
from ..observability.weave_ops import current_call_ref, redact_output, traced
from ..observability.workflow import attach_workflow, snapshot_workflow, workflow_session, workflow_stage
from ..providers.base import VIEWER_SYSTEM_PROMPT, ProbeMedia
from .boundary import BOUNDARY_VALIDATION_PROTOCOL, boundary_validation_issues
from .delivery import measure_delivery
from .mechanical import MECHANICAL_PROTOCOL, measure_frame_changes
from .models import ScreenFrame, ScreenJudgment, ScreenReport, ScreenWindow
from .scoring import SCORE_PROTOCOL, SCORE_RUBRIC, build_scorecard

SCREENING_VERSION = "grounded-screening-v3"
EVIDENCE_VALIDATION_PROTOCOL = {
    "version": "aria-informed-citation-guards-v1",
    "asr_quote": "NFC and whitespace normalization; case and punctuation preserved; whole Unicode words and numeric forms",
    "empty_or_punctuation_only_quote": "rejected",
    "frame_citation": "strict nonnegative integer and supplied-frame membership",
    "semantic_grounding_verified": False,
}
MAX_SCREEN_DURATION_MS = 180000
MAX_FULL_SCREEN_CALLS = 8
MAX_FULL_SCREEN_WORKERS = 3
SCREENING_SCHEMA = ScreenJudgment.model_json_schema()
FULL_SCREENING_SCHEMA = ScreenJudgment.model_json_schema()
FULL_SCREENING_SCHEMA["required"] = [*FULL_SCREENING_SCHEMA["required"], "review_checks", "potential_scores"]
FULL_SCREENING_SCHEMA["properties"]["review_checks"].update(minItems=8, maxItems=8)
FULL_SCREENING_SCHEMA["properties"]["review_checks"].pop("default", None)
FULL_SCREENING_SCHEMA["properties"]["potential_scores"].update(minItems=3, maxItems=3)
FULL_SCREENING_SCHEMA["properties"]["potential_scores"].pop("default", None)
SCREENING_INSTRUCTION = """
This is a screening suggestion, not a final evaluation or an edit decision.
Return exactly the screening JSON schema. The schema replaces the earlier list of response fields.
Keep understanding under 240 characters and suggestion under 200. Use 4–6 concise observations when needed to support the checks and scores, at most 12, each under 240 characters; at most 2 uncertainties under 160 characters each. Separate concrete observations from interpretation:
- visible_fact: describe only pixels actually visible, and cite one or more EXACT supplied frame timestamps in integer milliseconds.
- caption_claim: report what on-screen text claims, with frame timestamps; the text is not visual proof that its claim is true.
- asr_claim: report what the supplied transcript claims, with a verbatim quote from the supplied words; do not claim you listened to audio.
- inference: explicitly qualify interpretation as uncertain, separate from observed facts.
- unknown: use this when an object, action, result or property is concealed or cannot be established from the supplied frames.
For every observation include frame_timestamps_ms (empty when no image citation applies) and asr_quote (null when no transcript quote applies).
Each observation must contain one independently checkable claim. Separate observed appearance, caption wording, transcript wording and interpretation into different observations; omit extra claims to stay within the limit. Do not bundle an observed action with a guessed cause or hidden result.
visible_fact and caption_claim use frame citations and asr_quote=null. asr_claim uses a verbatim quote and an empty frame list. Inference and unknown labels do not turn speculation into factual evidence.
Do not judge the appearance or quality of a hidden object from a caption. Opaque containers, covers and occlusion hide contents; descriptions of hidden size, quality or identity must remain unknown.
Never fill missing words or facts using later moments, general knowledge, a title or a presumed storyline.
cause_observation_indices must cite zero-based entries in your observations list. No citations means attention_risk must be unknown.
An attention label needs at least one supplied frame or transcript anchor among its cited non-unknown observations. Never use unknown observations alone to support a specific risk level.
Attention risk is only a tentative low/medium/high/unknown screening label, never a probability, view forecast or measured audience result.
moment_kind describes the current moment: setup, development, reveal, endcard, signoff or unknown.
An endcard or signoff can naturally finish a video; do not recommend removing it merely because the story has ended.
suggestion is one short declarative finding about this interval, never a question or an instruction to execute an edit. State the observed change and a tentative consequence only when supported. If no issue is supported, state a specific strength or missing evidence. Do not ask the reader to perform the analysis. Use a complete sentence; never cut it off to fit a length limit.
Only these frame timestamp citations are valid: {frame_times}
"""


FULL_REVIEW_ASPECTS = {"pacing", "visual_clarity", "caption_readability", "caption_alignment", "hook_and_payoff", "tone_from_words", "voice_delivery", "share_motivation"}
FULL_REVIEW_RUBRIC = """
Write observations FIRST. Then refer to their final zero-based indices; never guess indices before the observations exist. Review this interval across ALL eight aspects in review_checks. Keep each reason to one complete declarative sentence (maximum 160 characters), with observation_indices pointing to supporting observations. In understanding, name one concrete contribution or problem in THIS interval in at most 18 words. Avoid generic genre summaries such as "a clear, fast-paced ad". Keep suggestion to one complete declarative finding of at most 18 words tied to this interval. For example, state an observed topic change and its possible effect; do not ask "Does the transition feel abrupt?". Do not invent a repair when no issue is supported. Clear means no specific issue was found in the supplied evidence; it is not proof of quality. Unknown means the necessary modality, sample density, or context is absent.
- pacing: name what new information or action appears in THIS interval compared with the earlier prefix. If a supported concern is repetition or waiting, cite the repeated content and the supplied timestamps spanning the wait; do not invent a wait duration when samples cannot establish it. Judge whether the wait adds explanation, reading time or anticipation. A short hold can help comprehension. Separate speech word rate from visual progression; cuts or motion alone do not imply retention. No universal pacing threshold.
- visual_clarity: can the main subject/action and intended reveal actually be identified in the sampled pixels? Occluded details stay unknown. Captions naming an object are not proof it was visible. Look for competing visual demands rather than declaring all complexity bad.
- caption_readability: assess only text that can actually be read at the supplied frame resolution. Mention crowding, cropping or contrast only with a cited visible example. Actual font size, every caption's reading time and safe-area placement are unknown without measurements.
- caption_alignment: compare readable on-screen words with the same interval's supplied transcript. Wording consistency can be checked; precise subtitle synchronization remains unknown from sparse frames and ASR timing alone. Silence is not a missing-caption failure.
- hook_and_payoff: identify the observed question/promise, what this interval adds toward it, and whether it is answered by this cutoff. Consider accumulated waiting and competing unresolved promises only with specific earlier evidence. During an unfinished prefix, a later payoff is unknown, not missing. A natural endcard/signoff is not automatically a problem or a repair target.
- tone_from_words: cautiously describe linguistic style in the actual words, not vocal emotion. Separate caption wording, transcript wording and speculation.
- voice_delivery: always unknown here. No native audio is supplied to the vision model; RMS and transcript cannot establish prosody, warmth, sarcasm, confidence, vocal energy or music quality.
- share_motivation: identify a specific possible reason someone might share (usefulness, surprise, relatable situation) only as a hypothesis anchored in observed content. Do not infer audience demand, recommend sensationalism or promise virality. Views and actual retention are unmeasured.
Duration gives more opportunities to leave, but does not establish a rising per-interval risk. Judge current evidence and accumulated unresolved effort, not length alone. A flat sequence can be valid. Never force varied ratings, a falling retention curve, a late-video penalty or a numeric probability. Per-interval risk is not cumulative audience survival.
For clear/concern, cite valid supporting observation indices. A visible_fact alone cannot establish caption readability, transcript alignment or linguistic tone. Caption readability requires a caption_claim; caption alignment requires BOTH a readable caption_claim and a separately quoted asr_claim from this interval; tone_from_words requires caption_claim or asr_claim wording. When these observations are absent, use unknown with empty indices, for example: caption_alignment/unknown/"No paired caption and transcript evidence." Do not invent captions, quotes or an issue to fill the checklist. Four observations may not support all eight aspects: unknown is the correct result for unsupported aspects, not clear. Return all eight aspects exactly once; voice_delivery must be unknown. No audience percentages or viral-outcome prediction. The separate 0–4 potential rubric is a subjective content assessment.
"""


def _quote_options(prefix: str) -> list[str]:
    """Recent exact contiguous ASR excerpts; no punctuation or word repair."""
    chunks: list[str] = []
    current = ""
    for word in prefix.split():
        if len(word) > 160:
            if current:
                chunks.append(current)
                current = ""
            continue
        if current and len(current) + len(word) + 1 > 160:
            chunks.append(current)
            current = word
        else:
            current = f"{current} {word}" if current else word
    if current:
        chunks.append(current)
    return [chunk for chunk in dict.fromkeys(chunks[-6:]) if _asr_quote_matches(chunk, prefix)]


def _full_response_schema(prefix: str) -> dict[str, Any]:
    schema = copy.deepcopy(FULL_SCREENING_SCHEMA)
    schema["$defs"]["ScreenObservation"]["properties"]["asr_quote"]["enum"] = [None, *_quote_options(prefix)]
    return schema


def screening_windows(duration_ms: int, coverage: str = "quick") -> list[tuple[int, int]]:
    if not 0 < duration_ms <= MAX_SCREEN_DURATION_MS:
        raise ValueError("Screening requires a video between 1 ms and 180 seconds")
    if coverage == "quick":
        return [(max(0, end - 2000), end) for end in sorted({min(2000, duration_ms), min(4000, duration_ms), duration_ms})]
    if coverage != "full":
        raise ValueError("Unknown screening coverage")
    # Keep detailed opening boundaries; cover every later interval without gaps.
    boundaries = sorted({0, min(2000, duration_ms), min(4000, duration_ms)})
    remaining = duration_ms - boundaries[-1]
    if remaining:
        count = min(MAX_FULL_SCREEN_CALLS - (len(boundaries) - 1), math.ceil(remaining / 4000))
        start = boundaries[-1]
        boundaries.extend(start + math.ceil(remaining * i / count) for i in range(1, count + 1))
    return list(zip(boundaries, boundaries[1:], strict=False))


def _full_payload(frames, signals, words, start_ms, end_ms, *, fps):
    """Sample every timeline section, with bounded images and no future evidence."""
    count = min(12, max(1, math.ceil((end_ms - start_ms) * 3 / 1000)))
    context, current = viewer_frame_times(start_ms, end_ms, fps=fps, precision_frames=count)
    if len(context) > 12:
        context = [context[round(i * (len(context) - 1) / 11)] for i in range(12)]
    sent = frames.many(context, 256) + frames.many(current, 448)
    heard = words_heard_by(words, end_ms)
    if any(f.timestamp_ms >= end_ms for f in sent) or any(w.end_ms > end_ms for w in heard):
        raise ValueError("Information boundary violated in full timeline review")
    word_lines = [f"{'>>' if w.start_ms >= start_ms else '  '} {w.start_ms / 1000:.1f}s {w.text}" for w in heard]
    instruction = WINDOW_INSTRUCTION.replace("(about one per second)", "(at most 12 earlier snapshots)").format(
        start=start_ms / 1000, end=end_ms / 1000, words="\n".join(word_lines) or "(no words yet)",
        audio="; ".join(_audio_lines(signals, words, start_ms, end_ms)))
    instruction += "\nThese are sampled frames across this entire interval, not continuous playback. Say when sampling cannot establish an action."
    window = InspectedWindow(kind="prefix", start_ms=start_ms, end_ms=end_ms,
        frame_timestamps_ms=current, frame_width=448, context_frames=len(context), context_width=256,
        context_frame_timestamps_ms=context, transcript_words=len(heard),
        latest_frame_ms=max((f.timestamp_ms for f in sent), default=None),
        latest_word_end_ms=max((w.end_ms for w in heard), default=None), boundary_ms=end_ms)
    return ViewerPayload(ProbeMedia(kind="frames", duration_ms=end_ms, frames=sent), instruction, window)


def _record_path(data_dir: Path, screening_id: str) -> Path:
    if not re.fullmatch(r"screen_[A-Za-z0-9_-]{1,100}", screening_id):
        raise ValueError("Invalid screening ID")
    return Path(data_dir) / "screenings" / f"{screening_id}.json"


def _persist(report: ScreenReport, data_dir: Path) -> None:
    attach_workflow(report)
    report.scorecard = build_scorecard(report)
    # Parallel stages own their journal entries; persist a detached snapshot.
    report.workflow_stages = snapshot_workflow() or report.workflow_stages
    target = _record_path(data_dir, report.id)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Redact each evidence attempt independently: trace depth limits must never
    # truncate durable observations merely because a correction adds nesting.
    body = redact_output(report.model_dump(mode="json", exclude={"windows"}))
    body["windows"] = []
    for window in report.windows:
        item = redact_output(window.model_dump(mode="json", exclude={"review_attempts"}))
        item["review_attempts"] = [
            {"phase": a["phase"], "accepted": a["accepted"], "window": redact_output(a["window"])}
            for a in window.review_attempts
        ]
        body["windows"].append(item)
    data = json.dumps(body, ensure_ascii=False, allow_nan=False, indent=2).encode()
    fd, temporary = tempfile.mkstemp(prefix=f".{report.id}-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load_screening(data_dir: Path, screening_id: str) -> ScreenReport:
    return ScreenReport.model_validate_json(_record_path(data_dir, screening_id).read_text())


def save_screening(data_dir: Path, report: ScreenReport) -> None:
    """Durably reconcile an interrupted record; callers must not change prior evidence."""
    _persist(report, data_dir)


def list_screenings(data_dir: Path) -> list[ScreenReport]:
    reports = []
    for path in (Path(data_dir) / "screenings").glob("screen_*.json"):
        try:
            reports.append(ScreenReport.model_validate_json(path.read_text()))
        except (ValueError, OSError):
            continue
    return sorted(reports, key=lambda report: report.created_at, reverse=True)


def _transcript(path: Path, artifact_hash: str, data_dir: Path, has_audio: bool) -> tuple[Transcript, str, dict[str, Any]]:
    if not has_audio:
        return Transcript(text="", has_speech=False, note="no audio stream"), "no_audio", {"has_audio": False}
    asr_protocol = {"language": get_settings().dl_asr_language,
                    "model_sha256": sha256_file(DEFAULT_MODEL) if DEFAULT_MODEL.is_file() else None,
                    "transcribe_code_sha256": sha256_file(Path(__file__).parents[1] / "media/transcribe.py")}
    cache = data_dir / "cache" / "screening_asr" / sha256_json(asr_protocol)
    cache.mkdir(parents=True, exist_ok=True)
    cache_file = cache / f"transcript_{artifact_hash}.json"
    with (cache / f".{artifact_hash}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        existed = cache_file.is_file()
        transcript = transcribe_cached(path, artifact_hash, cache)
        # The legacy cache loader drops language metadata. Read those persisted fields back without rerunning ASR.
        if cache_file.exists():
            raw = json.loads(cache_file.read_text())
            raw["segments"] = [TranscriptSegment(**{**s, "tokens": tuple(TranscriptToken(**t) for t in s.get("tokens", []))}) for s in raw.get("segments", [])]
            transcript = Transcript(**raw)
        mode = "cached" if existed else "fresh"
        if transcript.note and not transcript.has_speech:
            mode = "unavailable"
    return transcript, mode, asr_protocol


def _asr_quote_matches(quote: str, prefix_asr: str) -> bool:
    """Check verbatim membership without matching fragments of words or contractions.

    Keep punctuation and case: removing them could change a number or a claim.
    Unicode marks remain part of words, including scripts without ASCII letters.
    This conservative check does not infer word segmentation or semantic truth.
    """
    quote = " ".join(unicodedata.normalize("NFC", quote).split())
    transcript = " ".join(unicodedata.normalize("NFC", prefix_asr).split())
    if not any(char.isalnum() for char in quote):
        return False

    def word_character(index: int) -> bool:
        if not 0 <= index < len(transcript):
            return False
        category = unicodedata.category(transcript[index])
        return category[0] in "LMN" or category == "Pc"

    def word_part(index: int) -> bool:
        if not 0 <= index < len(transcript):
            return False
        char = transcript[index]
        right_digit = index + 1 < len(transcript) and transcript[index + 1].isdecimal()
        left_digit = index > 0 and transcript[index - 1].isdecimal()
        return (word_character(index)
                or (char in "'’" and word_character(index - 1) and word_character(index + 1))
                or (char in ".,/:–" and left_digit and right_digit)
                or (char in "+-−" and right_digit))

    for match in re.finditer(re.escape(quote), transcript):
        start, end = match.span()
        if not (word_part(start - 1) and word_part(start)) and not (word_part(end - 1) and word_part(end)):
            return True
    return False


def validate_evidence(judgment: ScreenJudgment, frame_times: list[int], prefix_asr: str) -> list[str]:
    """Check anchors exist. This intentionally makes no semantic-grounding claim."""
    issues = []
    allowed = {t for t in frame_times if type(t) is int and t >= 0}
    quote_matches = [_asr_quote_matches(o.asr_quote, prefix_asr) if o.asr_quote is not None else False
                     for o in judgment.observations]
    for index, observation in enumerate(judgment.observations):
        if not observation.text.strip():
            issues.append(f"Observation {index + 1}: claim text is empty")
        if len(observation.frame_timestamps_ms) != len(set(observation.frame_timestamps_ms)):
            issues.append(f"Observation {index + 1}: frame citations are duplicated")
        if any(type(t) is not int or t < 0 or t not in allowed for t in observation.frame_timestamps_ms):
            issues.append(f"Observation {index + 1}: frame citation was not supplied")
        if observation.kind in {"visible_fact", "caption_claim"} and not observation.frame_timestamps_ms:
            issues.append(f"Observation {index + 1}: image evidence requires a frame citation")
        if observation.kind == "asr_claim" and not observation.asr_quote:
            issues.append(f"Observation {index + 1}: transcript claim requires a verbatim quote")
        if observation.kind in {"visible_fact", "caption_claim"} and observation.asr_quote is not None:
            issues.append(f"Observation {index + 1}: split image and transcript claims into separate observations")
        if observation.kind == "asr_claim" and observation.frame_timestamps_ms:
            issues.append(f"Observation {index + 1}: transcript claims must not use image citations as proof")
        if observation.asr_quote is not None:
            if not quote_matches[index]:
                issues.append(f"Observation {index + 1}: quote is absent from the supplied ASR prefix")
    if any(i < 0 or i >= len(judgment.observations) for i in judgment.cause_observation_indices):
        issues.append("Attention explanation references a missing observation")
    if judgment.attention_risk != "unknown" and not judgment.cause_observation_indices:
        issues.append("Attention label has no supporting observation")
    if len(judgment.cause_observation_indices) != len(set(judgment.cause_observation_indices)):
        issues.append("Attention explanation repeats an observation reference")
    causes = [i for i in judgment.cause_observation_indices if 0 <= i < len(judgment.observations)]
    anchored = [i for i in causes if judgment.observations[i].kind != "unknown" and (
        any(type(t) is int and t >= 0 and t in allowed for t in judgment.observations[i].frame_timestamps_ms)
        or quote_matches[i])]
    if judgment.attention_risk != "unknown" and causes and not anchored:
        issues.append("Attention label has no anchored non-unknown observation; use unknown")
    seen = set()
    for check in judgment.review_checks:
        if check.aspect in seen:
            issues.append(f"Review check {check.aspect}: repeated aspect")
        seen.add(check.aspect)
        indices = check.observation_indices
        valid = [i for i in indices if 0 <= i < len(judgment.observations)]
        if len(valid) != len(indices) or len(indices) != len(set(indices)):
            issues.append(f"Review check {check.aspect}: invalid observation reference")
        if check.status != "unknown" and not valid:
            issues.append(f"Review check {check.aspect}: supporting observation missing")
        if check.status != "unknown" and any(any(issue.startswith(f"Observation {i + 1}:") for issue in issues) for i in valid):
            issues.append(f"Review check {check.aspect}: supporting observation failed evidence validation")
        if check.aspect == "voice_delivery" and check.status != "unknown":
            issues.append("Review check voice_delivery: native audio was not supplied")
        if check.aspect in {"caption_readability", "caption_alignment"} and check.status != "unknown" and not any(judgment.observations[i].kind == "caption_claim" for i in valid):
            issues.append(f"Review check {check.aspect}: readable caption evidence missing")
        if check.aspect == "caption_alignment" and check.status != "unknown" and not any(judgment.observations[i].kind == "asr_claim" and quote_matches[i] for i in valid):
            issues.append("Review check caption_alignment: matching transcript evidence missing")
        if check.aspect == "tone_from_words" and check.status != "unknown" and not any(judgment.observations[i].kind in {"asr_claim", "caption_claim"} for i in valid):
            issues.append("Review check tone_from_words: quoted or readable wording missing")
    return issues


def _save_payload_frames(payload: Any, data_dir: Path, screening_id: str) -> list[ScreenFrame]:
    directory = Path(data_dir) / "screenings" / screening_id / "frames"
    directory.mkdir(parents=True, exist_ok=True)
    records = []
    for frame in payload.media.frames:
        path = directory / f"{frame.timestamp_ms:06d}_{frame.width}.jpg"
        digest = sha256_bytes(frame.jpeg)
        if path.exists():
            if sha256_file(path) != digest:
                raise ValueError("Immutable screening frame changed")
        else:
            with path.open("xb") as handle:
                handle.write(frame.jpeg)
        records.append(ScreenFrame(t_ms=frame.timestamp_ms, path=str(path), sha256=digest, width=frame.width, height=frame.height))
    return records


def _safe_raw(value: Any) -> Any:
    value = redact_output(value)
    if isinstance(value, dict):
        return {key: _safe_raw(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_raw(item) for item in value]
    return str(value) if isinstance(value, float) and not math.isfinite(value) else value


@traced("directorloop.screening_prefix", kind="agent", display=lambda i: f"Video review | prefix {i['window']['end_ms'] / 1000:g}s")
def _screen_prefix(provider: Any, payload: Any, window: ScreenWindow, _checkpoint: Callable[[], None], *, full_review: bool = False) -> ScreenWindow:
    window.weave_call_id, window.weave_url = current_call_ref()
    instruction = payload.instruction + SCREENING_INSTRUCTION.format(frame_times=json.dumps(window.frame_timestamps_ms))
    if full_review:
        instruction += FULL_REVIEW_RUBRIC + SCORE_RUBRIC
        instruction += "\nASR QUOTE OPTIONS: " + json.dumps(_quote_options(window.prefix_asr_text), ensure_ascii=False)
        instruction += "\nFor asr_quote, choose exactly one complete option above, or null when not citing ASR. Do not join options, add punctuation, capitalize, or repair wording. With no options, do not make an asr_claim. Write each observation before assigning the check references."
        instruction += (
            "\nFINAL CHECKPOINT: This cutoff is the end of the source video. Judge only the supplied evidence; a natural ending is not automatically a problem."
            if window.is_last_prefix else
            "\nINTERMEDIATE CHECKPOINT: The source video continues beyond this evidence cutoff. The cutoff is only a review boundary, not the video's ending or an editing problem. Do not say the video ends here or mark a payoff as missing because it has not appeared yet. An unobserved later payoff remains unknown. Any concern must be supported by the content already observed, not by the withheld continuation."
        )
    response_schema = _full_response_schema(window.prefix_asr_text) if full_review else SCREENING_SCHEMA
    window.response_schema_sha256 = sha256_json(response_schema)
    window.instruction_sha256 = sha256_bytes(instruction.encode())
    window.payload_sha256 = sha256_json({"instruction": instruction, "frames": [{"timestamp_ms": f.t_ms, "sha256": f.sha256} for f in window.evidence_frames]})
    _checkpoint()  # input hashes and logical dispatch count survive process loss before a reply
    result = provider.judge_json(payload.media, instruction, response_schema)
    raw = _safe_raw(result.data)
    window.raw_output = raw if isinstance(raw, dict) else {"non_object_response": raw}
    window.input_tokens, window.output_tokens, window.latency_ms = result.input_tokens, result.output_tokens, result.latency_ms
    try:
        window.judgment = ScreenJudgment.model_validate(result.data)
    except ValidationError:
        window.status = "needs_review"
        window.validation_issues = ["Model output did not match the screening schema"]
        return window
    window.validation_issues = validate_evidence(window.judgment, window.frame_timestamps_ms, window.prefix_asr_text)
    if full_review:
        window.validation_issues.extend(boundary_validation_issues(window.judgment.model_dump(), is_last_prefix=window.is_last_prefix))
    window.status = "needs_review" if window.validation_issues else "complete"
    kind = window.judgment.moment_kind
    window.attention_context = f"last_{kind}" if window.is_last_prefix and kind in {"endcard", "signoff"} else "content"
    return window


@dataclass
class _ScreeningStop:
    """First failure closes dispatch while already-started calls drain."""

    lock: Lock = field(default_factory=Lock)
    error: BaseException | None = None

    def stop(self, error: BaseException) -> None:
        with self.lock:
            if self.error is None:
                self.error = error

    def reason(self) -> BaseException | None:
        with self.lock:
            return self.error


@dataclass
class _DispatchCheckpoint:
    index: int
    window: ScreenWindow
    ready: Event = field(default_factory=Event)
    error: BaseException | None = None


def _screen_full_windows(report: ScreenReport, payloads: list[ViewerPayload], provider: Any,
                         checkpoint: Callable[[], None], check_cancel: Callable[[], None],
                         on_stage: Callable[..., Any] | None) -> None:
    """Bounded prefix fan-out; only this coordinator writes the durable report.

    Payloads are prepared serially. Each worker sees its own past-only prefix and
    never another worker's judgment. Before dispatch it waits for its input hashes
    to be persisted by the coordinator. A failed or canceled call closes the gate;
    up to two other already-running provider calls may still finish and are saved.
    """
    requests: Queue[_DispatchCheckpoint] = Queue()
    stop = _ScreeningStop()

    def review(index: int) -> tuple[ScreenWindow, BaseException | None]:
        window = report.windows[index].model_copy(deep=True)
        attempted = False

        def request_checkpoint() -> None:
            nonlocal attempted
            request = _DispatchCheckpoint(index, window.model_copy(deep=True))
            requests.put(request)
            request.ready.wait()
            if request.error is not None:
                raise request.error
            attempted = True

        try:
            with workflow_stage("screening_prefix", f"Review {index + 1}/{len(payloads)} · {window.start_ms / 1000:g}–{window.end_ms / 1000:g}s", kind="agent",
                                inputs={"start_ms": window.start_ms, "end_ms": window.end_ms}) as stage:
                try:
                    _screen_prefix(provider, payloads[index], window, request_checkpoint, full_review=True)
                    if window.judgment and {c.aspect for c in window.judgment.review_checks} != FULL_REVIEW_ASPECTS:
                        window.validation_issues.append("Whole-video checklist is incomplete; unreported aspects remain unknown")
                        window.status = "needs_review"
                except BaseException as exc:
                    # Signal before trace cleanup so another completion cannot refill the pool.
                    stop.stop(exc)
                    raise
                stage.update(status=window.status, validation_issues=window.validation_issues,
                             mechanical_visual_status=window.mechanical_visual.status,
                             attention_context=window.attention_context, semantic_grounding_verified=False)
        except BaseException as exc:
            stop.stop(exc)
            window.status = "failed" if attempted else "not_attempted"
            window.error = ("Screening provider operation interrupted; returned usage unknown"
                            if attempted and isinstance(exc, (JobCanceled, JobInterrupted))
                            else safe_failure(exc) if attempted else "Screening stopped before this prefix was requested")
            return window, exc
        return window, None

    next_index = 0
    pending: dict[cf.Future, int] = {}
    with weave.ThreadPoolExecutor(max_workers=min(MAX_FULL_SCREEN_WORKERS, len(payloads))) as executor:
        while pending or (next_index < len(payloads) and stop.reason() is None):
            try:
                check_cancel()
            except BaseException as exc:
                stop.stop(exc)
            while stop.reason() is None and next_index < len(payloads) and len(pending) < MAX_FULL_SCREEN_WORKERS:
                index = next_index
                try:
                    check_cancel()
                    if on_stage:
                        window = report.windows[index]
                        on_stage("SCREENING_PREFIX", f"Review {index + 1}/{len(payloads)} · {window.start_ms / 1000:g}–{window.end_ms / 1000:g}s",
                                 {"screening_id": report.id, "end_ms": window.end_ms})
                    pending[executor.submit(review, index)] = index
                    next_index += 1
                except BaseException as exc:
                    stop.stop(exc)
            while True:
                try:
                    request = requests.get_nowait()
                except Empty:
                    break
                try:
                    check_cancel()
                    if stop.reason() is not None:
                        raise stop.reason()
                    report.windows[request.index] = request.window
                    report.model_calls += 1
                    try:
                        checkpoint()
                    except BaseException:
                        report.model_calls -= 1
                        raise
                except BaseException as exc:
                    stop.stop(exc)
                    request.error = exc
                finally:
                    request.ready.set()
            if not pending:
                break
            completed, _ = cf.wait(pending, timeout=0.025, return_when=cf.FIRST_COMPLETED)
            for future in completed:
                index = pending.pop(future)
                try:
                    window, error = future.result()
                    report.windows[index] = window
                    if error is not None:
                        stop.stop(error)
                    checkpoint()
                except BaseException as exc:
                    stop.stop(exc)
        # All provider calls and trace scopes have drained before terminal persistence.
    if stop.reason() is not None:
        raise stop.reason()


@traced("directorloop.quick_screen", kind="agent", display="Video review | evidence-linked suggestions")
@workflow_session(persist=_persist)
def run_screening(video_id: str, path: Path, provider: Any, data_dir: Path, *, screening_id: str | None = None, coverage: str = "quick", repair_incomplete: bool = False,
                  on_stage: Callable[..., Any] | None = None, is_cancelled: Callable[[], bool] | None = None) -> ScreenReport:
    """Provider is supplied with a durable spend guard by the API. No edits or final evaluations."""
    data_dir, path = Path(data_dir), Path(path)
    report = ScreenReport(id=screening_id or new_id("screen"), video_id=video_id, artifact_path=str(path), created_at=utc_now_iso())
    target = _record_path(data_dir, report.id)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Durable ownership prevents a duplicate job from silently replaying paid requests.
    with target.with_suffix(".started").open("x") as handle:
        handle.write(report.created_at)
    if target.exists():
        raise ValueError("Screening already exists; create a new explicit run")
    report.weave_call_id, report.weave_url = current_call_ref()
    started = time.monotonic()

    def checkpoint() -> None:
        report.elapsed_ms = int((time.monotonic() - started) * 1000)
        guard = getattr(provider, "spend_guard", None)
        if guard is not None:
            report.spend_guard = guard.ledger.summary()
        _persist(report, data_dir)

    def check_cancel() -> None:
        if is_cancelled and is_cancelled():
            raise JobCanceled("cancellation requested by the operator")

    try:
        checkpoint()
        check_cancel()
        with workflow_stage("screening_ingest", "Prepare video evidence") as stage:
            info = inspect_media(path)
            report.artifact_hash = sha256_file(path)
            report.duration_ms = int(info.duration_ms or 0)
            plan = screening_windows(report.duration_ms, coverage)
            if coverage == "full":
                report.limitations[0] = "Every timeline section is sampled chronologically; at most 8 intervals with up to 12 current and 12 earlier frames each. Not continuous video or native audio review."
            report.windows = [ScreenWindow(start_ms=start, end_ms=end, is_last_prefix=end == report.duration_ms) for start, end in plan]
            transcript, report.asr_cache_mode, asr_protocol = _transcript(path, report.artifact_hash, data_dir, info.has_audio)
            words = words_from_transcript(transcript)
            if report.asr_cache_mode == "unavailable":
                report.limitations.append("Transcript unavailable; do not infer missing speech")
            signals = VideoSignals(report.duration_ms, info.width or 0, info.height or 0, [], [], audio_rms(path) if info.has_audio else np.zeros(0))
            frames = FrameCache(path, report.duration_ms)
            request_policy = provider._request_policy() if callable(getattr(provider, "_request_policy", None)) else {
                "endpoint": getattr(provider, "_endpoint", None), "enable_thinking": getattr(provider, "enable_thinking", None),
                "max_output_tokens": getattr(getattr(provider, "spend_guard", None), "max_output_tokens", getattr(provider, "max_output_tokens", None)),
            }
            report.protocol = {"version": SCREENING_VERSION + (("-full-timeline-v8" if repair_incomplete else "-full-timeline-v7") if coverage == "full" else ""), "coverage": coverage, "provider": provider.capability.name, "model": provider.capability.model,
                               "max_prefix_calls": MAX_FULL_SCREEN_CALLS if coverage == "full" else 3, "prefix_windows_ms": [list(pair) for pair in plan],
                               "score_protocol": SCORE_PROTOCOL if coverage == "full" else None,
                               "asr_quote_options_version": "exact-recent-chunks-v1" if coverage == "full" else None,
                               "max_concurrent_prefix_calls": MAX_FULL_SCREEN_WORKERS if coverage == "full" else 1,
                               "provider_request_policy": request_policy, "mode": "fresh",
                               "instruction_sha256": sha256_bytes(SCREENING_INSTRUCTION.encode()), "schema_sha256": sha256_json(FULL_SCREENING_SCHEMA if coverage == "full" else SCREENING_SCHEMA),
                               "delivery_code_sha256": sha256_file(Path(__file__).with_name("delivery.py")) if coverage == "full" else None,
                               "review_rubric_sha256": sha256_bytes(FULL_REVIEW_RUBRIC.encode()) if coverage == "full" else None,
                               "system_prompt_sha256": sha256_bytes(VIEWER_SYSTEM_PROMPT.encode()),
                               "payload_builder_sha256": sha256_file(Path(__file__).parents[1] / "audit/review.py"),
                               "frame_extractor_sha256": sha256_file(Path(__file__).parents[1] / "media/frames.py"),
                               "provider_adapter_sha256": sha256_file(Path(__file__).parents[1] / "providers/openai_compat.py"),
                               "runner_sha256": sha256_file(Path(__file__)), "asr": asr_protocol,
                               "evidence_validation": EVIDENCE_VALIDATION_PROTOCOL,
                               "boundary_validation": BOUNDARY_VALIDATION_PROTOCOL if coverage == "full" else None,
                               "boundary_code_sha256": sha256_file(Path(__file__).with_name("boundary.py")) if coverage == "full" else None,
                               "mechanical_visual": MECHANICAL_PROTOCOL,
                               "mechanical_code_sha256": sha256_file(Path(__file__).with_name("mechanical.py")),
                               "transcript_sha256": sha256_json(transcript.to_dict()),
                               "asr_source": transcript.source, "asr_model": transcript.model,
                               "asr_language": transcript.requested_language, "sampling": ("contiguous intervals; at most12 context frames/256px and12 current frames/448px; complete ASR prefix" if coverage == "full" else "existing prefix builder: 1fps context/256px, 3fps current/448px, latest60 ASRwords"),
                               "semantic_validation": False, "edits": False, "platform_outcomes": False}
            report.protocol_fingerprint = sha256_json(report.protocol)
            if coverage == "full" and repair_incomplete:
                report.protocol["evidence_repair"] = {"version": "focused-evidence-repair-v2", "max_calls_per_section": 1, "max_total_calls": len(plan) * 2}
                report.protocol_fingerprint = sha256_json(report.protocol)
            stage.update(duration_ms=report.duration_ms, prefix_count=len(plan), asr_cache_mode=report.asr_cache_mode)
        checkpoint()
        if coverage == "full":
            payloads = []
            for window in report.windows:
                check_cancel()
                payload = _full_payload(frames, signals, words, window.start_ms, window.end_ms, fps=info.fps)
                window.delivery_signals = measure_delivery(transcript, signals.rms, window.start_ms, window.end_ms,
                    asr_available=report.asr_cache_mode in {"fresh", "cached"})
                timing_signals = {key: value for key, value in window.delivery_signals["signals"].items() if key != "caption_readability"}
                payload.instruction += "\nLocal measurements (not vocal-tone or retention judgments):\n" + json.dumps(timing_signals)
                window.evidence_frames = _save_payload_frames(payload, data_dir, report.id)
                window.frame_timestamps_ms = [f.timestamp_ms for f in payload.media.frames]
                window.prefix_asr_text = " ".join(w.text for w in words_heard_by(words, window.end_ms))
                window.mechanical_visual = measure_frame_changes(payload.media.frames, window.start_ms, window.end_ms)
                payloads.append(payload)
            checkpoint()
            _screen_full_windows(report, payloads, provider, checkpoint, check_cancel, on_stage)
            if repair_incomplete:
                from .repair_loop import repair_selected_windows, saved_payload
                repair_selected_windows(report, [saved_payload(w) for w in report.windows], provider, checkpoint, check_cancel, on_stage)
        else:
            for index, window in enumerate(report.windows):
                check_cancel()
                with workflow_stage("screening_prefix", f"Review {index + 1}/{len(report.windows)} · {window.start_ms / 1000:g}–{window.end_ms / 1000:g}s", kind="agent",
                                    inputs={"start_ms": window.start_ms, "end_ms": window.end_ms}) as stage:
                    payload = build_viewer_payload(frames, signals, words, window.start_ms, window.end_ms, fps=info.fps)
                    window.evidence_frames = _save_payload_frames(payload, data_dir, report.id)
                    window.frame_timestamps_ms = [f.timestamp_ms for f in payload.media.frames]
                    heard = words_heard_by(words, window.end_ms)
                    window.prefix_asr_text = " ".join(w.text for w in heard[-60:])
                    window.mechanical_visual = measure_frame_changes(payload.media.frames, window.start_ms, window.end_ms)
                    check_cancel()
                    if on_stage:
                        # Direct delivery preserves lease-interruption errors that the generic stage journal suppresses.
                        on_stage("SCREENING_PREFIX", f"Review {index + 1}/{len(report.windows)} · {window.start_ms / 1000:g}–{window.end_ms / 1000:g}s", {"screening_id": report.id, "end_ms": window.end_ms})
                    report.model_calls += 1
                    try:
                        report.windows[index] = _screen_prefix(provider, payload, window, checkpoint)
                    except (JobCanceled, JobInterrupted):
                        window.status, window.error = "failed", "Screening provider operation interrupted; returned usage unknown"
                        raise
                    except BaseException as exc:
                        window.status, window.error = "failed", safe_failure(exc)
                        raise
                    stage.update(status=window.status, validation_issues=window.validation_issues,
                                 mechanical_visual_status=window.mechanical_visual.status,
                                 attention_context=window.attention_context, semantic_grounding_verified=False)
                checkpoint()
        check_cancel()
        if sha256_file(path) != report.artifact_hash:
            raise ValueError("Source media changed while screening")
        report.status = "needs_review" if report.asr_cache_mode == "unavailable" or any(w.status == "needs_review" for w in report.windows) else "complete"
    except JobCanceled:
        report.status, report.error = "canceled", "cancellation requested by the operator"
    except JobInterrupted:
        report.status, report.error = "failed", "worker execution interrupted; not retried"
    except Exception as exc:
        report.status, report.error = "failed", safe_failure(exc)
    except BaseException:
        report.status, report.error = "failed", "worker execution interrupted; not retried"
        raise
    finally:
        for window in report.windows:
            if window.status == "pending":
                window.status = "not_attempted"
                window.error = "Screening stopped before this prefix was requested"
        attempted = [ScreenWindow.model_validate(attempt["window"]) for w in report.windows if w.review_attempts for attempt in w.review_attempts]
        attempted.extend(w for w in report.windows if not w.review_attempts and w.status != "not_attempted")
        if attempted and all(w.input_tokens is not None for w in attempted):
            report.input_tokens = sum(w.input_tokens for w in attempted)
        if attempted and all(w.output_tokens is not None for w in attempted):
            report.output_tokens = sum(w.output_tokens for w in attempted)
        report.ended_at = utc_now_iso()
        checkpoint()
    if on_stage:
        on_stage("FAILED" if report.status == "failed" else "CANCELED" if report.status == "canceled" else "DONE",
                 "Screening saved; review required", {"screening_id": report.id, "status": report.status, "weave_url": report.weave_url})
    return report
