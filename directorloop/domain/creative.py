"""Creative research domain: genome, retention signals, references, hypotheses, mutations, experiments.

Every derived feature records where it came from (mechanical, ASR, vision model, language model,
creator declared) and a confidence. Evidence classes stay separate:
REFERENCE (what comparable references did), OFFLINE FITNESS (model evals and mechanical checks on
our renders), HUMAN (blinded preference), REAL (published platform retention).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .edit_plan import EditOp


class FeatureSource(StrEnum):
    MECHANICAL = "mechanical"
    ASR = "asr"
    VISION_MODEL = "vision_model"
    LANGUAGE_MODEL = "language_model"
    CREATOR_DECLARED = "creator_declared"
    DERIVED_FROM_PLAN = "derived_from_plan"


class EvidenceClass(StrEnum):
    REAL = "real"  # measured on a published platform
    HISTORICAL = "historical"  # owned historical platform data (summary metrics)
    SIMULATED = "simulated"  # demo data, never shown as measured
    MODEL_EVAL = "model_eval"
    HUMAN_TEST = "human_test"
    MECHANICAL = "mechanical"
    REFERENCE = "reference"


class Feature(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: float | int | str | bool | None
    source: FeatureSource
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    note: str = ""


# ----------------------------------------------------------------------------- genome


class BeatRole(StrEnum):
    HOOK = "hook"
    SETUP = "setup"
    CONTEXT = "context"
    PROBLEM = "problem"
    TENSION = "tension"
    PROOF = "proof"
    MECHANISM = "mechanism"
    PAYOFF = "payoff"
    CTA = "cta"
    OTHER = "other"


class HookType(StrEnum):
    CURIOSITY_QUESTION = "curiosity_question"
    CONTRADICTION = "contradiction"
    SURPRISING_CLAIM = "surprising_claim"
    RESULT_FIRST = "result_first"
    DEMONSTRATION = "demonstration"
    PROBLEM_STATEMENT = "problem_statement"
    CHALLENGE = "challenge"
    SOCIAL_PROOF = "social_proof"
    CONFESSION = "confession"
    DIRECT_BENEFIT = "direct_benefit"
    VISUAL_SURPRISE = "visual_surprise"
    CONTEXT_FIRST = "context_first"
    UNKNOWN = "unknown"


class Shot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    start_ms: int
    end_ms: int
    motion: float = 0.0  # mean normalized frame difference inside the shot (mechanical)
    source: FeatureSource = FeatureSource.MECHANICAL

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


class TimelineBeat(BaseModel):
    """A sentence-aligned unit of the video: the smallest thing a mutation moves, removes or retimes."""

    model_config = ConfigDict(extra="forbid")

    id: str
    index: int
    start_ms: int
    end_ms: int
    text: str
    role: BeatRole = BeatRole.OTHER
    role_source: FeatureSource = FeatureSource.LANGUAGE_MODEL
    role_confidence: float = 0.0
    is_question: bool = False
    is_claim: bool = False
    introduces_new_information: bool = True
    redundant_with_beat_id: str | None = None
    shot_ids: list[str] = Field(default_factory=list)
    shot_changes_inside: int = 0
    motion: float = 0.0
    words: int = 0
    note: str = ""

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


class OpenLoop(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    opened_beat_id: str
    opened_ms: int
    resolved_beat_id: str | None = None
    resolved_ms: int | None = None
    description: str = ""
    source: FeatureSource = FeatureSource.LANGUAGE_MODEL
    confidence: float = 0.5

    @property
    def duration_ms(self) -> int | None:
        return None if self.resolved_ms is None else self.resolved_ms - self.opened_ms


class HookProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hook_start_ms: int = 0
    hook_end_ms: int = 3000
    hook_type: Feature
    first_spoken_words: Feature
    first_visual_description: Feature
    face_visible_at_start: Feature
    product_or_subject_visible_at_start: Feature
    first_visual_change_ms: Feature
    first_meaningful_motion_ms: Feature
    first_claim_ms: Feature
    first_question_ms: Feature


class CreativeGenome(BaseModel):
    """Machine-readable description of the ACTUAL rendered video."""

    model_config = ConfigDict(extra="forbid")

    id: str
    genome_version: str = "1"
    artifact_hash: str
    duration_ms: int
    width: int
    height: int
    aspect_ratio: str
    category: str = "educational_short"
    declared_objective: str = ""
    audience: str = "general"
    hook: HookProfile
    beats: list[TimelineBeat]
    shots: list[Shot]
    open_loops: list[OpenLoop] = Field(default_factory=list)
    transcript_source: str = "whisper.cpp"
    speech_rate_wps: Feature
    first_proof_ms: Feature
    first_payoff_ms: Feature
    context_before_proof_ms: Feature
    longest_static_span_ms: Feature
    longest_static_span_start_ms: Feature
    shots_per_10s: Feature
    cta_ms: Feature
    motion_curve: list[tuple[int, float]] = Field(default_factory=list)  # (t_ms, normalized change), mechanical
    extracted_at: str = ""
    extractor: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)

    def beat(self, beat_id: str) -> TimelineBeat:
        for b in self.beats:
            if b.id == beat_id:
                return b
        raise KeyError(beat_id)

    def beats_with_role(self, *roles: BeatRole) -> list[TimelineBeat]:
        return [b for b in self.beats if b.role in roles]


# ----------------------------------------------------------------------------- retention signal


class RetentionSourceType(StrEnum):
    REAL_PLATFORM = "real_platform"
    HISTORICAL_OWNED = "historical_owned"
    SIMULATED_DEMO = "simulated_demo"
    UNAVAILABLE = "unavailable"


class RetentionPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    t_ms: int
    remaining_fraction: float = Field(ge=0.0, le=1.0)


class RetentionSeries(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: RetentionSourceType
    platform: str = "manual"  # instagram | tiktok | youtube | facebook | manual | demo
    points: list[RetentionPoint] = Field(default_factory=list)
    plays: int | None = None
    reach: int | None = None
    avg_watch_time_ms: int | None = None
    hold_1s: float | None = None
    hold_3s: float | None = None
    completion_rate: float | None = None
    shares: int | None = None
    saves: int | None = None
    likes: int | None = None
    comments: int | None = None
    provenance: str = ""
    sample_size: int | None = None
    collected_at: str | None = None
    caveats: list[str] = Field(default_factory=list)

    @property
    def evidence_class(self) -> EvidenceClass:
        return {
            RetentionSourceType.REAL_PLATFORM: EvidenceClass.REAL,
            RetentionSourceType.HISTORICAL_OWNED: EvidenceClass.HISTORICAL,
            RetentionSourceType.SIMULATED_DEMO: EvidenceClass.SIMULATED,
            RetentionSourceType.UNAVAILABLE: EvidenceClass.MODEL_EVAL,
        }[self.source_type]


# ----------------------------------------------------------------------------- reference corpus


class ReferenceCreative(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str = ""
    source: str  # owned_curio | recorded | licensed | synthetic
    permission: str  # why we may analyze it
    provenance: dict[str, Any] = Field(default_factory=dict)  # local path, permalink, platform media id, publish date
    category: str = "educational_short"
    artifact_hash: str | None = None
    media_path: str | None = None  # local only; never committed for third-party or unpublished media
    performance: RetentionSeries | None = None
    genome_id: str | None = None
    patterns: list[str] = Field(default_factory=list)
    label: str = "REFERENCE CREATIVE"  # never "VIRAL WINNER" without legitimate performance data


class PatternObservation(BaseModel):
    """Descriptive counts from the reference corpus. A research prior, not causal evidence."""

    model_config = ConfigDict(extra="forbid")

    pattern_id: str
    description: str
    scope: str
    count: int
    total_comparable: int
    support_ratio: float
    example_ids: list[str] = Field(default_factory=list)
    counterexample_ids: list[str] = Field(default_factory=list)
    top_group_count: int | None = None  # among references in the upper half of the performance metric
    top_group_total: int | None = None
    bottom_group_count: int | None = None
    bottom_group_total: int | None = None
    performance_metric: str | None = None
    note: str = ""


class ReferenceCorpus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    version: int = 1
    description: str = ""
    references: list[ReferenceCreative] = Field(default_factory=list)
    patterns: list[PatternObservation] = Field(default_factory=list)
    built_at: str = ""
    notes: list[str] = Field(default_factory=list)

    def pattern(self, pattern_id: str) -> PatternObservation | None:
        return next((p for p in self.patterns if p.pattern_id == pattern_id), None)


# ----------------------------------------------------------------------------- hypotheses and mutations


class HypothesisFamily(StrEnum):
    PROOF_LATENCY = "proof_latency"
    CONTEXT_INTERRUPTION = "context_interruption"
    VISUAL_STAGNATION = "visual_stagnation"
    HOOK_MISMATCH = "hook_mismatch"
    WEAK_HOOK_TENSION = "weak_hook_tension"
    OPEN_LOOP_TOO_LONG = "open_loop_too_long"
    REDUNDANT_BEAT = "redundant_beat"
    DEAD_AIR = "dead_air"


class MutationType(StrEnum):
    HOOK_REWRITE = "HOOK_REWRITE"
    HOOK_VISUAL_SWAP = "HOOK_VISUAL_SWAP"
    RESULT_FIRST = "RESULT_FIRST"
    PROOF_EARLIER = "PROOF_EARLIER"
    PAYOFF_EARLIER = "PAYOFF_EARLIER"
    CONTEXT_COMPRESSION = "CONTEXT_COMPRESSION"
    REMOVE_REDUNDANT_BEAT = "REMOVE_REDUNDANT_BEAT"
    SHOT_SWAP = "SHOT_SWAP"
    SHOT_REORDER = "SHOT_REORDER"
    SHORTEN_SHOT = "SHORTEN_SHOT"
    EXTEND_PROOF = "EXTEND_PROOF"
    CAPTION_COMPRESSION = "CAPTION_COMPRESSION"
    CAPTION_EMPHASIS = "CAPTION_EMPHASIS"
    PATTERN_INTERRUPT = "PATTERN_INTERRUPT"
    PRODUCT_EARLIER = "PRODUCT_EARLIER"
    DEMO_EARLIER = "DEMO_EARLIER"
    CTA_REPOSITION = "CTA_REPOSITION"
    AUDIO_EMPHASIS = "AUDIO_EMPHASIS"
    GENERATE_MISSING_SHOT = "GENERATE_MISSING_SHOT"


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: EvidenceClass
    text: str
    source: FeatureSource | None = None
    ref: str | None = None  # pattern id, experiment id, beat id


class CreativeHypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    family: HypothesisFamily
    statement: str
    region_start_ms: int
    region_end_ms: int
    region_basis: str  # "creative structure" | "measured retention drop" | ...
    evidence: list[EvidenceItem] = Field(default_factory=list)
    counterevidence: list[EvidenceItem] = Field(default_factory=list)
    scope: str
    detection_confidence: float = Field(ge=0.0, le=1.0)
    candidate_mutations: list[MutationType]
    changed_variable: str


class CreativeMutation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    type: MutationType
    hypothesis_id: str
    parent_version_id: str
    target_start_ms: int
    target_end_ms: int
    changed_variable: str
    ops: list[EditOp]
    protected_variables: list[str]
    description: str
    expected_benefit: str  # a prediction, labeled as such
    risk: str = "low"
    cost_usd: float = 0.0
    latency_estimate_ms: int = 0
    evidence_refs: list[str] = Field(default_factory=list)
    diff_lines: list[str] = Field(default_factory=list)


# ----------------------------------------------------------------------------- fitness and experiments


class FitnessComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    value: float | None
    unit: str = ""
    better: str = "higher"  # higher | lower
    evidence: EvidenceClass
    detail: str = ""
    trials: int | None = None
    valid: int | None = None


class CreativeFitness(BaseModel):
    """Offline creative fitness. Not retention. Components stay visible; there is no hidden 0-100 score."""

    model_config = ConfigDict(extra="forbid")

    arm_id: str
    components: list[FitnessComponent]
    hard_gates_passed: bool
    gate_notes: list[str] = Field(default_factory=list)

    def get(self, name: str) -> FitnessComponent | None:
        return next((c for c in self.components if c.name == name), None)

    def value(self, name: str) -> float | None:
        c = self.get(name)
        return None if c is None else c.value


class ExperimentArm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str  # control | A | B | C
    mutation: CreativeMutation | None = None
    version_id: str | None = None
    artifact_hash: str | None = None
    artifact_path: str | None = None
    duration_ms: int | None = None
    render_ms: int | None = None
    status: str = "planned"  # planned | rendered | evaluated | failed | skipped
    error: str | None = None
    fitness: CreativeFitness | None = None


class HypothesisRanking(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mutation_type: MutationType
    hypothesis_id: str
    score: float
    detection_confidence: float
    reference_support: float | None = None
    policy_mean: float | None = None
    policy_wins: int = 0
    policy_losses: int = 0
    policy_neutral: int = 0
    reason: str = ""


class ExperimentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: str  # winner | no_clear_winner | insufficient_evidence | all_rejected
    winner_arm_id: str | None = None
    reason: str
    primary_metric: str
    per_arm: dict[str, str] = Field(default_factory=dict)  # arm id -> win | loss | neutral | rejected


class CreativeExperiment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    video_id: str
    generation: int = 1
    question: str
    policy_mode: str  # learned | none
    policy_version_before: int
    policy_version_after: int | None = None
    corpus_version: int | None = None
    genome_id: str
    hypotheses: list[CreativeHypothesis]
    ranking: list[HypothesisRanking]
    arms: list[ExperimentArm]
    suite_id: str
    suite_hash: str
    decision: ExperimentDecision | None = None
    policy_updates: list[dict[str, Any]] = Field(default_factory=list)
    human_test_plan: list[dict[str, str]] = Field(default_factory=list)
    timings_ms: dict[str, int] = Field(default_factory=dict)
    model_calls: int = 0
    weave_url: str | None = None
    weave_call_id: str | None = None
    created_at: str = ""
    status: str = "running"  # running | completed | failed
    notes: list[str] = Field(default_factory=list)


class PairwiseReview(BaseModel):
    """One blinded human answer. Which arm is A or B is randomized per assignment and never shown."""

    model_config = ConfigDict(extra="forbid")

    review_id: str
    experiment_id: str
    test_type: str  # hook | full
    arm_left: str
    arm_right: str
    shown_order: list[str]
    choice: str  # left | right | none
    chosen_arm_id: str | None
    response_text: str = ""
    session_hash: str
    created_at: str
