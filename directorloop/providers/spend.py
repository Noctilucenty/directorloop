"""Durable pre-dispatch reservations for priced, explicitly capped chat requests.

This is a conservative application budget, not a provider invoice limit. Only
requests sent through this guard are covered. Failed/ambiguous requests retain
their entire reservation; restarting a worker never refunds them. We reserve
the model's documented full context for input, avoiding heuristic image-token
estimates. A successful reply with valid usage releases unused reservation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any

from .base import ProviderError

USD_UNITS = 1_000_000_000  # exact integer nano-USD; never compare binary floats


class SpendGuardError(ProviderError):
    """No request may be sent when the cost policy cannot be established."""


class SpendLimitExceeded(SpendGuardError):
    code = "spend_limit_exceeded"


@dataclass(frozen=True)
class PriceCard:
    endpoint: str
    model: str
    input_usd_per_million: str
    output_usd_per_million: str
    max_input_tokens: int
    max_output_tokens: int
    output_parameter: str
    source: str
    verified_on: str
    expires_on: str
    long_context_threshold: int | None = None
    long_input_multiplier: str = "1"
    long_output_multiplier: str = "1"
    cache_write_multiplier: str = "1"
    service_tier: str | None = None
    pricing_source: str | None = None

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()

    def check(self, today: date | None = None) -> None:
        today = today or datetime.now(UTC).date()
        if not date.fromisoformat(self.verified_on) <= today <= date.fromisoformat(self.expires_on):
            raise SpendGuardError("spending guard pricing verification is stale; no request sent")
        for value in (self.input_usd_per_million, self.output_usd_per_million,
                      self.long_input_multiplier, self.long_output_multiplier, self.cache_write_multiplier):
            price = Decimal(value)
            if not price.is_finite() or price < 0:
                raise SpendGuardError("spending guard pricing is invalid; no request sent")
        if self.max_input_tokens <= 0 or self.max_output_tokens <= 0 or self.output_parameter not in {"max_tokens", "max_completion_tokens"}:
            raise SpendGuardError("spending guard token limits are invalid; no request sent")
        if any(Decimal(v) < 1 for v in (self.long_input_multiplier, self.long_output_multiplier, self.cache_write_multiplier)):
            raise SpendGuardError("spending guard premium multipliers are invalid; no request sent")

    def cost_units(self, input_tokens: int, output_tokens: int) -> int:
        # Treat all input as cache writes when their tariff is higher. A reply's
        # aggregate usage cannot reliably establish which tokens were written.
        input_rate = Decimal(self.input_usd_per_million) * Decimal(self.cache_write_multiplier)
        output_rate = Decimal(self.output_usd_per_million)
        if self.long_context_threshold is not None and input_tokens > self.long_context_threshold:
            input_rate *= Decimal(self.long_input_multiplier)
            output_rate *= Decimal(self.long_output_multiplier)
        total = Decimal(input_tokens) * input_rate + Decimal(output_tokens) * output_rate
        return int((total * Decimal(USD_UNITS) / Decimal(1_000_000)).to_integral_value(rounding=ROUND_CEILING))


# Only primary-source verified endpoint/model pairs belong here. Never infer
# hosted pricing from the upstream open-weight model's name.
PRICE_CARDS: tuple[PriceCard, ...] = (
    PriceCard(
        endpoint="https://api.inference.wandb.ai/v1",
        model="Qwen/Qwen3.8-27B",
        input_usd_per_million="0.40", output_usd_per_million="3.00",
        max_input_tokens=262_144,
        max_output_tokens=2048,  # deliberately small application ceiling, not a model capacity claim
        output_parameter="max_tokens",
        source="https://wandb.ai/site/inference-model/cw_qwen_qwen3.8-27b/",
        verified_on="2026-09-12", expires_on="2026-10-12",
    ),
    PriceCard(
        endpoint="https://api.openai.com/v1", model="gpt-5.6-terra",
        input_usd_per_million="2", output_usd_per_million="12",
        max_input_tokens=1_050_000, max_output_tokens=128_000,
        output_parameter="max_completion_tokens",
        source="https://developers.openai.com/api/docs/models/gpt-5.6-terra",
        verified_on="2026-09-13", expires_on="2026-10-13",
        long_context_threshold=272_000, long_input_multiplier="2", long_output_multiplier="1.5",
        cache_write_multiplier="1.25", service_tier="default",
        pricing_source="https://developers.openai.com/api/docs/pricing",
    ),
    PriceCard(
        endpoint="https://api.openai.com/v1", model="gpt-5.6-sol",
        input_usd_per_million="4", output_usd_per_million="20",
        max_input_tokens=1_050_000, max_output_tokens=128_000,
        output_parameter="max_completion_tokens",
        source="https://developers.openai.com/api/docs/models/gpt-5.6-sol",
        verified_on="2026-09-13", expires_on="2026-10-13",
        long_context_threshold=272_000, long_input_multiplier="2", long_output_multiplier="1.5",
        cache_write_multiplier="1.25", service_tier="default",
        pricing_source="https://developers.openai.com/api/docs/pricing",
    ),
)


def price_card(endpoint: str, model: str) -> PriceCard:
    for card in PRICE_CARDS:
        if card.endpoint == endpoint.rstrip("/") and card.model == model:
            card.check()
            return card
    raise SpendGuardError("spending guard has no verified price for this endpoint/model; no request sent")


def _money_units(value: str | float | Decimal) -> int:
    amount = Decimal(str(value))
    if not amount.is_finite() or amount <= 0 or amount * USD_UNITS != (amount * USD_UNITS).to_integral_value():
        raise SpendGuardError("spending limit must be a positive amount with at most nine decimal places")
    units = int(amount * USD_UNITS)
    if units > 9_000_000_000_000_000:
        raise SpendGuardError("spending limit is outside the supported range")
    return units


class SpendLedger:
    """One budget ID is shared by all calls/threads/processes using this file.

    Existing IDs have immutable ceilings. Raising a limit requires an explicit
    new ID; no startup, failure handler, or retry resets previous reservations.
    """

    def __init__(self, path: str | Path, budget_id: str, limit_usd: str | float | Decimal,
                 *, max_attempts: int | None = None, run_limit_usd: str | float | Decimal | None = None,
                 max_run_attempts: int | None = None) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", budget_id):
            raise SpendGuardError("spending budget ID must be an explicit short identifier")
        self.path = Path(path)
        self.budget_id = budget_id
        self.limit_units = _money_units(limit_usd)
        self.run_limit_units = _money_units(run_limit_usd) if run_limit_usd is not None else None
        if self.run_limit_units is not None and self.run_limit_units > self.limit_units:
            raise SpendGuardError("run spending limit cannot exceed the shared spending limit")
        if max_run_attempts is not None and (self.run_limit_units is None or type(max_run_attempts) is not int or max_run_attempts <= 0):
            raise SpendGuardError("run attempt limit needs a run spending limit and a positive integer")
        if max_attempts is not None and (isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts <= 0):
            raise SpendGuardError("spending attempt limit must be a positive integer")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS spend_budgets (
                    budget_id TEXT PRIMARY KEY, limit_units INTEGER NOT NULL,
                    halted INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS spend_attempts (
                    attempt_id TEXT PRIMARY KEY, budget_id TEXT NOT NULL,
                    endpoint TEXT NOT NULL, model TEXT NOT NULL, price_fingerprint TEXT NOT NULL,
                    price_json TEXT NOT NULL, input_bound INTEGER NOT NULL, output_bound INTEGER NOT NULL,
                    reserved_units INTEGER NOT NULL, held_units INTEGER NOT NULL,
                    status TEXT NOT NULL, input_tokens INTEGER, output_tokens INTEGER,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS spend_attempts_budget ON spend_attempts(budget_id);
                CREATE TABLE IF NOT EXISTS spend_run_policies (
                    budget_id TEXT PRIMARY KEY, limit_units INTEGER NOT NULL, max_attempts INTEGER
                );
                CREATE TABLE IF NOT EXISTS spend_attempt_runs (
                    attempt_id TEXT PRIMARY KEY, budget_id TEXT NOT NULL, run_id TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS spend_attempt_runs_scope ON spend_attempt_runs(budget_id, run_id);
            """)
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO spend_budgets (budget_id, limit_units, max_attempts, created_at) VALUES (?, ?, ?, ?)",
                       (budget_id, self.limit_units, max_attempts, datetime.now(UTC).isoformat()))
            saved = db.execute("SELECT limit_units, max_attempts FROM spend_budgets WHERE budget_id = ?", (budget_id,)).fetchone()
            if saved[0] != self.limit_units or saved[1] != max_attempts:
                raise SpendGuardError("existing spending budget has a different fixed limit; no request sent")
            policy = db.execute("SELECT limit_units, max_attempts FROM spend_run_policies WHERE budget_id = ?", (budget_id,)).fetchone()
            expected_policy = None if self.run_limit_units is None else (self.run_limit_units, max_run_attempts)
            if policy is None and expected_policy is not None:
                if db.execute("SELECT 1 FROM spend_attempts WHERE budget_id = ? LIMIT 1", (budget_id,)).fetchone():
                    raise SpendGuardError("existing unscoped spending budget cannot acquire a run policy; no request sent")
                db.execute("INSERT INTO spend_run_policies VALUES (?, ?, ?)", (budget_id, *expected_policy))
            elif (tuple(policy) if policy else None) != expected_policy:
                raise SpendGuardError("existing spending budget has a different fixed run policy; no request sent")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = None
        try:
            # Only __init__ creates a ledger. A missing/unreadable file during
            # admission or settlement must not become a fresh empty budget.
            db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=rw", uri=True, timeout=30)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout = 30000")
            with db:
                yield db
        except sqlite3.Error as exc:
            raise SpendGuardError("spending ledger unavailable; new requests blocked") from exc
        finally:
            if db is not None:
                db.close()

    def reserve(self, card: PriceCard, output_tokens: int, *, run_id: str | None = None) -> str:
        card.check()
        if isinstance(output_tokens, bool) or not isinstance(output_tokens, int) or not 1 <= output_tokens <= card.max_output_tokens:
            raise SpendGuardError("spending guard output cap is outside verified model limits; no request sent")
        amount = card.cost_units(card.max_input_tokens, output_tokens)
        attempt_id = uuid.uuid4().hex
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            budget = db.execute("SELECT * FROM spend_budgets WHERE budget_id = ?", (self.budget_id,)).fetchone()
            usage = db.execute("SELECT COALESCE(SUM(held_units), 0), COUNT(*) FROM spend_attempts WHERE budget_id = ?", (self.budget_id,)).fetchone()
            if budget["halted"] or usage[0] + amount > budget["limit_units"] or (budget["max_attempts"] is not None and usage[1] >= budget["max_attempts"]):
                raise SpendLimitExceeded("spending reservation limit reached; no request sent")
            policy = db.execute("SELECT * FROM spend_run_policies WHERE budget_id = ?", (self.budget_id,)).fetchone()
            if policy is not None:
                if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", run_id):
                    raise SpendGuardError("spending guard requires an explicit run ID; no request sent")
                run_usage = db.execute("""SELECT COALESCE(SUM(a.held_units), 0), COUNT(*)
                    FROM spend_attempts a JOIN spend_attempt_runs r ON a.attempt_id = r.attempt_id
                    WHERE r.budget_id = ? AND r.run_id = ?""", (self.budget_id, run_id)).fetchone()
                if run_usage[0] + amount > policy["limit_units"] or (policy["max_attempts"] is not None and run_usage[1] >= policy["max_attempts"]):
                    raise SpendLimitExceeded("spending reservation limit reached; no request sent")
            elif run_id is not None:
                raise SpendGuardError("spending budget has no fixed run policy; no request sent")
            db.execute("""INSERT INTO spend_attempts
                (attempt_id, budget_id, endpoint, model, price_fingerprint, price_json,
                 input_bound, output_bound, reserved_units, held_units, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?)""",
                       (attempt_id, self.budget_id, card.endpoint, card.model, card.fingerprint,
                        json.dumps(asdict(card), sort_keys=True), card.max_input_tokens, output_tokens,
                        amount, amount, datetime.now(UTC).isoformat()))
            if policy is not None:
                db.execute("INSERT INTO spend_attempt_runs VALUES (?, ?, ?)", (attempt_id, self.budget_id, run_id))
        return attempt_id

    def settle(self, attempt_id: str, input_tokens: Any, output_tokens: Any, *, halt: bool = False) -> None:
        """Missing/invalid usage is unknown, never zero; settlement is idempotent."""
        valid = all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in (input_tokens, output_tokens))
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM spend_attempts WHERE attempt_id = ? AND budget_id = ?", (attempt_id, self.budget_id)).fetchone()
            if row is None:
                raise SpendGuardError("spending reservation was not found")
            if row["status"] != "reserved":
                return
            if halt:
                db.execute("UPDATE spend_budgets SET halted = 1 WHERE budget_id = ?", (self.budget_id,))
            if not valid:
                db.execute("UPDATE spend_attempts SET status = 'unknown' WHERE attempt_id = ?", (attempt_id,))
                return
            card = PriceCard(**json.loads(row["price_json"]))
            actual = card.cost_units(input_tokens, output_tokens)
            violation = input_tokens > row["input_bound"] or output_tokens > row["output_bound"]
            db.execute("""UPDATE spend_attempts SET held_units = ?, status = ?, input_tokens = ?, output_tokens = ?
                          WHERE attempt_id = ?""",
                       (max(actual, row["reserved_units"]) if violation else actual,
                        "bound_violated" if violation else "settled", input_tokens, output_tokens, attempt_id))
            if violation:
                db.execute("UPDATE spend_budgets SET halted = 1 WHERE budget_id = ?", (self.budget_id,))

    def unknown(self, attempt_id: str, *, halt: bool = False) -> None:
        self.settle(attempt_id, None, None, halt=halt)

    def run_policy(self) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM spend_run_policies WHERE budget_id = ?", (self.budget_id,)).fetchone()
        return None if row is None else {"limit_usd": row["limit_units"] / USD_UNITS, "max_physical_attempts": row["max_attempts"]}

    def run_summary(self, run_id: str) -> dict[str, Any]:
        policy = self.run_policy()
        if policy is None:
            raise SpendGuardError("spending budget has no fixed run policy")
        with self._connect() as db:
            rows = db.execute("""SELECT a.status, COUNT(*) AS n, SUM(a.held_units) AS held
                FROM spend_attempts a JOIN spend_attempt_runs r ON a.attempt_id = r.attempt_id
                WHERE r.budget_id = ? AND r.run_id = ? GROUP BY a.status""", (self.budget_id, run_id)).fetchall()
        held = sum(r["held"] for r in rows)
        return {"run_id": run_id, **policy, "held_usd": held / USD_UNITS,
                "available_usd": max(0, _money_units(policy["limit_usd"]) - held) / USD_UNITS,
                "physical_attempts": sum(r["n"] for r in rows),
                "attempt_status_counts": {r["status"]: r["n"] for r in rows}}

    def summary(self) -> dict[str, Any]:
        with self._connect() as db:
            budget = db.execute("SELECT * FROM spend_budgets WHERE budget_id = ?", (self.budget_id,)).fetchone()
            rows = db.execute("SELECT status, COUNT(*) AS n, SUM(held_units) AS held FROM spend_attempts WHERE budget_id = ? GROUP BY status", (self.budget_id,)).fetchall()
        total = sum(row["held"] for row in rows)
        settled = sum(row["held"] for row in rows if row["status"] == "settled")
        return {"budget_id": self.budget_id, "limit_usd": self.limit_units / USD_UNITS,
                "held_usd": total / USD_UNITS, "usage_estimate_usd": settled / USD_UNITS,
                "unsettled_reserved_usd": (total - settled) / USD_UNITS,
                "available_usd": max(0, budget["limit_units"] - total) / USD_UNITS,
                "physical_attempts": sum(row["n"] for row in rows),
                "max_physical_attempts": budget["max_attempts"],
                "attempt_status_counts": {row["status"]: row["n"] for row in rows},
                "halted": bool(budget["halted"]),
                "scope": "application requests sharing this ledger and budget ID; not a provider invoice cap"}


