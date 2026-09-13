"""Screening records distinguish subjective rubric scores from audience forecasts."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..observability.workflow import WorkflowStage


class ScreenObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    text: str = Field(min_length=1, max_length=240)
    kind: Literal["visible_fact", "caption_claim", "asr_claim", "inference", "unknown"]
    frame_timestamps_ms: list[int] = Field(max_length=6)
    asr_quote: str | None = Field(max_length=160)


class ScreenReviewCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    aspect: Literal["pacing", "visual_clarity", "caption_readability", "caption_alignment", "hook_and_payoff", "tone_from_words", "voice_delivery", "share_motivation"]
    status: Literal["concern", "clear", "unknown"]
    reason: str = Field(min_length=1, max_length=160)
    observation_indices: list[int] = Field(max_length=4)
    suggested_change: str | None = Field(default=None, max_length=160)


class ScreenPotentialScore(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    dimension: Literal["creative", "retention", "virality"]
    rating: Annotated[int, Field(ge=0, le=4)] | None
    reason: str = Field(min_length=1, max_length=160)
    observation_indices: list[int] = Field(max_length=4)


class ScreenJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    observations: list[ScreenObservation] = Field(min_length=1, max_length=12)
    understanding: str = Field(min_length=1, max_length=240)
    attention_risk: Literal["low", "medium", "high", "unknown"]
    cause_observation_indices: list[int] = Field(max_length=4)
    suggestion: str = Field(max_length=200)
    moment_kind: Literal["setup", "development", "reveal", "endcard", "signoff", "unknown"]
    uncertainties: list[Annotated[str, Field(max_length=160)]] = Field(max_length=2)
    review_checks: list[ScreenReviewCheck] = Field(default_factory=list, max_length=8)
    potential_scores: list[ScreenPotentialScore] = Field(default_factory=list, max_length=3)


class ScreenFrame(BaseModel):
    t_ms: int
    path: str
    sha256: str
    width: int
    height: int


class ScreenFrameChange(BaseModel):
    from_ms: int
    to_ms: int
    gap_ms: int
    mean_absolute_luma_delta: float
    changed_pixel_fraction: float


class ScreenMechanicalVisual(BaseModel):
    """Pixel measurements on supplied samples; never semantic motion or attention."""

    version: str = "sampled-luma-change-v1"
    evidence_level: Literal["mechanical"] = "mechanical"
    status: Literal["measured", "insufficient_frames", "unavailable"]
    start_ms: int
    end_ms: int
    resize_width: int = 64
    resize_height: int = 64
    changed_pixel_threshold: int = 8
    sample_timestamps_ms: list[int] = Field(default_factory=list)
    sample_sha256: list[str] = Field(default_factory=list)
    pairs: list[ScreenFrameChange] = Field(default_factory=list)
    mean_absolute_luma_delta: float | None = None
    mean_changed_pixel_fraction: float | None = None
    error: str | None = None
    interpretation: str = "Differences between sampled pixels. Camera movement, cuts, light, captions and compression can all contribute; this does not identify motion meaning, engagement or retention."


class ScreenWindow(BaseModel):
    start_ms: int
    end_ms: int
    is_last_prefix: bool = False
    status: Literal["pending", "complete", "needs_review", "failed", "not_attempted"] = "pending"
    judgment: ScreenJudgment | None = None
    attention_context: Literal["content", "last_endcard", "last_signoff", "unknown"] = "unknown"
    validation_issues: list[str] = Field(default_factory=list)
    mechanical_visual: ScreenMechanicalVisual | None = None
    delivery_signals: dict[str, Any] | None = None
    semantic_grounding_verified: Literal[False] = False
    evidence_frames: list[ScreenFrame] = Field(default_factory=list)
    frame_timestamps_ms: list[int] = Field(default_factory=list)
    prefix_asr_text: str = ""
    raw_output: dict[str, Any] = Field(default_factory=dict)
    instruction_sha256: str | None = None
    payload_sha256: str | None = None
    response_schema_sha256: str | None = None
    latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None
    weave_url: str | None = None
    weave_call_id: str | None = None
    review_attempts: list[dict[str, Any]] = Field(default_factory=list)


class ScreenReport(BaseModel):
    id: str
    video_id: str
    artifact_path: str
    artifact_hash: str = ""
    duration_ms: int = 0
    created_at: str
    ended_at: str | None = None
    status: Literal["running", "complete", "needs_review", "failed", "canceled"] = "running"
    mode: Literal["fresh"] = "fresh"
    error: str | None = None
    review_required: Literal[True] = True
    evidence_label: Literal["Model screening suggestions"] = "Model screening suggestions"
    semantic_grounding_verified: Literal[False] = False
    automatic_edit_allowed: Literal[False] = False
    protocol_fingerprint: str = ""
    protocol: dict[str, Any] = Field(default_factory=dict)
    windows: list[ScreenWindow] = Field(default_factory=list)
    model_calls: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    elapsed_ms: int = 0
    asr_cache_mode: Literal["fresh", "cached", "no_audio", "unavailable"] = "unavailable"
    limitations: list[str] = Field(default_factory=lambda: [
        "Three sampled prefixes at most; not continuous video or native audio review.",
        "Frame citations identify bounded sampling requests, not independently measured decoded presentation times.",
        "Frame citations and ASR quotes are checked for input membership, not semantic truth.",
        "Suggestions require review; no measured retention, predicted views, edits or promotion.",
        "Caption and ASR content can be mistaken; hidden objects must remain unknown.",
        "Last-prefix endcards and signoffs are not automatically repair targets.",
    ])
    scorecard: dict[str, Any] | None = None
    spend_guard: dict[str, Any] | None = None
    weave_url: str | None = None
    weave_call_id: str | None = None
    workflow_stages: list[WorkflowStage] = Field(default_factory=list)
    review_repair: dict[str, Any] | None = None
