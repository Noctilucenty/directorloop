"""Transferable strategy memory (spec section 22) and router statistics (section 15)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .brief import GoalProfile, RepairAction
from .findings import FailureCategory


class PolicyStatus(StrEnum):
    PROPOSED = "proposed"
    SUPPORTED_ON_DEV = "supported_on_dev"
    VALIDATED_ON_OTHER_STORIES = "validated_on_other_stories"
    HUMAN_SUPPORTED = "human_supported"
    CONTRADICTED = "contradicted"
    REJECTED = "rejected"
    RETIRED = "retired"


class PolicyEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experiment_id: str
    project_id: str
    story_family: str
    split: str
    outcome: str  # promoted | rejected | no_gain | needs_review
    delta_questions: int = 0
    delta_score: float = 0.0
    regressions: int = 0
    note: str = ""


class PolicyRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    version: int = 1
    profile_scope: list[GoalProfile]
    trigger_category: FailureCategory
    trigger_condition: str  # detectable precondition in words
    recommended_action: RepairAction
    action_detail: str = ""
    contraindications: list[str] = Field(default_factory=list)
    supporting: list[PolicyEvidence] = Field(default_factory=list)
    counterexamples: list[PolicyEvidence] = Field(default_factory=list)
    status: PolicyStatus = PolicyStatus.PROPOSED
    confidence: float = 0.0
    created_at: str
    updated_at: str
    retired_reason: str | None = None
    probe_config: str = ""  # provider/model used when evidence was gathered

    @property
    def support_count(self) -> int:
        return len(self.supporting)

    @property
    def counter_count(self) -> int:
        return len(self.counterexamples)

    def story_families_supporting(self) -> set[str]:
        return {e.story_family for e in self.supporting}


class RouterStat(BaseModel):
    """Per profile/category/action outcome counts with conservative shrinkage."""

    model_config = ConfigDict(extra="forbid")

    profile: GoalProfile
    category: FailureCategory
    action: RepairAction
    attempts: int = 0
    successes: int = 0
    total_latency_ms: int = 0
    total_cost_usd: float = 0.0

    def shrunk_success_rate(self, prior: float = 0.5, prior_weight: float = 2.0) -> float:
        return (self.successes + prior * prior_weight) / (self.attempts + prior_weight)

    def mean_latency_ms(self) -> int | None:
        return int(self.total_latency_ms / self.attempts) if self.attempts else None


class PolicyStore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 0
    rules: list[PolicyRule] = Field(default_factory=list)
    router_stats: list[RouterStat] = Field(default_factory=list)

    def rule(self, rule_id: str) -> PolicyRule | None:
        for r in self.rules:
            if r.id == rule_id:
                return r
        return None

    def stat(self, profile: GoalProfile, category: FailureCategory, action: RepairAction) -> RouterStat:
        for s in self.router_stats:
            if s.profile == profile and s.category == category and s.action == action:
                return s
        s = RouterStat(profile=profile, category=category, action=action)
        self.router_stats.append(s)
        return s

    def applicable_rules(self, profile: GoalProfile, category: FailureCategory) -> list[PolicyRule]:
        return [
            r
            for r in self.rules
            if profile in r.profile_scope
            and r.trigger_category == category
            and r.status not in (PolicyStatus.REJECTED, PolicyStatus.RETIRED)
        ]
