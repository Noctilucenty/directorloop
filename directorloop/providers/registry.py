"""Capability registry: which providers exist, what they verifiably do, and smoke tests.

States: unknown -> verified_supported | verified_unsupported. Nothing is assumed from
documentation alone; `smoke_test_all` records real results with timestamps.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Settings, get_settings
from ..domain.ids import utc_now_iso
from .base import (
    CompletionResult,
    MediaProbeProvider,
    ProbeMedia,
    ProviderCapability,
    ProviderError,
    TextPlannerProvider,
)


@dataclass
class ProviderBundle:
    probe: MediaProbeProvider | None
    planner: TextPlannerProvider | None
    capabilities: list[ProviderCapability] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    generation_available: bool = False

    def capability_dicts(self) -> list[dict[str, Any]]:
        return [c.to_dict() for c in self.capabilities]


def _build_probe(settings: Settings, notes: list[str]) -> MediaProbeProvider | None:
    from .gemini import GeminiProvider
    from .openai_compat import openai_provider, wandb_inference_provider

    order = [settings.dl_probe_provider, "gemini", "openai", "wandb_inference"]
    seen: set[str] = set()
    for name in order:
        if name in seen:
            continue
        seen.add(name)
        try:
            if name == "gemini" and settings.gemini_api_key:
                return GeminiProvider(settings.gemini_api_key, settings.dl_probe_model if settings.dl_probe_provider == "gemini" else "gemini-3.8-flash", role="probe")
            if name == "openai" and settings.openai_api_key:
                model = settings.dl_probe_model if settings.dl_probe_provider == "openai" else "gpt-5.5"
                return openai_provider(settings.openai_api_key, model, role="probe")
            if name == "wandb_inference" and settings.wandb_api_key:
                return wandb_inference_provider(
                    settings.wandb_api_key, settings.dl_wandb_inference_probe_model, settings.weave_project_path(), role="probe", vision=True
                )
        except ProviderError as exc:
            notes.append(f"probe provider {name}: {exc}")
    notes.append("no probe provider configured (GEMINI_API_KEY, OPENAI_API_KEY or WANDB_API_KEY)")
    return None


def _build_planner(settings: Settings, notes: list[str]) -> TextPlannerProvider | None:
    from .gemini import GeminiProvider
    from .openai_compat import openai_provider, typesafe_provider, wandb_inference_provider

    order = [settings.dl_planner_provider, "gemini", "openai", "wandb_inference"]
    seen: set[str] = set()
    for name in order:
        if name in seen or name == "rules":
            continue
        seen.add(name)
        try:
            if name == "gemini" and settings.gemini_api_key:
                model = settings.dl_planner_model if settings.dl_planner_provider == "gemini" else "gemini-3.1-pro-preview"
                return GeminiProvider(settings.gemini_api_key, model, role="planner", thinking="high", timeout_s=90)
            if name == "openai" and settings.openai_api_key:
                model = settings.dl_planner_model if settings.dl_planner_provider == "openai" else "gpt-5.5"
                return openai_provider(settings.openai_api_key, model, role="planner")
            if name == "wandb_inference" and settings.wandb_api_key:
                return wandb_inference_provider(
                    settings.wandb_api_key, settings.dl_wandb_inference_planner_model, settings.weave_project_path(), role="planner", vision=False
                )
            if name == "typesafe" and settings.typesafe_api_key and settings.typesafe_base_url:
                return typesafe_provider(settings.typesafe_api_key, settings.typesafe_base_url, settings.typesafe_model, role="planner")
        except ProviderError as exc:
            notes.append(f"planner provider {name}: {exc}")
    notes.append("no LLM planner configured; the deterministic rules planner will be used")
    return None


def build_providers(settings: Settings | None = None) -> ProviderBundle:
    settings = settings or get_settings()
    notes: list[str] = []
    probe = _build_probe(settings, notes)
    planner = _build_planner(settings, notes)
    caps: list[ProviderCapability] = []
    if probe is not None:
        caps.append(probe.capability)
    if planner is not None and (probe is None or planner.capability is not probe.capability):
        caps.append(planner.capability)
    # Registry entries for providers that are configured but not selected, or not configured at all.
    known = {
        "gemini": bool(settings.gemini_api_key),
        "openai": bool(settings.openai_api_key),
        "wandb_inference": bool(settings.wandb_api_key),
        "typesafe": bool(settings.typesafe_api_key and settings.typesafe_base_url),
        "elevenlabs": bool(settings.elevenlabs_api_key),
    }
    listed = {c.name for c in caps}
    for name, present in known.items():
        if name not in listed:
            caps.append(
                ProviderCapability(
                    name=name,
                    role="unassigned",
                    model="",
                    modalities=set(),
                    present=present,
                    state="unknown",
                    smoke_result=None if present else "not configured",
                )
            )
    return ProviderBundle(probe=probe, planner=planner, capabilities=caps, notes=notes)


SMOKE_SCHEMA = {
    "type": "object",
    "properties": {"dominant_color": {"type": "string"}, "sees_motion": {"type": "boolean"}},
    "required": ["dominant_color", "sees_motion"],
}


def smoke_test_probe(provider: MediaProbeProvider, media: ProbeMedia) -> ProviderCapability:
    cap = provider.capability
    try:
        res: CompletionResult = provider.judge_json(
            media,
            "Reply with JSON: the dominant background color of this video and whether anything moves.",
            SMOKE_SCHEMA,
        )
        cap.state = "verified_supported"
        cap.smoke_result = f"ok in {res.latency_ms} ms: {json.dumps(res.data)[:120]}"
    except ProviderError as exc:
        cap.state = "verified_unsupported"
        cap.smoke_result = f"failed: {str(exc)[:200]}"
    cap.checked_at = utc_now_iso()
    return cap


def smoke_test_planner(provider: TextPlannerProvider) -> ProviderCapability:
    cap = provider.capability
    schema = {"type": "object", "properties": {"answer": {"type": "integer"}}, "required": ["answer"]}
    try:
        res = provider.complete_json("Answer with JSON only.", "What is 6 times 7? Return {\"answer\": <int>}", schema)
        ok = res.data.get("answer") == 42
        cap.state = "verified_supported" if ok else "verified_unsupported"
        cap.smoke_result = f"{'ok' if ok else 'wrong answer'} in {res.latency_ms} ms"
    except ProviderError as exc:
        cap.state = "verified_unsupported"
        cap.smoke_result = f"failed: {str(exc)[:200]}"
    cap.checked_at = utc_now_iso()
    return cap


def save_capability_report(bundle: ProviderBundle, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"checked_at": utc_now_iso(), "providers": bundle.capability_dicts(), "notes": bundle.notes}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
