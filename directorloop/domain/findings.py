"""Failure taxonomy and structured findings (spec section 19)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .brief import RepairAction
from .evaluation import MeasurementType


class FailureCategory(StrEnum):
    UNCLEAR_ACTOR = "UNCLEAR_ACTOR"
    UNCLEAR_CAUSALITY = "UNCLEAR_CAUSALITY"
    MISSING_CONTEXT = "MISSING_CONTEXT"
    AMBIGUOUS_PRONOUN = "AMBIGUOUS_PRONOUN"
    MISSING_REQUIRED_INFORMATION = "MISSING_REQUIRED_INFORMATION"
    VISUAL_NARRATION_MISMATCH = "VISUAL_NARRATION_MISMATCH"
    MISSING_ACTION_VISIBILITY = "MISSING_ACTION_VISIBILITY"
    EVENT_ORDER_CONFUSION = "EVENT_ORDER_CONFUSION"
    REVEAL_SPOILED = "REVEAL_SPOILED"
    STYLE_CONSTRAINT_VIOLATION = "STYLE_CONSTRAINT_VIOLATION"
    CAPTION_OVERFLOW = "CAPTION_OVERFLOW"
    CAPTION_TIMING_ERROR = "CAPTION_TIMING_ERROR"
    AUDIO_TEXT_CONTRADICTION = "AUDIO_TEXT_CONTRADICTION"
    AUDIO_CUT_OR_CLIP = "AUDIO_CUT_OR_CLIP"
    TEMPORAL_CONTINUITY_SUSPECTED = "TEMPORAL_CONTINUITY_SUSPECTED"
    UNSUPPORTED_FACT = "UNSUPPORTED_FACT"
    UNLICENSED_OR_UNAPPROVED_ASSET = "UNLICENSED_OR_UNAPPROVED_ASSET"
    SOURCE_MATERIAL_INSUFFICIENT = "SOURCE_MATERIAL_INSUFFICIENT"
    EVALUATOR_UNRELIABLE = "EVALUATOR_UNRELIABLE"
    RENDER_INVALID = "RENDER_INVALID"


class FindingEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    failed_question_ids: list[str] = Field(default_factory=list)
    probe_failures: int = 0
    probe_trials: int = 0
    mechanical_check_ids: list[str] = Field(default_factory=list)
    constraint_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    transcript_mentions_claim: bool | None = None
    coverage_note: str = ""


class FailureFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    category: FailureCategory
    severity: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    start_ms: int | None = None
    end_ms: int | None = None
    timestamp_precision: str = "segment"  # segment | frame | unknown; never finer than the evidence
    observed: str  # what was measured
    inferred_cause: str  # hypothesis, separate from the observation
    alternatives: list[str] = Field(default_factory=list)
    evidence: FindingEvidence = Field(default_factory=FindingEvidence)
    measurement_type: MeasurementType = MeasurementType.MODEL_PROBE
    editable_dimensions: list[RepairAction] = Field(default_factory=list)
    affected_segment_ids: list[str] = Field(default_factory=list)
