"""Model comprehension probes: build viewer input from the rendered artifact and run trials."""

from __future__ import annotations

import concurrent.futures as cf
from pathlib import Path

from ..domain.evaluation import ProbeAnswer, ProbeModality
from ..domain.truth import EvaluationSuite
from ..media.frames import SampledFrame, sample_frames
from ..media.transcribe import Transcript
from ..providers.base import MediaProbeProvider, ProbeMedia

MAX_INLINE_VIDEO_BYTES = 19 * 1024 * 1024


def build_probe_media(
    artifact_path: Path,
    duration_ms: int,
    provider: MediaProbeProvider,
    transcript: Transcript | None,
    frame_count: int = 8,
    prefer_video: bool = True,
) -> tuple[ProbeMedia, ProbeModality, list[SampledFrame]]:
    """Viewer input built only from the rendered file. Returns the media and its honest modality label."""
    caps = provider.capability.modalities
    size = artifact_path.stat().st_size
    if prefer_video and "video" in caps and size <= MAX_INLINE_VIDEO_BYTES:
        media = ProbeMedia(kind="video", duration_ms=duration_ms, video_bytes=artifact_path.read_bytes(), mime_type="video/mp4")
        return media, ProbeModality.VIDEO_NATIVE, []
    if "image" in caps:
        frames = sample_frames(artifact_path, duration_ms, count=frame_count, max_width=512)
        text = transcript.text if transcript is not None else None
        media = ProbeMedia(kind="frames", duration_ms=duration_ms, frames=frames, transcript=text if text is not None else "")
        modality = ProbeModality.FRAMES_AND_TRANSCRIPT if text else ProbeModality.FRAMES_AND_TRANSCRIPT
        return media, modality, frames
    text = transcript.text if transcript is not None else ""
    media = ProbeMedia(kind="frames", duration_ms=duration_ms, frames=[], transcript=text)
    return media, ProbeModality.TRANSCRIPT_ONLY, []


def run_probe_trials(
    provider: MediaProbeProvider,
    media: ProbeMedia,
    suite: EvaluationSuite,
    trials: int,
    seed_base: int = 1000,
    concurrency: int = 3,
) -> list[ProbeAnswer]:
    """Run `trials` independent viewer calls with per-trial option shuffles. Bounded concurrency."""

    def one(trial: int) -> list[ProbeAnswer]:
        views = suite.viewer_views(seed=seed_base + trial)
        return provider.answer_questions(media, views, seed=trial)

    answers: list[ProbeAnswer] = []
    with cf.ThreadPoolExecutor(max_workers=max(1, min(concurrency, trials))) as ex:
        for batch in ex.map(one, range(trials)):
            answers.extend(batch)
    return answers
