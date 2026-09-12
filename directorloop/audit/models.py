"""Cold-audience audit records: coverage, window reactions, findings, strengths, audience predictions, repairs.

Observed facts and interpretations are separate fields. Every prediction is labeled a model judgment. Coverage says
exactly what the reviewer received, so a sampled-frames-plus-transcript review is never presented as frame-by-frame
audiovisual viewing.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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

    kind: Literal["prefix", "diagnostic", "closeup"]
    start_ms: int
    end_ms: int
    frame_timestamps_ms: list[int]
    frame_width: int
    context_frames: int = 0  # earlier-moment thumbnails included with a prefix window
    context_width: int = 0
    transcript_words: int = 0


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
    window_reactions: list[WindowReaction] = Field(default_factory=list)
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
