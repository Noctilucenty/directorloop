"""Evaluation records: mechanical checks, model comprehension probes, constraint checks.

Three evidence levels stay separate (spec section 16). Nothing here averages them
into one number; `score` is the weighted probe pass rate and is labeled as such.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class MeasurementType(StrEnum):
    MECHANICAL = "mechanical"
    MODEL_PROBE = "model_probe"
    HUMAN = "human"


class ProbeModality(StrEnum):
    VIDEO_NATIVE = "video_native"  # the provider received the actual video file (frames + audio)
    FRAMES_AND_TRANSCRIPT = "frames_and_transcript"  # timestamped frames + ASR of rendered audio
    TRANSCRIPT_ONLY = "transcript_only"
    NONE = "none"  # no-media control


class MechanicalCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    passed: bool
    severity: str = "critical"  # critical | warning
    value: float | int | str | None = None
    threshold: float | int | str | None = None
    detail: str = ""
    measurement_type: MeasurementType = MeasurementType.MECHANICAL


class ConstraintCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    constraint_id: str
    kind: str
    passed: bool
    detail: str = ""


class ProbeAnswer(BaseModel):
    """Raw provider answer for one question in one trial."""

    model_config = ConfigDict(extra="forbid")

    question_id: str
    trial: int
    chosen_option_id: str | None = None
    raw_text: str | None = None
    latency_ms: int = 0
    error: str | None = None


class ProbeResult(BaseModel):
    """Scored answer. `correct` is None when the trial produced no usable answer."""

    model_config = ConfigDict(extra="forbid")

    question_id: str
    trial: int
    chosen_option_id: str | None = None
    correct: bool | None = None
    error: str | None = None
    latency_ms: int = 0


class QuestionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: str
    modality: str
    weight: float = 1.0
    regression_guard: bool = False
    valid_trials: int
    errors: int
    correct: int
    pass_rate: float
    passed: bool  # majority of valid trials correct
    chosen: dict[str, int] = Field(default_factory=dict)


class EvaluationRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    version_id: str
    artifact_hash: str
    suite_id: str
    suite_hash: str
    mode: str = "fresh"  # fresh | cached | recorded
    probe_modality: ProbeModality
    provider: str
    model: str
    trials: int
    frames_sampled: int | None = None
    frame_timestamps_ms: list[int] = Field(default_factory=list)
    transcript_source: str | None = None
    transcript_text: str | None = None
    results: list[ProbeResult] = Field(default_factory=list)
    question_summaries: list[QuestionSummary] = Field(default_factory=list)
    mechanical: list[MechanicalCheck] = Field(default_factory=list)
    constraints: list[ConstraintCheck] = Field(default_factory=list)
    questions_passed: int = 0
    questions_total: int = 0
    trials_correct: int = 0
    trials_valid: int = 0
    trials_errored: int = 0
    score: float = 0.0  # weighted fraction of questions passed; model-probe evidence only
    mechanical_passed: bool = True
    constraints_passed: bool = True
    started_at: str | None = None
    ended_at: str | None = None
    latency_ms: int = 0
    probe_latency_ms: int = 0
    cost_usd_estimate: float | None = None
    weave_call_id: str | None = None
    weave_url: str | None = None
    cache_key: str | None = None
    notes: list[str] = Field(default_factory=list)

    def summary_for(self, question_id: str) -> QuestionSummary | None:
        for s in self.question_summaries:
            if s.question_id == question_id:
                return s
        return None

    def failed_question_ids(self) -> list[str]:
        return [s.question_id for s in self.question_summaries if not s.passed]

    def passed_question_ids(self) -> list[str]:
        return [s.question_id for s in self.question_summaries if s.passed]

    def critical_mechanical_failures(self) -> list[MechanicalCheck]:
        return [m for m in self.mechanical if not m.passed and m.severity == "critical"]


class LeakageControlResult(BaseModel):
    """No-media control: can the model answer without the video?"""

    model_config = ConfigDict(extra="forbid")

    suite_hash: str
    provider: str
    model: str
    trials: int
    per_question: dict[str, float]  # question id -> fraction correct with no media
    weak_question_ids: list[str]
    chance_rate: dict[str, float]
    ran_at: str
