"""Capability registry: which providers exist, what they verifiably do, and smoke tests.

States: unknown -> verified_supported | verified_unsupported. Nothing is assumed from
documentation alone; `smoke_test_all` records real results with timestamps.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
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
from .spend import SpendGuard, SpendGuardError, SpendLedger, price_card


@dataclass
class ProviderBundle:
    probe: MediaProbeProvider | None
    planner: TextPlannerProvider | None
    capabilities: list[ProviderCapability] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    generation_available: bool = False
    spend_guard: SpendGuard | None = None
    spend_problem: str | None = None

    def capability_dicts(self) -> list[dict[str, Any]]:
        return [c.to_dict() for c in self.capabilities]

    def for_run(self, run_id: str) -> ProviderBundle:
        if self.spend_guard is None:
            return self
        guard = self.spend_guard.for_run(run_id)
        return replace(self, probe=self.probe.with_spend_guard(guard) if self.probe is not None else None,
                       planner=self.planner.with_spend_guard(guard) if self.planner is not None else None,
                       spend_guard=guard)

    def spend_status(self) -> dict[str, Any]:
        if self.spend_guard is None:
            return {"enabled": False, "available": True, "reason": "Full-review spending guard is disabled.", "budget": None}
        guard = self.spend_guard
        try:
            budget, policy = guard.ledger.summary(), guard.ledger.run_policy()
        except SpendGuardError:
            return {"enabled": True, "available": False, "reason": "Full-review spending ledger is unavailable; new requests blocked.", "budget": None}
        prices, reason = [], self.spend_problem
        if self.probe is None:
            reason = "No supported guarded review provider is configured."
        for provider in (self.probe, self.planner):
            if provider is None:
                continue
            try:
                card = price_card(provider._endpoint, provider.model)
                card.check()
                if not 1 <= guard.max_output_tokens <= card.max_output_tokens:
                    raise SpendGuardError("configured output cap is outside verified limits")
                amount = card.cost_units(card.max_input_tokens, guard.max_output_tokens) / 1_000_000_000
                prices.append({"model": card.model, "role": provider.capability.role, "source": card.source,
                               "pricing_source": card.pricing_source or card.source, "verified_on": card.verified_on,
                               "expires_on": card.expires_on, "request_reservation_usd": amount,
                               "input_bound": card.max_input_tokens, "output_cap": guard.max_output_tokens})
                if amount > budget["available_usd"] or policy is None or amount > policy["limit_usd"]:
                    reason = "Full-review budget is below the conservative request reservation; no request will be sent."
            except SpendGuardError:
                reason = "Full-review pricing is unavailable or stale; no request will be sent."
        if budget["halted"] or (budget["max_physical_attempts"] is not None and budget["physical_attempts"] >= budget["max_physical_attempts"]):
            reason = "Full-review spending limit reached; no request will be sent."
        return {"enabled": True, "available": reason is None, "reason": reason, "budget": budget,
                "per_run": policy, "prices": prices,
                "estimate_note": "Conservative token-cost estimates include possible cache-write tariffs; not reconciled invoices."}


def _build_probe(settings: Settings, notes: list[str], spend_guard: SpendGuard | None = None) -> MediaProbeProvider | None:
    from .gemini import GeminiProvider
    from .openai_compat import openai_provider, wandb_inference_provider

    order = [settings.dl_probe_provider] if spend_guard else [settings.dl_probe_provider, "gemini", "openai", "wandb_inference"]
    if spend_guard and settings.dl_probe_provider not in {"openai", "wandb_inference"}:
        notes.append("probe provider is unsupported by the spending guard; no fallback selected")
        return None
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
                return openai_provider(settings.openai_api_key, model, role="probe", spend_guard=spend_guard)
            if name == "wandb_inference" and settings.wandb_api_key:
                return wandb_inference_provider(
                    settings.wandb_api_key, settings.dl_wandb_inference_probe_model, settings.weave_project_path(), role="probe", vision=True,
                    spend_guard=spend_guard,
                )
        except ProviderError as exc:
            notes.append(f"probe provider {name}: {exc}")
    notes.append("no probe provider configured (GEMINI_API_KEY, OPENAI_API_KEY or WANDB_API_KEY)")
    return None


def _build_planner(settings: Settings, notes: list[str], spend_guard: SpendGuard | None = None) -> TextPlannerProvider | None:
    from .gemini import GeminiProvider
    from .openai_compat import openai_provider, typesafe_provider, wandb_inference_provider

    order = [settings.dl_planner_provider] if spend_guard else [settings.dl_planner_provider, "gemini", "openai", "wandb_inference"]
    if spend_guard and settings.dl_planner_provider not in {"openai", "wandb_inference", "rules"}:
        notes.append("planner provider is unsupported by the spending guard; no fallback selected")
        return None
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
                return openai_provider(settings.openai_api_key, model, role="planner", spend_guard=spend_guard)
            if name == "wandb_inference" and settings.wandb_api_key:
                return wandb_inference_provider(
                    settings.wandb_api_key, settings.dl_wandb_inference_planner_model, settings.weave_project_path(), role="planner", vision=False,
                    spend_guard=spend_guard,
                )
            if name == "typesafe" and settings.typesafe_api_key and settings.typesafe_base_url:
                return typesafe_provider(settings.typesafe_api_key, settings.typesafe_base_url, settings.typesafe_model, role="planner")
        except ProviderError as exc:
            notes.append(f"planner provider {name}: {exc}")
    notes.append("no LLM planner configured; the deterministic rules planner will be used")
    return None


def build_providers(settings: Settings | None = None) -> ProviderBundle:
    settings = settings or get_settings()
    spend_guard = None
    if settings.dl_spend_limit_usd is not None:
        ledger_path = Path(settings.dl_spend_ledger_path) if settings.dl_spend_ledger_path else settings.data_dir / "provider-spend.sqlite3"
        spend_guard = SpendGuard(SpendLedger(ledger_path, settings.dl_spend_budget_id, settings.dl_spend_limit_usd,
                                max_attempts=settings.dl_spend_max_physical_attempts,
                                run_limit_usd=settings.dl_spend_per_run_limit_usd or settings.dl_spend_limit_usd,
                                max_run_attempts=settings.dl_spend_max_run_attempts), settings.dl_max_output_tokens)
    notes: list[str] = []
    probe = _build_probe(settings, notes, spend_guard)
    planner = _build_planner(settings, notes, spend_guard)
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
    problem = None
    if spend_guard and (probe is None or (planner is None and settings.dl_planner_provider != "rules")):
        problem = "A configured provider is unavailable to the spending guard; no fallback selected."
    return ProviderBundle(probe=probe, planner=planner, capabilities=caps, notes=notes, spend_guard=spend_guard, spend_problem=problem)


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
