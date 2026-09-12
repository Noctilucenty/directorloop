"""Source truth and the frozen evaluation suite.

Source truth is the approved set of claims a video is supposed to communicate.
The evaluation suite holds hidden questions with correct answers. Viewer-facing
probe inputs are built from `ProbeQuestionView`, which carries no answers.
"""

from __future__ import annotations

import random
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .ids import sha256_json

NOT_SHOWN_OPTION_ID = "not_shown"


class ClaimKind(StrEnum):
    FACT = "fact"
    MECHANISM = "mechanism"
    SEQUENCE = "sequence"
    MOTIVATION = "motivation"
    ENTITY = "entity"
    OUTCOME = "outcome"


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    text: str
    kind: ClaimKind = ClaimKind.FACT
    uncertainty: str = "established"  # established | illustrative | interpretation
    supporting_asset_ids: list[str] = Field(default_factory=list)  # declared by the creator, not verified


class SourceReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    url: str | None = None
    note: str = ""


class SourceTruth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    version: int = 1
    is_fictional: bool
    title: str
    summary: str = ""
    claims: list[Claim]
    entities: list[str] = Field(default_factory=list)
    event_order: list[str] = Field(default_factory=list)  # claim ids in intended order
    source_references: list[SourceReference] = Field(default_factory=list)
    approved_by: str | None = None
    approved_at: str | None = None

    def claim(self, claim_id: str) -> Claim:
        for c in self.claims:
            if c.id == claim_id:
                return c
        raise KeyError(claim_id)

    def content_hash(self) -> str:
        return sha256_json(self.model_dump(mode="json"))


class EvidenceModality(StrEnum):
    VISUAL = "visual"  # must be answerable from the pictures alone
    AUDIO = "audio"  # answerable from narration / on-screen text
    EITHER = "either"


class ProbeOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    text: str


class ProbeQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    text: str
    modality: EvidenceModality = EvidenceModality.EITHER
    options: list[ProbeOption]
    correct_option_id: str
    claim_ids: list[str] = Field(default_factory=list)
    weight: float = 1.0
    regression_guard: bool = False  # expected to pass on the baseline; used to detect regressions
    leakage_status: str = "untested"  # untested | strong | weak
    leakage_note: str = ""

    @model_validator(mode="after")
    def _check_options(self) -> ProbeQuestion:
        ids = [o.id for o in self.options]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate option ids in {self.id}")
        if NOT_SHOWN_OPTION_ID not in ids:
            raise ValueError(f"question {self.id} must include a '{NOT_SHOWN_OPTION_ID}' option")
        if self.correct_option_id not in ids:
            raise ValueError(f"question {self.id} correct option not among options")
        if any(o.id == self.correct_option_id and o.id == NOT_SHOWN_OPTION_ID for o in self.options):
            raise ValueError("the correct answer cannot be 'not_shown'")
        return self


class ProbeQuestionView(BaseModel):
    """What the viewer model receives: no answers, no claim ids, no labels."""

    model_config = ConfigDict(extra="forbid")

    id: str
    text: str
    options: list[ProbeOption]


class SuiteSplit(StrEnum):
    DEV = "dev"
    VALIDATION = "validation"
    HOLDOUT = "holdout"


class EvaluationSuite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    version: int = 1
    split: SuiteSplit = SuiteSplit.DEV
    story_family: str
    questions: list[ProbeQuestion]
    scoring: str = "exact_option"
    frozen: bool = True

    def content_hash(self) -> str:
        return sha256_json(self.model_dump(mode="json"))

    def question(self, qid: str) -> ProbeQuestion:
        for q in self.questions:
            if q.id == qid:
                return q
        raise KeyError(qid)

    def viewer_views(self, seed: int | None = None) -> list[ProbeQuestionView]:
        """Answer-free views with option order shuffled (not_shown always last)."""
        rng = random.Random(seed)
        views: list[ProbeQuestionView] = []
        for q in self.questions:
            content = [o for o in q.options if o.id != NOT_SHOWN_OPTION_ID]
            not_shown = [o for o in q.options if o.id == NOT_SHOWN_OPTION_ID]
            rng.shuffle(content)
            views.append(ProbeQuestionView(id=q.id, text=q.text, options=content + not_shown))
        return views

    def total_weight(self) -> float:
        return sum(q.weight for q in self.questions) or 1.0
