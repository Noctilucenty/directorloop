"""Transcription of rendered audio with whisper.cpp.

The transcript comes from the actual output file, never from the planned script.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..config import get_settings
from .probe import FFMPEG, MediaError

WHISPER_CLI = shutil.which("whisper-cli") or "/opt/homebrew/bin/whisper-cli"
DEFAULT_MODEL = Path(os.environ.get("DL_WHISPER_MODEL", str(Path.home() / ".cache/whisper.cpp/ggml-large-v3-turbo-q5_0.bin")))


@dataclass(frozen=True)
class TranscriptToken:
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True)
class TranscriptSegment:
    start_ms: int
    end_ms: int
    text: str
    tokens: tuple[TranscriptToken, ...] = ()


@dataclass
class Transcript:
    text: str
    segments: list[TranscriptSegment] = field(default_factory=list)
    source: str = "whisper.cpp"
    model: str = ""
    has_speech: bool = True
    note: str = ""
    requested_language: str = "en"
    detected_language: str | None = None

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "segments": [
                {"start_ms": s.start_ms, "end_ms": s.end_ms, "text": s.text, "tokens": [t.__dict__ for t in s.tokens]}
                for s in self.segments
            ],
            "source": self.source,
            "model": self.model,
            "has_speech": self.has_speech,
            "note": self.note,
            "requested_language": self.requested_language,
            "detected_language": self.detected_language,
        }


def whisper_available() -> bool:
    return Path(WHISPER_CLI).exists() and DEFAULT_MODEL.exists()


def extract_wav16k(path: str | Path, out_path: str | Path) -> None:
    cmd = [
        FFMPEG,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "wav",
        str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    if proc.returncode != 0:
        raise MediaError(f"audio extraction failed: {proc.stderr[:200]}")


def transcribe(path: str | Path, model_path: Path | None = None, timeout: int = 180, language: str | None = None) -> Transcript:
    """Transcribe the audio track of a media file. Returns an empty transcript if there is no audio."""
    model_path = model_path or DEFAULT_MODEL
    language = language or get_settings().dl_asr_language
    if not (language == "auto" or language.isalpha() and language.islower() and 2 <= len(language) <= 3):
        raise ValueError("ASR language must be auto or a two/three-letter lowercase language code")
    if not whisper_available():
        return Transcript(text="", segments=[], has_speech=False, note="whisper.cpp unavailable; transcript missing", requested_language=language)
    with tempfile.TemporaryDirectory(prefix="dl_asr_") as tmp:
        wav = Path(tmp) / "audio.wav"
        try:
            extract_wav16k(path, wav)
        except MediaError as exc:
            return Transcript(text="", segments=[], has_speech=False, note=f"no audio: {exc}", requested_language=language)
        out_base = Path(tmp) / "out"
        cmd = [
            WHISPER_CLI,
            "-m",
            str(model_path),
            "-f",
            str(wav),
            "-ojf",
            "-of",
            str(out_base),
            "-ml",
            "0",
            "-l",
            language,
            "-np",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        if proc.returncode != 0:
            raise MediaError(f"whisper-cli failed: {proc.stderr[-300:]}")
        json_path = out_base.with_suffix(".json")
        if not json_path.exists():
            raise MediaError("whisper-cli produced no JSON output")
        data = json.loads(json_path.read_text(encoding="utf-8"))
    segments: list[TranscriptSegment] = []
    for item in data.get("transcription", []):
        offsets = item.get("offsets", {})
        text = (item.get("text") or "").strip()
        if not text or text.startswith("[") and text.endswith("]"):
            continue
        tokens = tuple(
            TranscriptToken(start_ms=int(tok.get("offsets", {}).get("from", 0)), end_ms=int(tok.get("offsets", {}).get("to", 0)), text=str(tok.get("text", "")))
            for tok in item.get("tokens", [])
            if str(tok.get("text", "")).strip() and not str(tok.get("text", "")).startswith("[_")
        )
        segments.append(
            TranscriptSegment(start_ms=int(offsets.get("from", 0)), end_ms=int(offsets.get("to", 0)), text=text, tokens=tokens)
        )
    text = " ".join(s.text for s in segments).strip()
    return Transcript(
        text=text,
        segments=segments,
        source="whisper.cpp",
        model=model_path.name,
        has_speech=bool(text),
        note="" if text else "no speech detected",
        requested_language=language,
        detected_language=data.get("result", {}).get("language") or None,
    )
