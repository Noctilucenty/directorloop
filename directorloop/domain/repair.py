"""Repair proposals and the transparent routing record (spec sections 15 and 20)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .brief import RepairAction
from .edit_plan import EditOp


class LatencyEstimate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_ms: int
    source: str  # "measured:<n> samples" | "default" | "unknown"
    samples: int = 0


class GenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(max_length=600)
    duration_ms: int = Field(gt=0, le=10000)
    width: int = 512
    height: int = 896
    conditioning_asset_id: str | None = None
    must_show: list[str] = Field(default_factory=list)  # claim ids the shot must make visible
    identity_notes: str = ""  # what must stay consistent with existing footage


class RejectedAlternative(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: RepairAction
    reason: str


class ActionEstimate(BaseModel):
    """One row of the routing table shown to judges."""

    model_config = ConfigDict(extra="forbid")

    action: RepairAction
    allowed: bool
    feasible: bool
    expected_improvement: str  # high | medium | low | none | unknown
    latency: LatencyEstimate
    cost_usd: float | None = None
    creative_risk: str = "low"  # low | medium | high
    regression_risk: str = "low"
    prior_success_rate: float | None = None  # from router statistics, shrunk toward the prior
    prior_samples: int = 0
    note: str = ""


class RepairProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    finding_id: str
    hypothesis: str
    evidence_refs: list[str] = Field(default_factory=list)
    action: RepairAction
    ops: list[EditOp] = Field(default_factory=list)
    predicted_benefit: str = ""  # a prediction, labeled as such in the UI
    expected_latency: LatencyEstimate
    estimated_cost_usd: float = 0.0
    creative_risk: str = "low"
    regression_risk: str = "low"
    rejected_alternatives: list[RejectedAlternative] = Field(default_factory=list)
    requires_generation: bool = False
    generation_request: GenerationRequest | None = None
    validation_requirements: list[str] = Field(default_factory=list)
    policy_rule_ids_used: list[str] = Field(default_factory=list)
    planner: str = "rules"  # rules | <provider:model>
    routing_table: list[ActionEstimate] = Field(default_factory=list)
    decision_summary: str = ""  # short evidence-grounded summary, not chain-of-thought
