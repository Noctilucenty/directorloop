"""Gemini adapter: native video input for probes, JSON-schema output for planning.

Verified against the installed google-genai SDK at build time; the smoke test in
`registry.py` records the actual result for docs/VERIFIED_CAPABILITIES.md.
"""

from __future__ import annotations

import time
from typing import Any

from ..domain.evaluation import ProbeAnswer
from ..domain.truth import ProbeQuestionView
from .base import (
    NO_MEDIA_SYSTEM_PROMPT,
    VIEWER_SYSTEM_PROMPT,
    CompletionResult,
    ProbeMedia,
    ProviderCapability,
    ProviderError,
    answers_schema,
    extract_json,
    parse_answers,
    render_questions_text,
)


class GeminiProvider:
    """Both a MediaProbeProvider and a TextPlannerProvider."""

    def __init__(self, api_key: str, model: str, role: str = "probe", timeout_s: int = 60, thinking: str = "low") -> None:
        if not api_key:
            raise ProviderError("GEMINI_API_KEY is not configured")
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self.model = model
        self.timeout_s = timeout_s
        self.thinking = thinking
        self.capability = ProviderCapability(
            name="gemini",
            role=role,
            model=model,
            modalities={"text", "image", "video", "audio"},
            structured_output=True,
            docs_ref="https://ai.google.dev/gemini-api/docs/video-understanding",
            present=True,
        )

    # ---- low level ---------------------------------------------------------
    def _generate(self, contents: list[Any], system: str, schema: dict[str, Any] | None, temperature: float) -> CompletionResult:
        from google.genai import types

        kwargs: dict[str, Any] = {
            "system_instruction": system,
            "temperature": temperature,
            "max_output_tokens": 4096,
            "http_options": types.HttpOptions(timeout=self.timeout_s * 1000),
        }
        if schema is not None:
            kwargs["response_mime_type"] = "application/json"
            kwargs["response_json_schema"] = schema
        attempts: list[dict[str, Any]] = []
        if self.thinking:
            attempts.append({**kwargs, "thinking_config": types.ThinkingConfig(thinking_level=self.thinking)})
        attempts.append(kwargs)
        last_exc: Exception | None = None
        for cfg in attempts:
            t0 = time.monotonic()
            try:
                resp = self._client.models.generate_content(
                    model=self.model, contents=contents, config=types.GenerateContentConfig(**cfg)
                )
            except Exception as exc:  # noqa: BLE001 - provider errors are reported, not hidden
                last_exc = exc
                msg = str(exc).lower()
                if "thinking" in msg and "thinking_config" in cfg:
                    continue
                raise ProviderError(f"gemini request failed: {str(exc)[:300]}") from exc
            latency = int((time.monotonic() - t0) * 1000)
            text = resp.text or ""
            usage = getattr(resp, "usage_metadata", None)
            data = extract_json(text) if schema is not None else {"text": text}
            return CompletionResult(
                data=data,
                latency_ms=latency,
                model=self.model,
                raw_text=text,
                input_tokens=getattr(usage, "prompt_token_count", None),
                output_tokens=getattr(usage, "candidates_token_count", None),
            )
        raise ProviderError(f"gemini request failed: {str(last_exc)[:300]}")

    def _media_parts(self, media: ProbeMedia) -> list[Any]:
        from google.genai import types

        parts: list[Any] = []
        if media.kind == "video" and media.video_bytes:
            parts.append(types.Part.from_bytes(data=media.video_bytes, mime_type=media.mime_type))
        elif media.kind == "frames":
            for fr in media.frames:
                parts.append(types.Part.from_text(text=f"Frame at t={fr.timestamp_ms / 1000:.2f}s:"))
                parts.append(types.Part.from_bytes(data=fr.jpeg, mime_type="image/jpeg"))
            if media.transcript:
                parts.append(types.Part.from_text(text=f"Transcript of the audio track:\n{media.transcript}"))
            elif media.transcript == "":
                parts.append(types.Part.from_text(text="The audio track contains no speech."))
        return parts

    # ---- MediaProbeProvider -------------------------------------------------
    def answer_questions(self, media: ProbeMedia, questions: list[ProbeQuestionView], *, seed: int) -> list[ProbeAnswer]:
        from google.genai import types

        system = NO_MEDIA_SYSTEM_PROMPT if media.kind == "none" else VIEWER_SYSTEM_PROMPT
        parts = self._media_parts(media)
        parts.append(types.Part.from_text(text=render_questions_text(questions)))
        schema = answers_schema([q.id for q in questions])
        try:
            res = self._generate([types.Content(role="user", parts=parts)], system, schema, temperature=0.2)
        except ProviderError as exc:
            return [
                ProbeAnswer(question_id=q.id, trial=seed, chosen_option_id=None, error=str(exc)[:200]) for q in questions
            ]
        return parse_answers(res.data, questions, trial=seed, latency_ms=res.latency_ms, raw=res.raw_text)

    def judge_json(self, media: ProbeMedia, instruction: str, schema: dict[str, Any]) -> CompletionResult:
        from google.genai import types

        parts = self._media_parts(media)
        parts.append(types.Part.from_text(text=instruction))
        return self._generate([types.Content(role="user", parts=parts)], VIEWER_SYSTEM_PROMPT, schema, temperature=0.1)

    # ---- TextPlannerProvider ------------------------------------------------
    def complete_json(self, system: str, user: str, schema: dict[str, Any], *, temperature: float = 0.2) -> CompletionResult:
        from google.genai import types

        return self._generate([types.Content(role="user", parts=[types.Part.from_text(text=user)])], system, schema, temperature)
