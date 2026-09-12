"""Speech synthesis providers for narration assets.

Two adapters share one interface: ElevenLabs (preferred when a key and voice id are configured)
and macOS `say` (zero-cost fallback). Output is always a 44.1 kHz mono 16-bit WAV so downstream
media code never has to care which provider produced the narration.

Secrets are read from settings; this module never logs or returns key values.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from directorloop.config import Settings, get_settings

FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
FFPROBE = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"

ELEVENLABS_BASE = "https://api.elevenlabs.io/v1"
# Newest general text-to-speech model at the time of writing. The configured key cannot list
# models (`models_read` permission missing), so availability is proven by a successful call.
ELEVENLABS_DEFAULT_MODEL = "eleven_v3"
ELEVENLABS_FALLBACK_MODEL = "eleven_multilingual_v2"


class SpeechError(RuntimeError):
    pass


@dataclass(frozen=True)
class SpeechResult:
    provider: str
    model: str
    duration_ms: int
    sample_rate: int
    path: Path


class SpeechProvider(Protocol):
    name: str

    def synthesize(self, text: str, out_path: Path) -> SpeechResult: ...


def _probe_duration_ms(path: Path) -> int:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return int(round(float(out) * 1000)) if out else 0


def _to_wav_mono_44k(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            FFMPEG, "-y", "-v", "error", "-i", str(src),
            "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le",
            "-fflags", "+bitexact", "-flags", "+bitexact", "-map_metadata", "-1",
            str(dst),
        ],
        check=True,
    )


class ElevenLabsSpeech:
    name = "elevenlabs"

    def __init__(self, api_key: str, voice_id: str, model_id: str = ELEVENLABS_DEFAULT_MODEL, timeout_s: float = 90.0):
        if not api_key or not voice_id:
            raise SpeechError("ElevenLabs needs an api key and a voice id")
        self._api_key = api_key
        self.voice_id = voice_id
        self.model_id = model_id
        self.timeout_s = timeout_s

    def _request(self, text: str, model_id: str) -> bytes:
        url = f"{ELEVENLABS_BASE}/text-to-speech/{self.voice_id}"
        resp = httpx.post(
            url,
            params={"output_format": "mp3_44100_128"},
            headers={"xi-api-key": self._api_key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
            json={"text": text, "model_id": model_id},
            timeout=self.timeout_s,
        )
        if resp.status_code != 200:
            detail = ""
            try:
                detail = resp.json().get("detail", {}).get("message", "")
            except Exception:  # noqa: BLE001 - body may not be JSON
                detail = resp.text[:200]
            raise SpeechError(f"ElevenLabs HTTP {resp.status_code} for model {model_id}: {detail}")
        return resp.content

    def synthesize(self, text: str, out_path: Path) -> SpeechResult:
        model_used = self.model_id
        try:
            audio = self._request(text, self.model_id)
        except SpeechError as first:
            if self.model_id == ELEVENLABS_FALLBACK_MODEL:
                raise
            try:
                audio = self._request(text, ELEVENLABS_FALLBACK_MODEL)
                model_used = ELEVENLABS_FALLBACK_MODEL
            except SpeechError as second:
                raise SpeechError(f"{first}; fallback also failed: {second}") from second
        with tempfile.TemporaryDirectory() as tmp:
            mp3 = Path(tmp) / "narration.mp3"
            mp3.write_bytes(audio)
            _to_wav_mono_44k(mp3, out_path)
        return SpeechResult(
            provider=self.name, model=model_used, duration_ms=_probe_duration_ms(out_path), sample_rate=44100, path=out_path
        )


class MacOSSayProvider:
    name = "macos_say"

    def __init__(self, voice: str = "Samantha", rate_wpm: int | None = None):
        self.voice = voice
        self.rate_wpm = rate_wpm
        if shutil.which("say") is None:
            raise SpeechError("macOS `say` is not available on this machine")

    def synthesize(self, text: str, out_path: Path) -> SpeechResult:
        with tempfile.TemporaryDirectory() as tmp:
            aiff = Path(tmp) / "narration.aiff"
            cmd = ["say", "-v", self.voice, "-o", str(aiff)]
            if self.rate_wpm:
                cmd += ["-r", str(self.rate_wpm)]
            cmd.append(text)
            subprocess.run(cmd, check=True)
            _to_wav_mono_44k(aiff, out_path)
        return SpeechResult(
            provider=self.name, model=self.voice, duration_ms=_probe_duration_ms(out_path), sample_rate=44100, path=out_path
        )


def default_speech_provider(settings: Settings | None = None, prefer: str = "auto") -> SpeechProvider:
    """`prefer` is auto | elevenlabs | say. Auto picks ElevenLabs when configured, else macOS say."""
    s = settings or get_settings()
    if prefer == "say":
        return MacOSSayProvider()
    if prefer in ("auto", "elevenlabs") and s.elevenlabs_api_key and s.elevenlabs_voice_id:
        return ElevenLabsSpeech(s.elevenlabs_api_key, s.elevenlabs_voice_id)
    if prefer == "elevenlabs":
        raise SpeechError("ElevenLabs requested but ELEVENLABS_API_KEY / ELEVENLABS_VOICE_ID are not configured")
    return MacOSSayProvider()
