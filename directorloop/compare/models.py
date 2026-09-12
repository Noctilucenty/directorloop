"""A/B-to-C records: declared context, frozen rubric, beat alignment, comparisons, C proposals, evaluations, decisions."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..audit.models import ChangeVerification

Label = Literal["A", "B", "C"]
Outcome = Literal["improvement", "regression", "mixed", "tie", "insufficient_evidence"]


class DeclaredContext(BaseModel):
    """Held constant for every version. The cold audit never receives objective, payoff or owner notes."""

    model_config = ConfigDict(extra="forbid")

    creative_type: str = Field(default="educational short", max_length=120)
    audience: str = Field(default="general viewers who do not know the topic", max_length=200)
    objective: str = Field(max_length=400)
    expected_payoff: str = Field(default="", max_length=300)
    encounter: str = Field(default="a cold scrolling feed on a phone with sound on", max_length=200)
    source: Literal["declared_by_owner", "defaults"] = "declared_by_owner"


class VersionRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: Label
    video_id: str
    title: str = ""
    role: Literal["input", "director"] = "input"
    original_path: str | None = None
    original_hash: str | None = None
    evaluated_path: str  # the file every reviewer received (same render pipeline for A, B and C)
    evaluated_hash: str
    duration_ms: int
    audit_id: str | None = None
    audit_status: str | None = None
    genome_cached: bool | None = None
    notes: list[str] = Field(default_factory=list)


class AlignedUnit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    kind: Literal["shared", "reworded", "only_a", "only_b", "time_aligned"]
    a_beats: list[str] = Field(default_factory=list)
    b_beats: list[str] = Field(default_factory=list)
    a_start_ms: int | None = None
    a_end_ms: int | None = None
    b_start_ms: int | None = None
    b_end_ms: int | None = None
    text_a: str = ""
    text_b: str = ""
    similarity: float | None = None


class DimensionJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension: str
    verdict: str  # a version label, "same", "unstable" (presentation orders disagree) or "unclear"
    votes: dict[str, int] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)


class PairComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first: Label
    second: Label
    scope: Literal["whole", "region"]
    region: dict[str, Any] | None = None  # intervals compared when scope is region
    dimensions: list[DimensionJudgment] = Field(default_factory=list)
    overall: DimensionJudgment
    calls: int = 0
    failed_calls: int = 0
    rubric_version: str
    label: str = "MODEL JUDGMENT: both presentation orders; disagreement kept as 'unstable'"

    def verdict(self, dimension: str) -> str:
        if dimension == "overall":
            return self.overall.verdict
        return next((d.verdict for d in self.dimensions if d.dimension == dimension), "unclear")


class ABComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    comparable: bool
    confounds: list[str] = Field(default_factory=list)
    transcript_similarity: float | None = None
    alignment: list[AlignedUnit] = Field(default_factory=list)
    whole: PairComparison | None = None
    regions: list[PairComparison] = Field(default_factory=list)
    findings_by_unit: dict[str, list[str]] = Field(default_factory=dict)  # "A:2" -> finding summaries
    strengths_by_unit: dict[str, list[str]] = Field(default_factory=dict)
    best_supported: str = ""  # "A", "B" or "" when the comparison gives no reliable preference
    best_supported_reason: str = ""


class COption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    kind: Literal["swap_unit", "insert_unit", "remove_unit", "repair"]
    base: Literal["A", "B"]
    donor: Literal["A", "B"] | None = None
    unit_index: int | None = None
    description: str
    changes: list[str] = Field(default_factory=list)  # every change in the bundle; a result is never attributed to one of several
    side_effects: list[str] = Field(default_factory=list)
    duration_ms: int
    base_region_ms: tuple[int, int]  # the part of the base that changes, with one neighbouring sentence each side
    candidate_region_ms: tuple[int, int]  # the same stretch in C


class CProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    option_key: str
    base: Literal["A", "B"]
    donor: Literal["A", "B"] | None = None
    description: str
    target_dimensions: list[str]
    expected_improvement: str = ""
    protected_strengths: list[dict[str, str]] = Field(default_factory=list)  # {"source": "A"|"B", "item": text}
    tradeoffs: list[str] = Field(default_factory=list)
    why_smallest: str = ""
    selector_reason: str = ""
    options_offered: list[str] = Field(default_factory=list)


class CEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_audit_id: str | None = None
    candidate_audit_status: str | None = None
    change_verification: ChangeVerification | None = None
    vs_base: PairComparison | None = None
    vs_other: PairComparison | None = None
    target_region: PairComparison | None = None
    protected: list[dict[str, str]] = Field(default_factory=list)
    new_weaknesses: list[str] = Field(default_factory=list)
    improved: list[str] = Field(default_factory=list)
    regressed: list[str] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)
    uncertain: list[str] = Field(default_factory=list)
    outcome: Outcome = "insufficient_evidence"
    outcome_reason: str = ""


class CAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    proposal: CProposal | None = None
    render_path: str | None = None
    render_hash: str | None = None
    version_id: str | None = None
    evaluation: CEvaluation | None = None
    decision: Literal["accept", "reject_keep_inputs", "incomplete", "stop"]
    reason: str
    next_action: Literal["try_another_c", "stop"]
    next_action_reason: str = ""
    lesson_id: str | None = None
    timings_ms: dict[str, int] = Field(default_factory=dict)


class ABCRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: Literal["running", "completed", "failed"] = "running"
    created_at: str
    ended_at: str | None = None
    context: DeclaredContext
    constraints: list[str] = Field(default_factory=list)
    limits: dict[str, Any] = Field(default_factory=dict)
    rubric: dict[str, Any] = Field(default_factory=dict)  # frozen before any candidate is chosen; includes its sha256
    versions: dict[str, VersionRef] = Field(default_factory=dict)
    comparison: ABComparison | None = None
    attempts: list[CAttempt] = Field(default_factory=list)
    final_version: str = ""
    final_decision: str = ""
    stop_reason: str = ""
    usage: dict[str, Any] = Field(default_factory=dict)
    runtime: dict[str, Any] = Field(default_factory=dict)
    mocked_stages: list[str] = Field(default_factory=list)
    manual_interventions: list[str] = Field(default_factory=list)
    annotations: list[str] = Field(default_factory=list)
    launched_via: str = "python"
    weave_url: str | None = None
    weave_call_id: str | None = None
    timings_ms: dict[str, int] = Field(default_factory=dict)
    error: str | None = None
