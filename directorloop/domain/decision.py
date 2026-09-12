"""Version comparison and the promotion decision (spec section 21)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Outcome(StrEnum):
    PROMOTED = "promoted"
    REJECTED = "rejected"
    NEEDS_REVIEW = "needs_review"
    NO_GAIN = "no_gain"
    NEEDS_SOURCE_MATERIAL = "needs_source_material"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class HardGate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    passed: bool
    detail: str = ""


class QuestionDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: str
    before_passed: bool
    after_passed: bool
    before_rate: float
    after_rate: float
    regression_guard: bool = False

    @property
    def status(self) -> str:
        if self.before_passed and not self.after_passed:
            return "regressed"
        if not self.before_passed and self.after_passed:
            return "fixed"
        if self.before_passed:
            return "kept"
        return "still_failing"


class Comparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    baseline_version_id: str
    candidate_version_id: str
    suite_hash: str
    matched_config: bool
    config_note: str = ""
    deltas: list[QuestionDelta]
    fixed: list[str]
    regressed: list[str]
    kept: list[str]
    still_failing: list[str]
    score_before: float
    score_after: float
    questions_passed_before: int
    questions_passed_after: int
    questions_total: int
    duration_before_ms: int
    duration_after_ms: int
    mechanical_before: bool
    mechanical_after: bool
    constraints_before: bool
    constraints_after: bool

    @property
    def delta_questions(self) -> int:
        return self.questions_passed_after - self.questions_passed_before

    @property
    def delta_score(self) -> float:
        return self.score_after - self.score_before


class PromotionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Outcome
    hard_gates: list[HardGate]
    min_improvement_questions: int
    delta_questions: int
    delta_score: float
    regressions: list[str] = Field(default_factory=list)
    fixed: list[str] = Field(default_factory=list)
    reason: str
    blocking_gate_id: str | None = None
