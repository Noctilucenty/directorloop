"""One evidence correction per incomplete section, with durable original attempts."""

from __future__ import annotations

import concurrent.futures as cf
from collections.abc import Callable
from pathlib import Path
from queue import Empty, Queue
from typing import Any

import weave

from ..audit.models import InspectedWindow
from ..audit.review import ViewerPayload
from ..domain.ids import sha256_bytes
from ..media.frames import SampledFrame
from ..observability.workflow import workflow_stage
from ..providers.base import ProbeMedia
from .models import ScreenReport, ScreenWindow


def saved_payload(window: ScreenWindow) -> ViewerPayload:
    """Reconstruct only the immutable evidence supplied at this original cutoff."""
    frames = []
    for evidence in window.evidence_frames:
        jpeg = Path(evidence.path).read_bytes()
        if sha256_bytes(jpeg) != evidence.sha256 or not 0 <= evidence.t_ms < window.end_ms:
            raise ValueError("Saved review evidence changed or crosses the cutoff")
        frames.append(SampledFrame(evidence.t_ms, jpeg, evidence.width, evidence.height))
    if not frames or [f.timestamp_ms for f in frames] != window.frame_timestamps_ms:
        raise ValueError("Saved review frame inventory is incomplete")
    media = ProbeMedia(kind="frames", duration_ms=window.end_ms, frames=frames, transcript=window.prefix_asr_text or None)
    instruction = (
        f"Review the current section {window.start_ms}–{window.end_ms} ms. Earlier images are context only. "
        "Images and transcript end at this cutoff. No later media is available.\n"
        + "Frame timestamps in supplied order: " + ", ".join(str(f.timestamp_ms) for f in frames)
        + "\nTranscript available at this cutoff:\n" + window.prefix_asr_text
    )
    return ViewerPayload(media, instruction, InspectedWindow(
        kind="prefix", start_ms=window.start_ms, end_ms=window.end_ms,
        frame_timestamps_ms=window.frame_timestamps_ms, frame_width=448, boundary_ms=window.end_ms,
    ))


def _attempt(window: ScreenWindow, phase: str, accepted: bool) -> dict[str, Any]:
    return {"phase": phase, "accepted": accepted, "window": window.model_dump(mode="json", exclude={"review_attempts"})}


def repair_selected_windows(report: ScreenReport, payloads: list[ViewerPayload], provider: Any,
                            checkpoint: Callable[[], None], check_cancel: Callable[[], None],
                            on_stage: Callable[..., Any] | None = None, *, focused: bool = True,
                            upgrade: bool = False) -> None:
    """Workers inspect evidence; only the coordinator persists and admits results.

    Each section gets at most one correction. A provider error closes dispatch;
    already-running requests drain and keep their charged usage and evidence.
    """
    from .repair import repair_needed, repair_screen_window
    if focused:
        from .repair import focused_repair_screen_window
        reviewer = focused_repair_screen_window
    else:
        reviewer = repair_screen_window
    from .runner import MAX_FULL_SCREEN_WORKERS, _DispatchCheckpoint, _ScreeningStop

    if len(payloads) != len(report.windows):
        raise ValueError("Repair payload inventory does not match the saved sections")
    phase = "focused_repair" if focused else "repair"
    indices = [i for i, w in enumerate(report.windows) if repair_needed(w) and (
        not w.review_attempts or upgrade and not any(a["phase"] == phase for a in w.review_attempts)
    )]
    if not indices:
        report.review_repair = {"version": "bounded-evidence-repair-v1", "status": "complete", "attempted_sections": 0, "completed_sections": 0}
        checkpoint()
        return
    previous = report.review_repair
    report.review_repair = {"version": "focused-evidence-repair-v2" if focused else "bounded-evidence-repair-v1", "status": "running", "attempted_sections": 0, "completed_sections": 0}
    if upgrade and previous:
        report.review_repair["previous_pass"] = previous
    checkpoint()
    requests: Queue[_DispatchCheckpoint] = Queue()
    stop = _ScreeningStop()

    def review(index: int):
        original = report.windows[index].model_copy(deep=True)
        dispatched = None

        def before_call(candidate: ScreenWindow) -> None:
            nonlocal dispatched
            request = _DispatchCheckpoint(index, candidate.model_copy(deep=True))
            requests.put(request)
            request.ready.wait()
            if request.error is not None:
                raise request.error
            dispatched = candidate

        try:
            with workflow_stage("screening_repair", f"Recheck section {index + 1} · {original.start_ms / 1000:g}–{original.end_ms / 1000:g}s", kind="agent",
                                inputs={"start_ms": original.start_ms, "end_ms": original.end_ms, "original_call_id": original.weave_call_id}) as stage:
                try:
                    base = ScreenWindow.model_validate(original.review_attempts[-1]["window"]) if upgrade and original.review_attempts else original
                    candidate = reviewer(provider, payloads[index], base, checkpoint=before_call)
                except BaseException as exc:
                    stop.stop(exc)
                    raise
                stage.update(status=candidate.status, validation_issues=candidate.validation_issues, semantic_grounding_verified=False)
                return candidate, None
        except BaseException as exc:
            stop.stop(exc)
            if dispatched is not None:
                from ..jobs.worker import safe_failure
                dispatched.status = "failed"
                dispatched.error = safe_failure(exc)
            return dispatched, exc

    pending = {}
    next_index = 0
    with weave.ThreadPoolExecutor(max_workers=min(MAX_FULL_SCREEN_WORKERS, len(indices))) as pool:
        while pending or (next_index < len(indices) and stop.reason() is None):
            try:
                check_cancel()
            except BaseException as exc:
                stop.stop(exc)
            while stop.reason() is None and next_index < len(indices) and len(pending) < MAX_FULL_SCREEN_WORKERS:
                index = indices[next_index]
                try:
                    check_cancel()
                    if on_stage:
                        on_stage("SCREENING_REPAIR", f"Rechecking incomplete section {index + 1}/{len(report.windows)}", {"screening_id": report.id, "window_index": index})
                    pending[pool.submit(review, index)] = index
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
                    original = report.windows[request.index]
                    history = list(original.review_attempts)
                    original.review_attempts = (history or [_attempt(original, "initial", True)]) + [_attempt(request.window, phase, False)]
                    report.model_calls += 1
                    report.review_repair["attempted_sections"] += 1
                    try:
                        checkpoint()
                    except BaseException:
                        original.review_attempts = history
                        report.model_calls -= 1
                        report.review_repair["attempted_sections"] -= 1
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
                candidate, error = future.result()
                original = report.windows[index]
                if candidate is not None and original.review_attempts:
                    accepted = candidate.status == "complete" and not repair_needed(candidate)
                    original.review_attempts[-1] = _attempt(candidate, phase, accepted)
                    if accepted:
                        candidate.review_attempts = original.review_attempts
                        report.windows[index] = candidate
                        report.review_repair["completed_sections"] += 1
                if error is not None:
                    stop.stop(error)
                try:
                    checkpoint()
                except BaseException as exc:
                    stop.stop(exc)
    report.review_repair["status"] = "complete" if report.review_repair["completed_sections"] == len(indices) else "partial"
    checkpoint()
    if stop.reason() is not None:
        raise stop.reason()
