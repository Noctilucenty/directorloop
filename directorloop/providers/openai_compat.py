"""OpenAI-compatible chat adapter: OpenAI, W&B Inference, and TypeSafe (once its contract is known).

Frame-based probing only: the model receives timestamped JPEG frames plus the ASR
transcript of the rendered audio. It never receives the actual video file, so runs
through this adapter are labeled `frames_and_transcript`.
"""

from __future__ import annotations

import base64
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

WANDB_INFERENCE_BASE_URL = "https://api.inference.wandb.ai/v1"


class OpenAICompatProvider:
    def __init__(
        self,
        name: str,
        api_key: str,
        model: str,
        base_url: str | None = None,
        project: str | None = None,
        role: str = "probe",
        vision: bool = True,
        timeout_s: int = 60,
        docs_ref: str = "",
        reasoning_effort: str | None = None,
        supports_temperature: bool | None = None,
    ) -> None:
        if not api_key:
            raise ProviderError(f"{name}: API key is not configured")
        import openai

        kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout_s, "max_retries": 1}
        if base_url:
            kwargs["base_url"] = base_url
        if project:
            kwargs["project"] = project  # sent as the OpenAI-Project header (W&B Inference usage attribution)
        self._client = openai.OpenAI(**kwargs)
        self.name = name
        self.model = model
        self.capability = ProviderCapability(
            name=name,
            role=role,
            model=model,
            modalities={"text", "image"} if vision else {"text"},
            structured_output=True,
            docs_ref=docs_ref,
            present=True,
        )
        self._json_mode: str = "json_schema"  # downgraded automatically if the endpoint rejects it
        self._supports_temperature: bool | None = supports_temperature  # reasoning models accept only the default
        self.reasoning_effort = reasoning_effort

    def _chat(self, messages: list[dict[str, Any]], schema: dict[str, Any] | None, temperature: float) -> CompletionResult:
        attempts = [self._json_mode, "json_object", "none"] if schema is not None else ["none"]
        last_exc: Exception | None = None
        send_temperature = self._supports_temperature is not False
        reasoning_effort = self.reasoning_effort
        for mode in attempts:
            kwargs: dict[str, Any] = {"model": self.model, "messages": messages}
            if send_temperature:
                kwargs["temperature"] = temperature
            if reasoning_effort:
                kwargs["reasoning_effort"] = reasoning_effort
            if mode == "json_schema":
                kwargs["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "structured_reply", "schema": schema, "strict": False},
                }
            elif mode == "json_object":
                kwargs["response_format"] = {"type": "json_object"}
            t0 = time.monotonic()
            try:
                resp = self._client.chat.completions.create(**kwargs)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                msg = str(exc).lower()
                if "temperature" in msg and send_temperature:
                    send_temperature = False
                    self._supports_temperature = False
                    attempts.insert(attempts.index(mode), mode)  # retry the same mode without temperature
                    continue
                if "reasoning_effort" in msg and reasoning_effort:
                    reasoning_effort = None
                    self.reasoning_effort = None
                    attempts.insert(attempts.index(mode), mode)
                    continue
                if mode != "none" and ("response_format" in msg or "json_schema" in msg or "json_object" in msg):
                    self._json_mode = "json_object" if mode == "json_schema" else "none"
                    continue
                raise ProviderError(f"{self.name} request failed: {str(exc)[:300]}") from exc
            latency = int((time.monotonic() - t0) * 1000)
            text = (resp.choices[0].message.content or "") if resp.choices else ""
            usage = getattr(resp, "usage", None)
            data = extract_json(text) if schema is not None else {"text": text}
            return CompletionResult(
                data=data,
                latency_ms=latency,
                model=self.model,
                raw_text=text,
                input_tokens=getattr(usage, "prompt_tokens", None),
                output_tokens=getattr(usage, "completion_tokens", None),
            )
        raise ProviderError(f"{self.name} request failed: {str(last_exc)[:300]}")

    def _media_content(self, media: ProbeMedia) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = []
        if media.kind == "video":
            raise ProviderError(f"{self.name} does not accept video input; use frames")
        if media.kind == "frames":
            if "image" not in self.capability.modalities:
                raise ProviderError(f"{self.name}/{self.model} is text-only; refusing to route a visual task to it")
            for fr in media.frames:
                content.append({"type": "text", "text": f"Frame at t={fr.timestamp_ms / 1000:.2f}s:"})
                b64 = base64.b64encode(fr.jpeg).decode("ascii")
                content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
            if media.transcript:
                content.append({"type": "text", "text": f"Transcript of the audio track:\n{media.transcript}"})
            elif media.transcript == "":
                content.append({"type": "text", "text": "The audio track contains no speech."})
        return content

    def answer_questions(self, media: ProbeMedia, questions: list[ProbeQuestionView], *, seed: int) -> list[ProbeAnswer]:
        system = NO_MEDIA_SYSTEM_PROMPT if media.kind == "none" else VIEWER_SYSTEM_PROMPT
        try:
            content = self._media_content(media)
        except ProviderError as exc:
            return [ProbeAnswer(question_id=q.id, trial=seed, chosen_option_id=None, error=str(exc)[:200]) for q in questions]
        content.append({"type": "text", "text": render_questions_text(questions)})
        messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
        try:
            res = self._chat(messages, answers_schema([q.id for q in questions]), temperature=0.2)
        except ProviderError as exc:
            return [ProbeAnswer(question_id=q.id, trial=seed, chosen_option_id=None, error=str(exc)[:200]) for q in questions]
        return parse_answers(res.data, questions, trial=seed, latency_ms=res.latency_ms, raw=res.raw_text)

    def judge_json(self, media: ProbeMedia, instruction: str, schema: dict[str, Any]) -> CompletionResult:
        content = self._media_content(media)
        content.append({"type": "text", "text": instruction})
        messages = [{"role": "system", "content": VIEWER_SYSTEM_PROMPT}, {"role": "user", "content": content}]
        return self._chat(messages, schema, temperature=0.1)

    def complete_json(self, system: str, user: str, schema: dict[str, Any], *, temperature: float = 0.2) -> CompletionResult:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        return self._chat(messages, schema, temperature)


def wandb_inference_provider(api_key: str, model: str, project_path: str, role: str, vision: bool) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        name="wandb_inference",
        api_key=api_key,
        model=model,
        base_url=WANDB_INFERENCE_BASE_URL,
        project=project_path,
        role=role,
        vision=vision,
        docs_ref="https://docs.wandb.ai/inference/models",
    )


def openai_provider(api_key: str, model: str, role: str, reasoning_effort: str | None = None) -> OpenAICompatProvider:
    if reasoning_effort is None:
        reasoning_effort = "low" if role == "probe" else "high"
    return OpenAICompatProvider(
        name="openai", api_key=api_key, model=model, role=role, vision=True,
        docs_ref="https://platform.openai.com/docs/models", reasoning_effort=reasoning_effort,
        supports_temperature=False if model.startswith(("gpt-5", "o")) else None,
    )


def typesafe_provider(api_key: str, base_url: str, model: str, role: str) -> OpenAICompatProvider:
    """Hypothesis adapter: assumes an OpenAI-compatible chat endpoint. Disabled until a smoke test passes."""
    return OpenAICompatProvider(
        name="typesafe",
        api_key=api_key,
        model=model,
        base_url=base_url,
        role=role,
        vision=False,
        docs_ref="https://typesafe.ai/ (event API contract unverified)",
    )