@dataclass(frozen=True)
class SpendGuard:
    ledger: SpendLedger
    max_output_tokens: int
    run_id: str | None = None

    def for_run(self, run_id: str) -> SpendGuard:
        return replace(self, run_id=run_id)

    def prepare(self, endpoint: str, model: str, kwargs: dict[str, Any]) -> tuple[str, PriceCard]:
        card = price_card(endpoint, model)
        allowed = {"model", "messages", "stream", "tools", "max_tokens", "max_completion_tokens", "n",
                   "response_format", "temperature", "reasoning_effort", "extra_body", "service_tier"}
        if set(kwargs) - allowed or kwargs.get("n", 1) != 1 or ("model" in kwargs and kwargs["model"] != model):
            raise SpendGuardError("spending guard only supports one capped completion; no request sent")
        # Guard controls the cap and disallows streaming/tool billable operations.
        if kwargs.get("stream") or kwargs.get("tools"):
            raise SpendGuardError("spending guard only supports non-streaming chat without tools; no request sent")
        if "extra_body" in kwargs:
            body = kwargs["extra_body"]
            template = body.get("chat_template_kwargs") if isinstance(body, dict) else None
            if (endpoint.rstrip("/") != "https://api.inference.wandb.ai/v1" or model != "Qwen/Qwen3.8-27B"
                    or not isinstance(body, dict) or set(body) != {"chat_template_kwargs"}
                    or not isinstance(template, dict) or set(template) != {"enable_thinking"}
                    or type(template["enable_thinking"]) is not bool):
                raise SpendGuardError("spending guard does not allow unverified request-body overrides; no request sent")
        if card.service_tier is not None:
            if kwargs.get("service_tier", card.service_tier) != card.service_tier:
                raise SpendGuardError("spending guard has no verified price for this service tier; no request sent")
            kwargs["service_tier"] = card.service_tier
            kwargs["n"] = 1
        elif "service_tier" in kwargs:
            raise SpendGuardError("spending guard has no verified price for this service tier; no request sent")
        for message in kwargs.get("messages", []):
            if not isinstance(message, dict) or set(message) - {"role", "content"}:
                raise SpendGuardError("spending guard only supports text and image input; no request sent")
            content = message.get("content")
            if not isinstance(content, (str, list)):
                raise SpendGuardError("spending guard only supports text and image input; no request sent")
            if isinstance(content, list) and any(not isinstance(part, dict) or part.get("type") not in {"text", "image_url"} for part in content):
                raise SpendGuardError("spending guard only supports text and image input; no request sent")
        kwargs.pop("max_tokens" if card.output_parameter == "max_completion_tokens" else "max_completion_tokens", None)
        kwargs[card.output_parameter] = self.max_output_tokens
        attempt_id = self.ledger.reserve(card, self.max_output_tokens, run_id=self.run_id)
        return attempt_id, card
