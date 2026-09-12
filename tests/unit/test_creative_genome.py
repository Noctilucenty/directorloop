"""Creative genome building blocks: sentence beats, quiet-point snapping, static spans, cut detection."""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

from directorloop.creative.genome import sentence_beats, split_segments_at_sentences
from directorloop.creative.signals import (
    VideoSignals,
    audio_rms,
    detect_cuts,
    first_visual_change_ms,
    motion_curve,
    silence_gaps,
    snap_to_quiet,
    static_spans,
)
from directorloop.media.probe import FFMPEG
from directorloop.media.transcribe import Transcript, TranscriptSegment, TranscriptToken


def _tokens(words: list[tuple[str, int, int]]) -> tuple[TranscriptToken, ...]:
    return tuple(TranscriptToken(start_ms=a, end_ms=b, text=t) for t, a, b in words)


def _transcript() -> Transcript:
    seg1 = TranscriptSegment(
        start_ms=0, end_ms=6000, text="That is a top. Tops fall over. This one climbs.",
        tokens=_tokens([(" That", 0, 300), (" is", 300, 500), (" a", 500, 600), (" top.", 600, 1400),
                        (" Tops", 1900, 2300), (" fall", 2300, 2800), (" over.", 2800, 3500),
                        (" This", 4000, 4400), (" one", 4400, 4700), (" climbs.", 4700, 6000)]),
    )
    seg2 = TranscriptSegment(start_ms=6300, end_ms=9000, text="Friction lifts it.", tokens=())
    return Transcript(text=seg1.text + " " + seg2.text, segments=[seg1, seg2])


def test_split_segments_at_sentences_uses_token_times() -> None:
    parts = split_segments_at_sentences(_transcript())
    assert [p.text for p in parts] == ["That is a top.", "Tops fall over.", "This one climbs.", "Friction lifts it."]
    assert parts[0].end_ms == 1400 and parts[1].start_ms == 1900 and parts[2].start_ms == 4000 and parts[2].end_ms == 6000


def _signals(duration_ms: int, quiet_ms: list[int]) -> VideoSignals:
    rms = np.full(duration_ms // 10, 0.5, dtype=np.float32)
    for q in quiet_ms:
        rms[q // 10 - 3 : q // 10 + 3] = 0.01
    return VideoSignals(duration_ms=duration_ms, width=720, height=1280, cuts_ms=[], motion=[], rms=rms)


def test_sentence_beats_tile_the_video_and_snap_to_pauses() -> None:
    duration = 10000
    beats = sentence_beats(_transcript(), duration, _signals(duration, quiet_ms=[1650, 3750, 6150]))
    assert beats[0].start_ms == 0 and beats[-1].end_ms == duration
    for a, b in zip(beats, beats[1:], strict=False):
        assert a.end_ms == b.start_ms, "beats must tile the timeline without gaps"
    cuts = [b.start_ms for b in beats[1:]]
    for cut in cuts:
        assert min(abs(cut - q) for q in (1650, 3750, 6150)) <= 40, f"cut {cut} not snapped to a pause"
    assert all(b.end_ms - b.start_ms >= 300 for b in beats)


def test_snap_to_quiet_moves_to_local_minimum() -> None:
    rms = np.full(500, 0.4, dtype=np.float32)
    rms[210:214] = 0.0
    assert abs(snap_to_quiet(rms, 2000, window_ms=220) - 2115) <= 20


def test_static_spans_and_first_visual_change() -> None:
    motion = [(t, 0.001) for t in range(100, 5000, 100)] + [(5000, 0.2)] + [(t, 0.001) for t in range(5100, 8000, 100)]
    spans = static_spans(cuts_ms=[6000], motion=motion, duration_ms=8000)
    assert spans[0][1] - spans[0][0] >= 4500
    assert first_visual_change_ms([6000], motion) == 5000


def test_silence_gaps_relative_to_speech_level() -> None:
    rms = np.full(400, 0.3, dtype=np.float32)
    rms[150:200] = 0.01  # 500 ms pause
    gaps = silence_gaps(rms, 0, 4000)
    assert len(gaps) == 1 and abs(gaps[0][0] - 1500) <= 10 and abs(gaps[0][1] - 2000) <= 10


def test_detect_cuts_motion_and_rms_on_real_ffmpeg_clip(tmp_path: Path) -> None:
    clip = tmp_path / "cut.mp4"
    cmd = [
        FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=navy:s=180x320:d=1:r=30",
        "-f", "lavfi", "-i", "color=c=yellow:s=180x320:d=1:r=30",
        "-f", "lavfi", "-i", "sine=frequency=300:duration=2",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-map", "2:a",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(clip),
    ]
    subprocess.run(cmd, check=True, timeout=60)
    cuts = detect_cuts(clip)
    assert len(cuts) == 1 and abs(cuts[0] - 1000) <= 70
    curve = motion_curve(clip, 180, 320)
    assert len(curve) >= 15 and max(v for _, v in curve) > 0.2
    assert len(audio_rms(clip)) >= 180
