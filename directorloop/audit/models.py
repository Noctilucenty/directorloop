"""Cold-audience audit records: coverage, window reactions, findings, strengths, audience predictions, repairs.

Observed facts and interpretations are separate fields. Every prediction is labeled a model judgment. Coverage says
exactly what the reviewer received, so a sampled-frames-plus-transcript review is never presented as frame-by-frame
audiovisual viewing.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..observability.workflow import WorkflowStage

Severity = Literal["low", "medium", "high"]
Verdict = Literal["YES", "MAYBE", "NO", "INSUFFICIENT_EVIDENCE"]

ISSUE_TYPES = [
    "unclear_opening_or_subject",
    "missing_context_or_ambiguous_reference",
    "narration_visual_mismatch",
    "unreadable_or_competing_text",
    "repetition_without_new_information",
    "delayed_expected_action",
    "prematurely_resolved_mystery",
    "weak_or_missing_payoff",
    "story_order_undermines_discovery",
    "framing_hides_important_action",
    "weak_emotional_or_social_relevance",
    "visual_stagnation",
    "other",
]

OBJECTIVES = ["attention", "comprehension", "emotional_payoff", "sharing", "motivation_to_engage", "other"]


class EvidenceFrame(BaseModel):
    model_config = ConfigDict(extra="forbid")

    t_ms: int
    path: str  # local path under data/audit/<audit_id>/frames; the API turns it into a URL


class InspectedWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["prefix", "precision", "diagnostic", "closeup"]
    start_ms: int
    end_ms: int
    frame_timestamps_ms: list[int]
    frame_width: int
    context_frames: int = 0  # earlier-moment thumbnails included with a prefix window
    context_width: int = 0
    context_frame_timestamps_ms: list[int] | None = None  # None on older records that stored only the count
    transcript_words: int = 0
    # Information boundary of a prefix or precision review (None on records written before it was recorded):
    # the latest frame request, the end of the last word shown, and the boundary both must respect.
    latest_frame_ms: int | None = None
    latest_word_end_ms: int | None = None
    boundary_ms: int | None = None


class CoverageRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["sampled_frames_plus_asr", "native_video", "transcript_only"]
    duration_ms: int
    windows: list[InspectedWindow] = Field(default_factory=list)
    total_frames_sent: int = 0
    transcript_source: str = ""
    audio_inspection: str = ""
    provider: str = ""
    model: str = ""
    limitations: list[str] = Field(default_factory=list)
    cold_viewer_prompt_version: str = "cold-viewer-v1"  # records written before versioning used the v1 prompt

    def covered_ms(self, kind: str = "prefix") -> int:
        spans = sorted((w.start_ms, w.end_ms) for w in self.windows if w.kind == kind)
        total, cur_s, cur_e = 0, None, None
        for s, e in spans:
            if cur_e is None or s > cur_e:
                if cur_e is not None:
                    total += cur_e - cur_s  # type: ignore[operator]
                cur_s, cur_e = s, e
            else:
                cur_e = max(cur_e, e)
        if cur_e is not None:
            total += cur_e - cur_s  # type: ignore[operator]
        return total


class WindowReaction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_ms: int
    end_ms: int
    understanding: str = ""
    expectation: str = ""
    open_question: str = ""
    reaction: Literal["engaged", "neutral", "losing_interest", "confused", "unknown"] = "unknown"
    attention_risk: Literal["low", "medium", "high", "unknown"] = "unknown"
    cause: str = ""
    label: str = "MODEL JUDGMENT (predicted viewer reaction)"
    latency_ms: int = 0
    error: str | None = None
    scan: Literal["coarse", "precision"] = "coarse"
    # What the viewer says about a set-up it is waiting on, from the same call ("unknown" on older records).
    payoff: Literal["none", "waiting", "delivered", "unknown"] = "unknown"
    waiting_since_ms: int | None = None  # model-reported time the awaited set-up began; never later than end_ms
    input_tokens: int | None = None
    output_tokens: int | None = None


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["visual", "audio", "caption", "mechanical"]
    text: str
    t_ms: int | None = None


class AuditRepair(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    route: Literal["A", "B", "C"]
    runnable: bool = False
    mutation_type: str | None = None
    edit_description: str = ""
    keep_unchanged: list[str] = Field(default_factory=list)
    required_materials: list[str] = Field(default_factory=list)
    dependency_state: str = ""
    secondary_changes: list[str] = Field(default_factory=list)
    why_not_runnable: str | None = None
    selection_reason: str = ""  # the selector's stated reason for this choice (or for choosing nothing)
    implements_proposal: bool = True  # False: an executable edit that addresses the weakness by a different means than the reviewer proposed
    proposal_route: Literal["A", "B", "C"] | None = None  # the route the reviewer's own proposal needs
    proposal_needs: str = ""  # what the reviewer's own proposal would need when it cannot be executed
    candidates_offered: list[str] = Field(default_factory=list)  # validated existing-video edits the selector could choose from


class AuditFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    version_id: str
    start_ms: int
    end_ms: int
    evidence_frames: list[EvidenceFrame] = Field(default_factory=list)
    observed: list[Observation] = Field(default_factory=list)
    viewer_understanding: str = ""
    predicted_reaction: str = ""
    weakness: str = ""
    issue_type: str = "other"
    alternatives: list[str] = Field(default_factory=list)
    objective: str = "attention"
    severity: Severity = "medium"
    uncertainty: Severity = "medium"
    proposed_repair: str = ""
    repair_kind: str = "other"  # the reviewer's view of what kind of change is needed; feasibility is decided separately
    keep_unchanged: list[str] = Field(default_factory=list)
    repair: AuditRepair | None = None
    verified_closeup: bool = False
    closeup_note: str = ""


class Strength(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    start_ms: int
    end_ms: int
    what: str
    why: str
    evidence_frames: list[EvidenceFrame] = Field(default_factory=list)


class AudienceVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Verdict = "INSUFFICIENT_EVIDENCE"
    reason: str = ""
    at_ms: int | None = None
    to_whom: str | None = None


class AudienceResponses(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = "MODEL JUDGMENT (predicted), not measured audience behavior"
    stop: AudienceVerdict = Field(default_factory=AudienceVerdict)
    continue_watching: AudienceVerdict = Field(default_factory=AudienceVerdict)
    finish: AudienceVerdict = Field(default_factory=AudienceVerdict)
    like: AudienceVerdict = Field(default_factory=AudienceVerdict)
    send: AudienceVerdict = Field(default_factory=AudienceVerdict)
    comment: AudienceVerdict = Field(default_factory=AudienceVerdict)
    save_or_replay: AudienceVerdict = Field(default_factory=AudienceVerdict)
    visit_creator: AudienceVerdict = Field(default_factory=AudienceVerdict)


# ----------------------------------------------------------------------------- temporal attention (model-predicted)
#
# Everything below is derived deterministically from WindowReaction records. Risk levels are ordinal model predictions
# (low < medium < high), never probabilities, retention rates or measured drop-off.

AttentionLevel = Literal["low", "medium", "high"]
ATTENTION_LABEL = "MODEL-PREDICTED cold-viewer attention risk from prefix-only reviews; not measured audience retention"
# compare-v2: whole-video and protected-content frames show the same source moments in both versions, and a target moment the
# original wins in the pairwise judgment counts as a regression.
# compare-v3: a fresh finding continues the target weakness only through the same specific issue type or the same objective; the
# catch-all issue type "other" never matches by itself. Outcomes from older comparisons are not admitted as policy priors.
COMPARISON_VERSION = "compare-v3"


class TimeRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_ms: int
    end_ms: int


class ViewerState(BaseModel):
    """One window's reading, as the cold viewer gave it."""

    model_config = ConfigDict(extra="forbid")

    start_ms: int
    end_ms: int
    scan: Literal["coarse", "precision"] = "coarse"
    reaction: str = "unknown"
    attention_risk: str = "unknown"
    understanding: str = ""
    expectation: str = ""
    open_question: str = ""
    cause: str = ""
    payoff: str = "unknown"


class AttentionEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["initial_high_risk", "initial_elevated_risk", "risk_increase", "predicted_dropoff", "interest_loss", "confusion_onset",
                  "recovery", "unresolved_expectation", "payoff_delivered"]
    start_ms: int
    end_ms: int
    resolution: Literal["coarse", "precision"]
    severity: AttentionLevel
    from_state: str | None = None  # "low / engaged"
    to_state: str | None = None
    evidence: list[str] = Field(default_factory=list)
    note: str = ""


class ExpectationThread(BaseModel):
    """Consecutive windows in which the viewer said it was still waiting on the same set-up (by its reported set-up time)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    set_up_ms: int | None = None  # the viewer's own reported time; None when not reported
    first_waiting: TimeRange
    last_waiting: TimeRange
    resolved: TimeRange | None = None  # first later window that reports the set-up delivered
    expectations: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    elevated_while_waiting: TimeRange | None = None  # first window in the thread whose risk is above low


class FailureRegion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    start_ms: int  # the symptom: consecutive reviewed windows above low risk
    end_ms: int
    failure_type: Literal["hook_failure", "interest_loss", "confusion", "interest_loss_and_confusion", "elevated_risk"]
    severity: AttentionLevel  # highest predicted risk inside the region
    onset: TimeRange  # where deterioration began, at the finest resolution that reviewed it
    high_risk_onset: TimeRange | None = None
    temporal_resolution_ms: int
    refined_by_precision: bool = False
    viewer_before: ViewerState | None = None
    viewer_during: list[ViewerState] = Field(default_factory=list)
    viewer_after: ViewerState | None = None
    recovered: bool = False
    recovery: TimeRange | None = None
    expectation: str = ""
    open_question: str = ""
    cause: str = ""
    cause_region: TimeRange | None = None  # only when the viewer reported when the unresolved set-up began
    cause_basis: str = ""
    later_content_needs_survival: bool = False  # a recovery after high risk is only seen by viewers who did not scroll away
    finding_ids: list[str] = Field(default_factory=list)
    label: str = ATTENTION_LABEL


class PrecisionScan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    trigger: str
    trigger_window: TimeRange
    region: TimeRange
    window_ms: int
    windows_planned: int = 0
    windows_reviewed: int = 0
    status: Literal["complete", "partial", "failed", "skipped"] = "complete"
    errors: list[str] = Field(default_factory=list)
    skipped_reason: str = ""
    refined_onset: TimeRange | None = None


class RiskSegment(BaseModel):
    """Graph-ready, non-overlapping pieces of the timeline at the finest reviewed resolution."""

    model_config = ConfigDict(extra="forbid")

    start_ms: int
    end_ms: int
    scan: Literal["coarse", "precision"]
    attention_risk: str  # low | medium | high | unknown
    risk_ordinal: int | None = None  # 0, 1, 2: ordering only
    predicted_attention_score: float | None = None  # 1 - ordinal / 2, for drawing; not a probability or retention rate
    reaction: str = "unknown"


class AttentionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    windows_reviewed: int = 0
    windows_failed: int = 0
    precision_windows_reviewed: int = 0
    precision_scans_triggered: int = 0
    peak_risk: AttentionLevel | None = None
    starts_elevated: bool = False
    ends_elevated: bool = False
    first_risk_increase: TimeRange | None = None
    first_high_risk: TimeRange | None = None
    peak_regions: list[TimeRange] = Field(default_factory=list)
    recovery_regions: list[TimeRange] = Field(default_factory=list)
    predicted_dropoff_regions: list[TimeRange] = Field(default_factory=list)
    text: str = ""


class EvaluatorRecord(BaseModel):
    """What produced the predictions, so two timelines can be checked for comparability."""

    model_config = ConfigDict(extra="forbid")

    cold_viewer_prompt_version: str
    provider: str = ""
    model: str = ""
    reasoning_effort: str | None = None
    coarse_window_ms: int
    precision_enabled: bool
    precision_window_ms: int
    precision_lead_ms: int
    max_precision_regions: int
    max_precision_windows: int
    frame_sampling: str = ""
    asr_language: str = "en"


class AttentionTimeline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = ATTENTION_LABEL
    duration_ms: int
    evaluator: EvaluatorRecord
    precision_scans: list[PrecisionScan] = Field(default_factory=list)
    segments: list[RiskSegment] = Field(default_factory=list)
    events: list[AttentionEvent] = Field(default_factory=list)
    failure_regions: list[FailureRegion] = Field(default_factory=list)
    expectation_threads: list[ExpectationThread] = Field(default_factory=list)
    summary: AttentionSummary = Field(default_factory=AttentionSummary)


class RiskChange(BaseModel):
    """One candidate segment whose predicted risk differs from the original moment it maps to."""

    model_config = ConfigDict(extra="forbid")

    start_ms: int  # candidate time
    end_ms: int
    before: AttentionLevel
    after: AttentionLevel
    original_start_ms: int
    original_end_ms: int


class AttentionComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = "Deterministic comparison of two MODEL-PREDICTED attention timelines; not measured retention"
    comparable: bool = False
    comparability_notes: list[str] = Field(default_factory=list)
    target_region: TimeRange | None = None  # original time
    target_region_in_candidate: TimeRange | None = None
    target_peak_before: str | None = None
    target_peak_after: str | None = None
    target_region_improved: bool | None = None
    elevated_ms_before: int = 0
    elevated_ms_after: int = 0
    high_ms_before: int = 0
    high_ms_after: int = 0
    first_deterioration_before_ms: int | None = None
    first_deterioration_after_ms: int | None = None  # candidate time
    new_regressions: list[str] = Field(default_factory=list)
    improvements: list[str] = Field(default_factory=list)
    regression_changes: list[RiskChange] = Field(default_factory=list)
    improvement_changes: list[RiskChange] = Field(default_factory=list)
    global_direction: Literal["improved", "regressed", "mixed", "unchanged", "insufficient_evidence"] = "insufficient_evidence"
    decision_evidence: list[str] = Field(default_factory=list)


class CallUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    calls: int = 0
    failed: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0


class AuditReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    video_id: str
    version_id: str
    artifact_hash: str
    artifact_path: str
    duration_ms: int
    created_at: str
    status: Literal["complete", "incomplete"] = "complete"
    coverage: CoverageRecord
    window_reactions: list[WindowReaction] = Field(default_factory=list)  # the coarse scan, unchanged in shape
    precision_reactions: list[WindowReaction] = Field(default_factory=list)
    attention: AttentionTimeline | None = None
    call_usage: dict[str, CallUsage] = Field(default_factory=dict)  # coarse, precision, diagnosis, closeup
    findings: list[AuditFinding] = Field(default_factory=list)
    strengths: list[Strength] = Field(default_factory=list)
    audience: AudienceResponses = Field(default_factory=AudienceResponses)
    overall_summary: str = ""
    weave_url: str | None = None
    weave_call_id: str | None = None
    timings_ms: dict[str, int] = Field(default_factory=dict)
    model_calls: int = 0
    notes: list[str] = Field(default_factory=list)
    incomplete_reasons: list[str] = Field(default_factory=list)
    primed_with: list[str] = Field(default_factory=list)  # must stay empty for the cold review
    workflow_stages: list[WorkflowStage] = Field(default_factory=list)


class IntervalMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    orig_start_ms: int
    orig_end_ms: int
    new_start_ms: int | None
    new_end_ms: int | None
    note: str = ""


class ChangeVerification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intended: str
    verified: bool
    checks: list[str] = Field(default_factory=list)


class StabilityRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runs: int
    agreeing_runs: int
    order_flips: int
    agreement: str


class AuditComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    improved: list[str] = Field(default_factory=list)
    regressed: list[str] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)
    target_resolved: Literal["yes", "no", "unclear"] = "unclear"
    target_evidence: list[str] = Field(default_factory=list)
    new_weaknesses: list[str] = Field(default_factory=list)
    outcome: Literal["improvement", "regression", "mixed", "tie", "insufficient_evidence"] = "insufficient_evidence"
    target_preference: float | None = None
    full_preference: float | None = None
    stability: StabilityRecord | None = None
    label: str = "MODEL JUDGMENT: fresh reviewer, same frozen criteria, both presentation orders"
    notes: list[str] = Field(default_factory=list)
    attention: AttentionComparison | None = None
    comparison_version: str = ""  # empty on records made before versioning (compare-v1 behaviour)
    protected_requested: list[str] = Field(default_factory=list)  # every protected item and constraint sent to the check
    protected_checks: list[dict[str, str]] = Field(default_factory=list)  # item, kind, status, evidence as returned
    protected_unchecked: list[str] = Field(default_factory=list)  # requested items the check returned no result for


class RepairRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    audit_id: str
    finding_id: str
    video_id: str
    status: Literal["proposed", "rendered", "reviewed", "accepted", "rejected", "incomplete"] = "proposed"
    hypothesis: str = ""
    repair: AuditRepair
    original_artifact_hash: str
    original_path: str
    candidate_artifact_hash: str | None = None
    candidate_path: str | None = None
    candidate_version_id: str | None = None
    interval_map: list[IntervalMapping] = Field(default_factory=list)
    change_verification: ChangeVerification | None = None
    candidate_audit_id: str | None = None
    comparison: AuditComparison | None = None
    weave_url: str | None = None
    created_at: str = ""
    timings_ms: dict[str, int] = Field(default_factory=dict)
    incomplete_reasons: list[str] = Field(default_factory=list)
    lesson_id: str | None = None
