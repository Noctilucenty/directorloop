"""OpenAI-compatible chat adapter: OpenAI, W&B Inference, and TypeSafe (once its contract is known).

Frame-based probing only: the model receives timestamped JPEG frames plus the ASR
transcript of the rendered audio. It never receives the actual video file, so runs
through this adapter are labeled `frames_and_transcript`.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from typing import Any

from ..domain.evaluation import ProbeAnswer
from ..domain.truth import ProbeQuestionView
from ..observability.weave_ops import traced
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
from .spend import SpendGuard, SpendGuardError

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
        spend_guard: SpendGuard | None = None,
        max_output_tokens: int | None = None,
        allow_compatibility_fallback: bool = True,
        enable_thinking: bool | None = None,
    ) -> None:
        if not api_key:
            raise ProviderError(f"{name}: API key is not configured")
        endpoint = (base_url or "https://api.openai.com/v1").rstrip("/")
        if enable_thinking is not None and (type(enable_thinking) is not bool or endpoint != WANDB_INFERENCE_BASE_URL or model != "Qwen/Qwen3.8-27B"):
            raise ProviderError("thinking toggle is only verified for W&B Qwen/Qwen3.8-27B")
        import openai

        # Pin the actual SDK destination to the endpoint used by the price
        # allowlist; OPENAI_BASE_URL must not silently redirect a guarded call.
        kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout_s, "max_retries": 0, "base_url": endpoint}
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
        self.spend_guard = spend_guard
        self.max_output_tokens = max_output_tokens
        self.allow_compatibility_fallback = allow_compatibility_fallback
        self._endpoint = endpoint
        self.enable_thinking = enable_thinking

    def _request_policy(self) -> dict[str, Any]:
        """Public request settings recorded with the model span; no credentials."""
        policy = {"endpoint": self._endpoint, "enable_thinking": self.enable_thinking,
                "max_output_tokens": self.spend_guard.max_output_tokens if self.spend_guard else self.max_output_tokens,
                "sdk_retries": 0, "compatibility_fallback": self.allow_compatibility_fallback}
        if self.spend_guard is not None and self.spend_guard.run_id is not None:
            policy.update(spend_budget_id=self.spend_guard.ledger.budget_id, spend_run_id=self.spend_guard.run_id,
                          input_reservation="documented full context; no heuristic token estimate",
                          service_tier="default" if self._endpoint == "https://api.openai.com/v1" else None)
        return policy

    def with_spend_guard(self, guard: SpendGuard) -> OpenAICompatProvider:
        """Bind an immutable job scope without mutating another concurrent job."""
        from copy import copy

        provider = copy(self)
        provider.spend_guard = guard
        provider.allow_compatibility_fallback = False
        return provider

    def _chat(self, messages: list[dict[str, Any]], schema: dict[str, Any] | None, temperature: float) -> CompletionResult:
        attempts = list(dict.fromkeys([self._json_mode, "json_object", "none"])) if schema is not None else ["none"]
        last_exc: Exception | None = None
        send_temperature = self._supports_temperature is not False
        reasoning_effort = self.reasoning_effort
        # Bound explicit compatibility negotiation as well as disabling SDK
        # retries: each physical HTTP attempt receives its own reservation.
        for _physical_attempt in range(3 if self.allow_compatibility_fallback else 1):
            mode = attempts[0]
            kwargs: dict[str, Any] = {"model": self.model, "messages": messages}
            if self.enable_thinking is not None:
                # W&B documents this exact Qwen toggle; arbitrary extra_body
                # overrides are not accepted by this adapter or the spend guard.
                # https://docs.wandb.ai/inference/response-settings/reasoning
                kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": self.enable_thinking}}
            if self.max_output_tokens is not None:
                cap_parameter = "max_completion_tokens" if self._endpoint == "https://api.openai.com/v1" else "max_tokens"
                kwargs[cap_parameter] = self.max_output_tokens
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
            reservation = None
            card = None
            if self.spend_guard is not None:
                reservation, card = self.spend_guard.prepare(self._endpoint, self.model, kwargs)
            try:
                resp = self._client.chat.completions.create(**kwargs)
            except Exception as exc:  # noqa: BLE001
                if reservation is not None:
                    self.spend_guard.ledger.unknown(reservation)
                last_exc = exc
                msg = str(exc).lower()
                if not self.allow_compatibility_fallback or getattr(exc, "status_code", None) not in {400, 422}:
                    raise ProviderError(f"{self.name} request failed: {str(exc)[:300]}") from exc
                if "temperature" in msg and send_temperature:
                    send_temperature = False
                    self._supports_temperature = False
                    continue
                if "reasoning_effort" in msg and reasoning_effort:
                    reasoning_effort = None
                    self.reasoning_effort = None
                    continue
                if mode != "none" and ("response_format" in msg or "json_schema" in msg or "json_object" in msg):
                    self._json_mode = "json_object" if mode == "json_schema" else "none"
                    attempts.pop(0)
                    continue
                raise ProviderError(f"{self.name} request failed: {str(exc)[:300]}") from exc
            latency = int((time.monotonic() - t0) * 1000)
            text = (resp.choices[0].message.content or "") if resp.choices else ""
            usage = getattr(resp, "usage", None)
            if reservation is not None:
                if card.service_tier is not None and getattr(resp, "service_tier", None) not in {None, card.service_tier}:
                    self.spend_guard.ledger.unknown(reservation, halt=True)
                    raise SpendGuardError("provider returned an unpriced service tier; spending halted")
                self.spend_guard.ledger.settle(reservation, getattr(usage, "prompt_tokens", None),
                                              getattr(usage, "completion_tokens", None))
            if resp.choices and getattr(resp.choices[0], "finish_reason", None) == "length":
                raise ProviderError("model output token limit reached; response incomplete")
            data = extract_json(text) if schema is not None else {"text": text}
            return CompletionResult(
                data=data,
                latency_ms=latency,
                model=self.model,
                raw_text=text,
                input_tokens=getattr(usage, "prompt_tokens", None),
                output_tokens=getattr(usage, "completion_tokens", None),
            )
        raise ProviderError(f"{self.name} request failed: {str(last_exc)[:300]}") from last_exc

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
        return _vision_model_call(self.name, self.model, self.reasoning_effort, VIEWER_SYSTEM_PROMPT, media, instruction, schema,
                                  _send=lambda: self._chat(messages, schema, temperature=0.1), provider_policy=self._request_policy())

    def complete_json(self, system: str, user: str, schema: dict[str, Any], *, temperature: float = 0.2) -> CompletionResult:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        return _text_model_call(self.name, self.model, self.reasoning_effort, system, user, schema,
                                _send=lambda: self._chat(messages, schema, temperature), provider_policy=self._request_policy())


@traced("model_call.vision", kind="llm")
def _vision_model_call(provider_name: str, model: str, reasoning_effort: str | None, system_prompt: str, media: ProbeMedia, instruction: str,
                       schema: dict[str, Any], _send: Callable[[], CompletionResult], provider_policy: dict[str, Any] | None = None) -> CompletionResult:
    """One vision request, traced with the exact instruction, frame timestamps and transcript (image bytes are not stored)."""
    return _send()


@traced("model_call.text", kind="llm")
def _text_model_call(provider_name: str, model: str, reasoning_effort: str | None, system_prompt: str, user_prompt: str, schema: dict[str, Any],
                     _send: Callable[[], CompletionResult], provider_policy: dict[str, Any] | None = None) -> CompletionResult:
    """One text request, traced with the exact system and user prompts."""
    return _send()


def wandb_inference_provider(api_key: str, model: str, project_path: str, role: str, vision: bool,
                             *, spend_guard: SpendGuard | None = None) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        name="wandb_inference",
        api_key=api_key,
        model=model,
        base_url=WANDB_INFERENCE_BASE_URL,
        project=project_path,
        role=role,
        vision=vision,
        docs_ref="https://docs.wandb.ai/inference/models",
        spend_guard=spend_guard,
    )


def openai_provider(api_key: str, model: str, role: str, reasoning_effort: str | None = None,
                    *, spend_guard: SpendGuard | None = None) -> OpenAICompatProvider:
    if reasoning_effort is None:
        reasoning_effort = "low" if role == "probe" else "high"
    return OpenAICompatProvider(
        name="openai", api_key=api_key, model=model, role=role, vision=True,
        docs_ref="https://platform.openai.com/docs/models", reasoning_effort=reasoning_effort,
        supports_temperature=False if model.startswith(("gpt-5", "o")) else None,
        spend_guard=spend_guard,
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
