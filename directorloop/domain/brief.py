"""Creative brief: what the video must accomplish and what must not change."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GoalProfile(StrEnum):
    EDUCATIONAL = "educational"
    PRODUCT_DEMO = "product_demo"
    NARRATIVE = "narrative"
    COMEDY = "comedy"
    CINEMATIC = "cinematic"


class RepairAction(StrEnum):
    """Repair actions in increasing cost/risk order (spec section 15)."""

    DO_NOTHING = "do_nothing"
    TRIM_OR_RETIME = "trim_or_retime"
    REORDER_SEGMENTS = "reorder_segments"
    REPLACE_WITH_EXISTING_ASSET = "replace_with_existing_asset"
    CROP_EXISTING_SHOT = "crop_existing_shot"
    REVISE_CAPTIONS = "revise_captions"
    REVISE_NARRATION = "revise_narration"
    ADD_GRAPHIC = "add_graphic"
    GENERATE_MISSING_SHOT = "generate_missing_shot"
    REGENERATE_SEGMENT = "regenerate_segment"
    REGENERATE_FULL_DRAFT = "regenerate_full_draft"


ACTION_COST_ORDER: list[RepairAction] = [
    RepairAction.DO_NOTHING,
    RepairAction.TRIM_OR_RETIME,
    RepairAction.REORDER_SEGMENTS,
    RepairAction.REPLACE_WITH_EXISTING_ASSET,
    RepairAction.CROP_EXISTING_SHOT,
    RepairAction.REVISE_CAPTIONS,
    RepairAction.REVISE_NARRATION,
    RepairAction.ADD_GRAPHIC,
    RepairAction.GENERATE_MISSING_SHOT,
    RepairAction.REGENERATE_SEGMENT,
    RepairAction.REGENERATE_FULL_DRAFT,
]


class ConstraintKind(StrEnum):
    KEEP_NARRATION = "keep_narration"
    KEEP_MUSIC = "keep_music"
    NO_NEW_TEXT = "no_new_text"
    PRESERVE_INTERVAL = "preserve_interval"
    MAX_DURATION_MS = "max_duration_ms"
    MIN_DURATION_MS = "min_duration_ms"
    NO_GENERATION = "no_generation"
    PRESERVE_PRODUCT_APPEARANCE = "preserve_product_appearance"
    NO_NEW_FACTS = "no_new_facts"
    KEEP_SILENCE_INTERVAL = "keep_silence_interval"
    NO_CTA = "no_cta"


class ProtectedConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: ConstraintKind
    reason: str = ""
    start_ms: int | None = None
    end_ms: int | None = None
    value_ms: int | None = None

    @model_validator(mode="after")
    def _check_fields(self) -> ProtectedConstraint:
        if self.kind in (ConstraintKind.PRESERVE_INTERVAL, ConstraintKind.KEEP_SILENCE_INTERVAL):
            if self.start_ms is None or self.end_ms is None or self.end_ms <= self.start_ms:
                raise ValueError(f"{self.kind} needs start_ms < end_ms")
        if self.kind in (ConstraintKind.MAX_DURATION_MS, ConstraintKind.MIN_DURATION_MS):
            if self.value_ms is None or self.value_ms <= 0:
                raise ValueError(f"{self.kind} needs a positive value_ms")
        return self


class RequiredInformation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    text: str
    claim_ids: list[str] = Field(default_factory=list)


class Budget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_usd: float = 2.0
    max_llm_calls: int = 40
    max_generation_calls: int = 1
    max_wall_seconds: int = 45


class CreativeBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    profile: GoalProfile
    objective: str
    audience: str = "general"
    language: str = "en"
    aspect_ratio: str = "9:16"
    target_duration_ms_min: int = 6000
    target_duration_ms_max: int = 15000
    required_information: list[RequiredInformation] = Field(default_factory=list)
    style_notes: str = ""
    protected_constraints: list[ProtectedConstraint] = Field(default_factory=list)
    allowed_actions: list[RepairAction] = Field(default_factory=list)
    generation_permitted: bool = False
    narration_change_permitted: bool = False
    budget: Budget = Field(default_factory=Budget)
    review_status: str = "draft"  # draft | approved
    approved_by: str | None = None

    def max_duration_ms(self) -> int:
        limit = self.target_duration_ms_max
        for c in self.protected_constraints:
            if c.kind == ConstraintKind.MAX_DURATION_MS and c.value_ms is not None:
                limit = min(limit, c.value_ms)
        return limit

    def min_duration_ms(self) -> int:
        limit = self.target_duration_ms_min
        for c in self.protected_constraints:
            if c.kind == ConstraintKind.MIN_DURATION_MS and c.value_ms is not None:
                limit = max(limit, c.value_ms)
        return limit

    def has_constraint(self, kind: ConstraintKind) -> bool:
        return any(c.kind == kind for c in self.protected_constraints)

    def action_allowed(self, action: RepairAction) -> bool:
        if action == RepairAction.DO_NOTHING:
            return True
        if action in (
            RepairAction.GENERATE_MISSING_SHOT,
            RepairAction.REGENERATE_SEGMENT,
            RepairAction.REGENERATE_FULL_DRAFT,
        ):
            if not self.generation_permitted or self.has_constraint(ConstraintKind.NO_GENERATION):
                return False
        if action == RepairAction.REVISE_NARRATION and not self.narration_change_permitted:
            return False
        return action in self.allowed_actions
