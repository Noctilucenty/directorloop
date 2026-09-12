"""Scoped creative policy: evidence about which mutation types help, by scope.

A strategy is a mutation type within a scope (global, ugc, category, brand, account, audience).
Evidence arrives from reference patterns (a prior), our own offline experiments (wins, losses,
neutral, rejected), blinded human pairwise tests, and later real platform results. Nothing is
hand-written as a rule: statuses and ranking weights follow the recorded counts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..domain.creative import HypothesisFamily, MutationType
from ..domain.ids import utc_now_iso

SCOPE_LEVELS = ["global", "ugc", "category", "brand", "account", "audience"]


class StrategyEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experiment_id: str
    video_id: str
    arm_id: str
    outcome: str  # win | loss | neutral | rejected
    evidence_class: str  # model_eval | human_test | real
    primary_metric: str
    control_value: float | None = None
    arm_value: float | None = None
    n: int | None = None
    note: str = ""
    weave_url: str | None = None
    created_at: str = Field(default_factory=utc_now_iso)


class CreativeStrategy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    mutation_type: MutationType
    hypothesis_family: HypothesisFamily | None = None
    scope_level: str = "category"
    scope_value: str = "educational_short"
    trigger: str = ""
    intervention: str = ""
    exceptions: list[str] = Field(default_factory=list)
    reference_prior: dict[str, Any] | None = None  # pattern id, top/bottom counts, lift
    wins: int = 0
    losses: int = 0
    neutral: int = 0
    rejected: int = 0
    human_prefer: int = 0
    human_total: int = 0
    real_world: dict[str, Any] | None = None
    evidence: list[StrategyEvidence] = Field(default_factory=list)
    confidence: float = 0.5
    status: str = "PROPOSED"
    updated_at: str = Field(default_factory=utc_now_iso)

    @property
    def experiments(self) -> int:
        return self.wins + self.losses + self.neutral + self.rejected

    def evidence_mean(self) -> float:
        """Smoothed success rate from our own experiments. 0.5 when there is no evidence."""
        a = 1.0 + self.wins + 0.5 * self.neutral
        b = 1.0 + self.losses + self.rejected + 0.5 * self.neutral
        return a / (a + b)

    def reference_score(self) -> float:
        """0.5 without a prior; shifted by at most 0.125 by the reference lift (weak prior by design)."""
        if not self.reference_prior or self.reference_prior.get("lift") is None:
            return 0.5
        lift = max(-1.0, min(1.0, float(self.reference_prior["lift"])))
        return 0.5 + 0.125 * lift

    def recompute(self) -> None:
        if self.experiments == 0 and self.human_total == 0:
            self.status = "REFERENCE_PRIOR" if self.reference_prior else "PROPOSED"
        elif self.losses + self.rejected >= 2 and self.wins == 0:
            self.status = "REJECTED"  # repeated failures and not a single win
        elif self.losses + self.rejected > self.wins:
            self.status = "CONTRADICTED"  # mixed evidence leaning against it
        elif self.wins > self.losses + self.rejected:
            self.status = "SUPPORTED_OFFLINE"
        else:
            self.status = "PROPOSED"
        if (
            self.status == "SUPPORTED_OFFLINE"
            and self.human_total >= 10
            and self.human_prefer / self.human_total >= 0.6
        ):
            self.status = "HUMAN_SUPPORTED"
        if self.real_world and self.real_world.get("supported"):
            self.status = "REAL_WORLD_SUPPORTED"
        self.confidence = round(self.evidence_mean(), 3)
        self.updated_at = utc_now_iso()


class PolicyChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    experiment_id: str
    strategy_id: str
    before: dict[str, Any]
    after: dict[str, Any]
    created_at: str = Field(default_factory=utc_now_iso)


class CreativePolicyStore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 0
    strategies: list[CreativeStrategy] = Field(default_factory=list)
    changes: list[PolicyChange] = Field(default_factory=list)

    @staticmethod
    def strategy_id(mutation_type: MutationType, scope_level: str, scope_value: str) -> str:
        return f"strat_{mutation_type.value.lower()}__{scope_level}_{scope_value}"

    def get(self, mutation_type: MutationType, scope_value: str, scope_level: str = "category") -> CreativeStrategy | None:
        sid = self.strategy_id(mutation_type, scope_level, scope_value)
        return next((s for s in self.strategies if s.id == sid), None)

    def get_or_create(self, mutation_type: MutationType, scope_value: str, scope_level: str = "category", family: HypothesisFamily | None = None) -> CreativeStrategy:
        existing = self.get(mutation_type, scope_value, scope_level)
        if existing is not None:
            return existing
        strat = CreativeStrategy(
            id=self.strategy_id(mutation_type, scope_level, scope_value),
            mutation_type=mutation_type,
            hypothesis_family=family,
            scope_level=scope_level,
            scope_value=scope_value,
        )
        strat.recompute()
        self.strategies.append(strat)
        return strat

    def lookup(self, mutation_type: MutationType, category: str) -> CreativeStrategy | None:
        """Most specific scope first: category, then ugc, then global."""
        for level, value in (("category", category), ("ugc", "ugc"), ("global", "global")):
            s = self.get(mutation_type, value, level)
            if s is not None:
                return s
        return None

    def record_outcome(self, mutation_type: MutationType, category: str, evidence: StrategyEvidence, family: HypothesisFamily | None = None) -> CreativeStrategy:
        strat = self.get_or_create(mutation_type, category, "category", family)
        before = {"status": strat.status, "wins": strat.wins, "losses": strat.losses, "neutral": strat.neutral, "rejected": strat.rejected, "confidence": strat.confidence}
        if evidence.evidence_class == "human_test":
            strat.human_total += evidence.n or 0
            strat.human_prefer += int(round((evidence.arm_value or 0.0) * (evidence.n or 0)))
        elif evidence.outcome == "win":
            strat.wins += 1
        elif evidence.outcome == "loss":
            strat.losses += 1
        elif evidence.outcome == "rejected":
            strat.rejected += 1
        else:
            strat.neutral += 1
        strat.evidence.append(evidence)
        strat.recompute()
        self.version += 1
        after = {"status": strat.status, "wins": strat.wins, "losses": strat.losses, "neutral": strat.neutral, "rejected": strat.rejected, "confidence": strat.confidence}
        self.changes.append(PolicyChange(version=self.version, experiment_id=evidence.experiment_id, strategy_id=strat.id, before=before, after=after))
        return strat

    def summary(self) -> list[dict[str, Any]]:
        return [
            {
                "id": s.id,
                "mutation": s.mutation_type.value,
                "scope": f"{s.scope_level}:{s.scope_value}",
                "status": s.status,
                "wins": s.wins,
                "losses": s.losses,
                "neutral": s.neutral,
                "rejected": s.rejected,
                "human": f"{s.human_prefer}/{s.human_total}" if s.human_total else None,
                "reference_prior": s.reference_prior,
                "confidence": s.confidence,
            }
            for s in sorted(self.strategies, key=lambda s: (-s.wins, s.losses, s.id))
        ]

    def snapshot(self) -> CreativePolicyStore:
        return CreativePolicyStore.model_validate(self.model_dump(mode="json"))


def load_policy(path: Path) -> CreativePolicyStore:
    if not path.exists():
        return CreativePolicyStore()
    return CreativePolicyStore.model_validate(json.loads(path.read_text(encoding="utf-8")))


def save_policy(store: CreativePolicyStore, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(store.model_dump(mode="json"), indent=2), encoding="utf-8")
    tmp.replace(path)
