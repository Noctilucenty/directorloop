"""From a detected weakness to a ranked queue of single-variable experiments.

symptom (where predicted attention rises, or a diagnosed weakness)
  -> evidence dossier (viewer reports, diagnosis enums, mechanical measurements, beat structure), each item with an id
  -> competing cause hypotheses, each with the evidence for it and against it
  -> executable single-variable experiments for the hypotheses that have an edit on a flattened file
  -> ranking by evidence, how well the result would separate the hypotheses, isolation, and a policy prior learned from
     earlier experiments under similar conditions
  -> decision: which experiments to run, or not to edit at all

Deterministic: no model calls. Every number below has a stated formula; none is a probability, retention rate or
calibrated confidence. Free-text viewer reports are quoted as evidence but never parsed for meaning.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from ..audit.attention import fmt_range
from ..audit.models import COMPARISON_VERSION, AuditFinding, AuditReport, FailureRegion, TimeRange
from ..creative.signals import VideoSignals, silence_gaps, static_spans
from ..domain.creative import BeatRole, CreativeGenome
from ..domain.edit_plan import EditPlan
from ..domain.ids import new_id, utc_now_iso
from ..observability.weave_ops import traced
from .evidence_admission import legacy_evaluation_receipt

CauseType = Literal["delayed_payoff", "dead_air_after_payoff", "static_visual", "redundant_information", "missing_context"]

CAUSES: dict[str, str] = {
    "delayed_payoff": "The viewer is kept waiting too long for what the video set up",
    "dead_air_after_payoff": "The video keeps running after its payoff with nothing new to see or hear",
    "static_visual": "The picture stays visually static for too long",
    "redundant_information": "A beat repeats information the viewer already has",
    "missing_context": "The viewer is confused because context is missing or unclear",
}

# Edit types that test each cause on a flattened file. A cause without one can be diagnosed but not tested here.
EDITS_FOR_CAUSE: dict[str, list[str]] = {
    "delayed_payoff": ["MOVE_BEAT_EARLIER", "REMOVE_BEAT"],
    "dead_air_after_payoff": ["TRIM_PAUSE"],
    "static_visual": ["PUNCH_IN"],
    "redundant_information": ["REMOVE_BEAT"],
    "missing_context": [],
}

# Structured diagnosis enums (the reviewer's own issue and repair kinds) and the causes they point to.
ISSUE_CAUSES: dict[str, list[str]] = {
    "delayed_expected_action": ["delayed_payoff"], "story_order_undermines_discovery": ["delayed_payoff"], "prematurely_resolved_mystery": [],
    "visual_stagnation": ["static_visual"], "framing_hides_important_action": ["static_visual"],
    "repetition_without_new_information": ["redundant_information"],
    "missing_context_or_ambiguous_reference": ["missing_context"], "narration_visual_mismatch": ["missing_context"],
    "unreadable_or_competing_text": ["missing_context"], "unclear_opening_or_subject": ["missing_context"],
}
REPAIR_KIND_CAUSES: dict[str, list[str]] = {
    "trim_or_tighten": ["dead_air_after_payoff", "delayed_payoff"], "pause_adjust": ["dead_air_after_payoff", "delayed_payoff"], "reorder": ["delayed_payoff"],
    "remove_segment": ["redundant_information"], "reframe_or_zoom": ["static_visual"], "caption_or_text": ["missing_context"], "narration_rewrite": ["missing_context"],
}

STRENGTH_LEVEL = {"high": 2, "moderate": 1, "low": 0}
GAIN_LEVEL = {"high": 1.0, "medium": 0.5, "low": 0.0}
OUTCOME_VALUE = {"win": 1.0, "neutral": 0.0, "loss": -1.0, "rejected": -1.0}
PRIORITY_FORMULA = "priority = 2 x evidence level (high 2, moderate 1, low 0) + information gain (high 1, medium 0.5, low 0) + 1.5 x policy prior (-1..1) + 0.5 if single-variable"
EVIDENCE_FORMULA = "evidence share = supporting items / (supporting + contradicting items + 1); high = 3+ supporting and none against, moderate = 2+ supporting and more for than against"
PRIOR_FORMULA = "policy prior = similarity-weighted mean of earlier outcomes for the same cause and edit type (win +1, neutral 0, loss or rejected -1), over records whose conditions match at least half"
MIN_SIMILARITY = 0.5


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    source: Literal["viewer_report", "diagnosis", "mechanical", "structure"]
    text: str
    start_ms: int | None = None
    end_ms: int | None = None
    supports: list[str] = Field(default_factory=list)
    against: list[str] = Field(default_factory=list)


class Symptom(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["predicted_attention_rise", "diagnosed_weakness"]
    start_ms: int
    end_ms: int
    onset: TimeRange
    severity: str
    resolution_ms: int
    description: str
    region_id: str | None = None
    finding_ids: list[str] = Field(default_factory=list)


class OpenLoopView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    established_ms: int | None
    expectation: str = ""
    still_open_at_ms: list[int] = Field(default_factory=list)
    resolved_ms: int | None = None
    risk_rises_at_ms: int | None = None
    delay_to_payoff_ms: int | None = None


class Dossier(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audit_id: str
    video_id: str
    duration_ms: int
    symptom: Symptom
    evidence: list[EvidenceItem]
    open_loops: list[OpenLoopView] = Field(default_factory=list)
    beats: list[dict[str, Any]] = Field(default_factory=list)  # semantic beat timing: role, start_ms, end_ms, text
    conditions: dict[str, Any] = Field(default_factory=dict)


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    cause_type: str
    statement: str
    status: Literal["supported", "contradicted"] = "supported"  # contradicted: no supporting item and at least one against
    evidence_for: list[str] = Field(default_factory=list)
    evidence_against: list[str] = Field(default_factory=list)
    evidence_share: float = 0.0
    strength: Literal["high", "moderate", "low"] = "low"
    why_not_high: list[str] = Field(default_factory=list)
    testable: bool = False
    testable_edits: list[str] = Field(default_factory=list)


class PriorSupport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: float = 0.0
    records: int = 0
    matches: list[dict[str, Any]] = Field(default_factory=list)  # every contributing record: id, video, source file, run, outcome, weight
    excluded: list[dict[str, Any]] = Field(default_factory=list)  # same cause and edit type, but not admitted, with the reason


class Experiment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str  # assigned after ranking; compare interventions across plans by intervention_id
    hypothesis_id: str
    cause_type: str
    also_tests: list[str] = Field(default_factory=list)  # other hypotheses whose best available edit is this same executable plan
    also_causes: list[str] = Field(default_factory=list)
    intervention_id: str = ""  # edit key @ plan hash prefix: stable identity of the executable edit
    edit_key: str
    plan_hash: str = ""
    mutation_type: str
    description: str
    changes: list[str] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)
    side_effects: list[str] = Field(default_factory=list)  # consequences of the edit that the plan diff does not name (from the edit builder)
    isolation: Literal["single-variable", "multi-variable"] = "single-variable"
    information_gain: Literal["high", "medium", "low"] = "medium"
    discriminating: bool = True  # False when the same edit also fits another supported cause, so its result cannot separate them
    evidence_strength: str = "low"
    prior: PriorSupport = Field(default_factory=PriorSupport)
    already_tested_here: list[dict[str, Any]] = Field(default_factory=list)  # earlier outcomes of this cause and edit type on this exact file
    priority: float = 0.0
    priority_without_policy: float = 0.0
    rank: int = 0
    rank_without_policy: int = 0


class PolicyEffect(BaseModel):
    """What the policy prior changed, as separate facts: scores, order, the selected set, and the first experiment."""

    model_config = ConfigDict(extra="forbid")

    records_used: list[str] = Field(default_factory=list)
    score_changes: list[str] = Field(default_factory=list)
    rank_changes: list[str] = Field(default_factory=list)
    order_changed: bool = False
    selection_changed: bool = False
    first_choice_changed: bool = False
    first_choice_with_policy: str | None = None  # intervention ids
    first_choice_without_policy: str | None = None
    summary: str = ""


class ExperimentPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hypotheses: list[Hypothesis]
    experiments: list[Experiment]
    decision: Literal["run_experiments", "do_not_edit"]
    decision_reason: str
    chosen: list[str] = Field(default_factory=list)  # experiment ids in run order
    chosen_without_policy: list[str] = Field(default_factory=list)  # what the same evidence would select with no policy prior
    policy_effect: PolicyEffect = Field(default_factory=PolicyEffect)
    uncertainty: str = ""
    best_discriminating_test: str = ""
    formulas: dict[str, str] = Field(default_factory=lambda: {"evidence": EVIDENCE_FORMULA, "priority": PRIORITY_FORMULA, "prior": PRIOR_FORMULA})


# ----------------------------------------------------------------------------- dossier


def _overlap(a0: int, a1: int, b0: int, b1: int) -> int:
    return max(0, min(a1, b1) - max(a0, b0))


def choose_symptom(audit: AuditReport) -> tuple[Symptom, FailureRegion | None, AuditFinding | None] | None:
    """The weakness to explain: the most severe predicted attention rise (earliest first), else the strongest diagnosed finding."""
    sev = {"high": 2, "medium": 1, "low": 0}
    regions = audit.attention.failure_regions if audit.attention else []
    if regions:
        r = sorted(regions, key=lambda x: (-sev.get(x.severity, 0), x.start_ms))[0]
        finding = next((f for f in sorted(audit.findings, key=lambda f: -_overlap(f.start_ms, f.end_ms, r.start_ms, r.end_ms)) if _overlap(f.start_ms, f.end_ms, r.start_ms, r.end_ms) > 0), None)
        before = r.viewer_before.attention_risk if r.viewer_before else "none"
        desc = (f"predicted attention risk rises {before} -> {r.severity} with onset {fmt_range(r.onset.start_ms, r.onset.end_ms)}" if r.start_ms > 0
                else f"the video opens at {r.severity} predicted attention risk")
        return Symptom(kind="predicted_attention_rise", start_ms=r.start_ms, end_ms=r.end_ms, onset=r.onset, severity=r.severity, resolution_ms=r.temporal_resolution_ms,
                       description=desc, region_id=r.id, finding_ids=[f.id for f in audit.findings if _overlap(f.start_ms, f.end_ms, r.start_ms, r.end_ms) > 0]), r, finding
    ranked = sorted(audit.findings, key=lambda f: (-sev.get(f.severity, 0), {"low": 0, "medium": 1, "high": 2}.get(f.uncertainty, 1), f.start_ms))
    if not ranked:
        return None
    f = ranked[0]
    return Symptom(kind="diagnosed_weakness", start_ms=f.start_ms, end_ms=f.end_ms, onset=TimeRange(start_ms=f.start_ms, end_ms=f.end_ms), severity=f.severity,
                   resolution_ms=f.end_ms - f.start_ms, description=f"diagnosed weakness at {fmt_range(f.start_ms, f.end_ms)}: {f.issue_type}", finding_ids=[f.id]), None, f


def build_dossier(audit: AuditReport, genome: CreativeGenome | None, signals: VideoSignals | None, category: str = "educational_short") -> Dossier | None:
    chosen = choose_symptom(audit)
    if chosen is None:
        return None
    symptom, region, finding = chosen
    s, e, onset = symptom.start_ms, symptom.end_ms, symptom.onset.start_ms
    items: list[EvidenceItem] = []

    def add(source: str, text: str, supports: Iterable[str] = (), against: Iterable[str] = (), start: int | None = None, end: int | None = None) -> None:
        items.append(EvidenceItem(id=f"e{len(items) + 1}", source=source, text=text, start_ms=start, end_ms=end, supports=list(dict.fromkeys(supports)),  # type: ignore[arg-type]
                                  against=list(dict.fromkeys(against))))

    timeline = audit.attention
    readings = sorted(audit.window_reactions + audit.precision_reactions, key=lambda r: (r.start_ms, r.end_ms - r.start_ms))
    in_region = [r for r in readings if not r.error and r.start_ms < e and r.end_ms > s]
    # viewer reports: the set-up the viewer was waiting on, and whether the payoff had already landed
    loops: list[OpenLoopView] = []
    waiting_thread = None
    if timeline is not None:
        for th in timeline.expectation_threads:
            open_at = [r.end_ms for r in readings if r.payoff == "waiting" and r.waiting_since_ms is not None and th.first_waiting.start_ms <= r.start_ms <= th.last_waiting.start_ms]
            loops.append(OpenLoopView(established_ms=th.set_up_ms, expectation=th.expectations[0] if th.expectations else "", still_open_at_ms=sorted(set(open_at))[:8],
                                      resolved_ms=th.resolved.start_ms if th.resolved else None,
                                      risk_rises_at_ms=onset if th.first_waiting.start_ms <= onset <= max(th.last_waiting.end_ms, th.first_waiting.end_ms) else None,
                                      delay_to_payoff_ms=(th.resolved.start_ms - th.set_up_ms) if th.resolved and th.set_up_ms is not None else None))
            if th.set_up_ms is not None and th.first_waiting.start_ms <= onset < max(th.last_waiting.end_ms, th.first_waiting.end_ms) and (th.resolved is None or th.resolved.start_ms >= onset):
                waiting_thread = th
        delivered = [ev for ev in timeline.events if ev.type == "payoff_delivered" and ev.start_ms < onset]
    else:
        delivered = []
    if waiting_thread is not None:
        add("viewer_report", f"at the onset the viewer was still waiting on a set-up it placed at {waiting_thread.set_up_ms / 1000:.1f}s: \"{(waiting_thread.expectations or [''])[0][:140]}\"",
            supports=["delayed_payoff"], against=["dead_air_after_payoff"], start=waiting_thread.set_up_ms, end=onset)
    if delivered:
        last = delivered[-1]
        add("viewer_report", f"the viewer said the payoff was delivered at {fmt_range(last.start_ms, last.end_ms)}, before the rise at {fmt_range(symptom.onset.start_ms, symptom.onset.end_ms)}",
            supports=["dead_air_after_payoff"], against=["delayed_payoff"], start=last.start_ms, end=last.end_ms)
    confused = [r for r in in_region if r.reaction == "confused"]
    if confused:
        add("viewer_report", f"the viewer reported confusion at {', '.join(fmt_range(r.start_ms, r.end_ms) for r in confused[:3])}", supports=["missing_context"], start=confused[0].start_ms, end=confused[-1].end_ms)
    elif in_region and all(r.understanding for r in in_region):
        add("viewer_report", "no confusion reported during the symptom, and the viewer states what the video is about in every reading", against=["missing_context"], start=s, end=e)
    onset_reading = next((r for r in in_region if r.start_ms == symptom.onset.start_ms), in_region[0] if in_region else None)
    if onset_reading is not None and onset_reading.cause:
        add("viewer_report", f"quoted cause at the onset (not classified): \"{onset_reading.cause[:160]}\"", start=onset_reading.start_ms, end=onset_reading.end_ms)

    # mechanical measurements of the rendered file
    static_flag: bool | None = None
    silence_flag: bool | None = None
    if signals is not None:
        spans = [sp for sp in static_spans(signals.cuts_ms, signals.motion, signals.duration_ms) if _overlap(sp[0], sp[1], s, e) > 0]
        covered = sum(_overlap(a, b, s, e) for a, b in spans)
        if signals.motion:
            static_flag = covered >= min(600, (e - s) // 2)
            if static_flag:
                add("mechanical", f"no cut and little motion for {covered / 1000:.1f}s of the symptom ({', '.join(fmt_range(a, b) for a, b in spans[:2])})", supports=["static_visual"], start=spans[0][0], end=spans[0][1])
            else:
                add("mechanical", "the picture changes during the symptom (no long still stretch overlaps it)", against=["static_visual"], start=s, end=e)
        gaps = silence_gaps(signals.rms, max(0, s - 300), e, min_gap_ms=300) if len(signals.rms) else []
        quiet = sum(_overlap(a, b, s, e) for a, b in gaps)
        if len(signals.rms):
            silence_flag = bool(gaps) and quiet >= 300
        if gaps and quiet >= 300:
            add("mechanical", f"near-silent audio for {quiet / 1000:.1f}s in the symptom ({', '.join(fmt_range(a, b) for a, b in gaps[:2])})",
                supports=["dead_air_after_payoff"] if delivered else (["delayed_payoff"] if waiting_thread is not None else []), start=gaps[0][0], end=gaps[-1][1])
        elif len(signals.rms):
            add("mechanical", "sound continues through the symptom (no near-silent stretch of 300 ms or more)", against=["dead_air_after_payoff"], start=s, end=e)

    # the diagnosis: its structured issue and repair kinds, never its prose
    if finding is not None:
        causes = ISSUE_CAUSES.get(finding.issue_type, []) + REPAIR_KIND_CAUSES.get(finding.repair_kind, [])
        # A repair kind that fits both payoff causes (trim or pause) supports only the one consistent with the viewer's own payoff state.
        ambiguous = REPAIR_KIND_CAUSES.get(finding.repair_kind, []) if set(REPAIR_KIND_CAUSES.get(finding.repair_kind, [])) >= {"dead_air_after_payoff", "delayed_payoff"} else []

        def consistent(c: str) -> bool:
            if c not in ambiguous or c in ISSUE_CAUSES.get(finding.issue_type, []):
                return True
            if c == "dead_air_after_payoff":
                return not (waiting_thread is not None and not delivered)
            return not (delivered and waiting_thread is None)

        if causes or finding.issue_type:
            add("diagnosis", f"the diagnosis at {fmt_range(finding.start_ms, finding.end_ms)} labels it {finding.issue_type} with repair kind {finding.repair_kind}",
                supports=[c for c in causes if consistent(c)], start=finding.start_ms, end=finding.end_ms)

    # beat structure (sentence beats with model-labelled roles)
    beats: list[dict[str, Any]] = []
    if genome is not None and genome.beats:
        beats = [{"id": b.id, "role": b.role.value, "start_ms": b.start_ms, "end_ms": b.end_ms, "text": b.text[:80], "redundant_with": b.redundant_with_beat_id} for b in genome.beats]
        speech_end = max(b.end_ms for b in genome.beats)
        if onset >= speech_end - 100:
            add("structure", f"the last sentence ends at {speech_end / 1000:.1f}s, before the rise at {symptom.onset.start_ms / 1000:.1f}s", supports=["dead_air_after_payoff"], against=["redundant_information"],
                start=speech_end, end=e)
        at = next((b for b in genome.beats if b.start_ms <= onset < b.end_ms), None)
        if at is not None and at.redundant_with_beat_id:
            add("structure", f"the beat at the onset ({at.role.value}, {fmt_range(at.start_ms, at.end_ms)}) repeats {at.redundant_with_beat_id}", supports=["redundant_information"], start=at.start_ms, end=at.end_ms)
        before = [b for b in genome.beats if b.end_ms <= onset + 200]
        run = 0
        for b in reversed(before):
            if b.role in (BeatRole.CONTEXT, BeatRole.SETUP):
                run += 1
            else:
                break
        payoff_later = any(b.role in (BeatRole.PAYOFF, BeatRole.PROOF, BeatRole.MECHANISM) and b.start_ms >= onset for b in genome.beats)
        if run >= 2 and payoff_later:
            add("structure", f"{run} context or setup beats in a row lead into the rise, and the payoff beat comes later", supports=["delayed_payoff"], start=before[-run].start_ms, end=onset)

    flags: dict[str, bool | None] = {
        "payoff_delivered_before_symptom": bool(delivered) if timeline is not None else None,
        "open_loop_unresolved_at_symptom": (waiting_thread is not None) if timeline is not None else None,
        "static_picture_at_symptom": static_flag,
        "silence_at_symptom": silence_flag,
        "confusion_at_symptom": bool(confused) if in_region else None,
    }
    conditions = derive_conditions(audit, symptom, flags, genome, category)
    return Dossier(audit_id=audit.id, video_id=audit.video_id, duration_ms=audit.duration_ms, symptom=symptom, evidence=items, open_loops=loops, beats=beats, conditions=conditions)


def derive_conditions(audit: AuditReport, symptom: Symptom, flags: dict[str, bool | None], genome: CreativeGenome | None, category: str) -> dict[str, Any]:
    """The situation an experiment result applies to, from measured flags. Stored with every policy record and matched on later videos."""
    duration = max(1, audit.duration_ms)
    coarse = audit.attention.evaluator.coarse_window_ms if audit.attention else 2000
    position = "opening" if symptom.onset.start_ms < coarse else ("ending" if symptom.end_ms >= duration * 0.85 else "middle")
    return {
        "category": category,
        "symptom_position": position,
        "symptom_severity": symptom.severity,
        **flags,
        "hook_type": genome.hook.hook_type.value if genome is not None and getattr(genome.hook.hook_type, "value", None) else None,
    }


# ----------------------------------------------------------------------------- hypotheses


@traced(
    "generate_hypotheses", kind="tool",
    display=lambda i: "Generate competing cause hypotheses",
    summarize=lambda hs: [{"id": h.id, "cause": h.cause_type, "status": h.status, "for": h.evidence_for, "against": h.evidence_against, "evidence_share": h.evidence_share,
                           "strength": h.strength, "testable_edits": h.testable_edits} for h in hs],
)
def competing_hypotheses(dossier: Dossier, minimum: int = 2, maximum: int = 4) -> list[Hypothesis]:
    """Every cause with supporting evidence competes; causes the evidence speaks against stay in view as contradicted
    alternatives, so a single explanation is never accepted without its rivals. Causes with no evidence either way are left out."""
    hyps: list[Hypothesis] = []
    for cause, statement in CAUSES.items():
        f = [it.id for it in dossier.evidence if cause in it.supports]
        a = [it.id for it in dossier.evidence if cause in it.against]
        share = round(len(f) / (len(f) + len(a) + 1), 2)
        strength = "high" if len(f) >= 3 and not a else "moderate" if len(f) >= 2 and len(f) > len(a) else "low"
        why = []
        if strength != "high":
            if len(f) < 3:
                why.append(f"only {len(f)} supporting item{'s' if len(f) != 1 else ''}")
            if a:
                why.append(f"counterevidence {', '.join(a)}")
        hyps.append(Hypothesis(id="", cause_type=cause, statement=statement, status="supported" if f else "contradicted", evidence_for=f, evidence_against=a,
                               evidence_share=share, strength=strength, why_not_high=why, testable=bool(EDITS_FOR_CAUSE[cause]), testable_edits=EDITS_FOR_CAUSE[cause]))  # type: ignore[arg-type]
    supported = sorted([h for h in hyps if h.evidence_for], key=lambda h: (-STRENGTH_LEVEL[h.strength], -(len(h.evidence_for) - len(h.evidence_against)), -h.evidence_share))
    considered = sorted([h for h in hyps if not h.evidence_for and h.evidence_against], key=lambda h: -len(h.evidence_against))
    out = supported[:maximum]
    for h in considered:
        if len(out) >= max(minimum, maximum):
            break
        out.append(h)
    for n, h in enumerate(out, start=1):
        h.id = f"H{n}"
    return out


# ----------------------------------------------------------------------------- experiments


def plan_changes(base: EditPlan, candidate: EditPlan) -> tuple[list[str], list[str]]:
    """(what the edit changes, what it leaves unchanged), read from the two plans rather than from the edit's description."""
    from ..audit.revise import removed_source_ranges

    changes: list[str] = []
    base_ids = [s.id for s in base.segments]
    kept_order = [s.id for s in candidate.segments if s.id in base_ids]
    if kept_order != [i for i in base_ids if i in kept_order]:
        changes.append("sentence order (each moved sentence keeps its own footage, captions and sound)")
    removed = removed_source_ranges(base, candidate)
    if removed:
        changes.append("removed " + ", ".join(f"{fmt_range(a, b)} ({(b - a) / 1000:.2f}s)" for a, b in removed[:3]) + " of footage and its sound")
    crops = [s for s in candidate.segments if s.crop is not None]
    if crops:
        changes.append("framing: a punch-in on " + ", ".join(f"{fmt_range(s.source_in_ms, s.source_out_ms)}" for s in crops[:2]))
    speed = any(abs(s.speed - 1.0) > 1e-6 for s in candidate.segments)
    if speed:
        changes.append("playback speed")
    unchanged = [label for label, same in (
        ("words and voice of every remaining sentence", True),
        ("burned-in caption styling", True),
        ("music and sound mix", True),
        ("colour", True),
        ("playback speed", not speed),
        ("framing", not crops),
        ("sentence order", not any(c.startswith("sentence order") for c in changes)),
        ("nothing removed", not removed),
    ) if same]
    return changes, unchanged


def _isolation(changes: list[str]) -> str:
    return "single-variable" if len(changes) <= 1 else "multi-variable"


PAYOFF_ROLES = {"payoff", "proof"}


def edit_fits_cause(cause: str, candidate: Any, beats: list[dict[str, Any]]) -> bool:
    """Whether an edit can test the cause at all. Removing the payoff does not bring the payoff earlier, moving a set-up sentence
    earlier does not either, and removing a sentence tests redundancy only if that sentence repeats another."""
    ops = getattr(candidate, "ops", []) or []
    target = getattr(ops[0], "segment_id", None) if ops else None
    beat = next((b for b in beats if b.get("id") == target), None)
    if beat is None:
        return cause not in ("delayed_payoff", "redundant_information") or candidate.mutation_type not in ("REMOVE_BEAT", "MOVE_BEAT_EARLIER")
    if cause == "delayed_payoff" and candidate.mutation_type == "REMOVE_BEAT":
        later_payoff = [b for b in beats if b["role"] in PAYOFF_ROLES and b["start_ms"] >= beat["end_ms"]]
        return beat["role"] not in PAYOFF_ROLES and bool(later_payoff)
    if cause == "delayed_payoff" and candidate.mutation_type == "MOVE_BEAT_EARLIER":
        return beat["role"] in PAYOFF_ROLES | {"mechanism"}
    if cause == "redundant_information" and candidate.mutation_type == "REMOVE_BEAT":
        return bool(beat.get("redundant_with"))
    return True


def _candidate_target(candidate: Any) -> tuple[int, int] | None:
    ops = getattr(candidate, "ops", []) or []
    for op in ops:
        a, b = getattr(op, "source_in_ms", None), getattr(op, "source_out_ms", None)
        if isinstance(a, int) and isinstance(b, int):
            return a, b
    plan = getattr(candidate, "plan", None)
    if plan is not None:
        crops = [s for s in plan.segments if s.crop is not None]
        if crops:
            return crops[0].source_in_ms, crops[0].source_out_ms
    return None


@traced(
    "rank_experiments", kind="tool",
    display=lambda i: "Rank single-variable experiments",
    summarize=lambda plan: {"decision": plan.decision, "reason": plan.decision_reason, "chosen": plan.chosen, "chosen_without_policy": plan.chosen_without_policy,
                            "policy_effect": plan.policy_effect.summary,
                            "queue": [{"id": x.id, "intervention": x.intervention_id, "tests": [x.hypothesis_id, *x.also_tests], "edit": x.mutation_type,
                                       "priority": x.priority, "priority_without_policy": x.priority_without_policy, "rank": x.rank, "rank_without_policy": x.rank_without_policy,
                                       "information_gain": x.information_gain, "discriminating": x.discriminating, "prior": x.prior.score,
                                       "prior_records": [m["record_id"] for m in x.prior.matches]} for x in plan.experiments]},
)
def plan_experiments(dossier: Dossier, hypotheses: list[Hypothesis], candidates: list[Any], base_plan: EditPlan, policy: ConditionalPolicy | None, arms: int = 2, *,
                     target_artifact_hash: str | None = None, include_mocked: bool = False) -> ExperimentPlan:
    """Rank one executable single-variable edit per testable hypothesis and decide which to run (or not to edit).

    One executable plan is one intervention: when the best available edit for two hypotheses is the same plan, it is a single
    experiment that addresses both and cannot separate them. Policy priors come only from earlier experiments on other source
    files (see prior_support)."""
    experiments: list[Experiment] = []
    by_plan: dict[str, Experiment] = {}
    s, e = dossier.symptom.start_ms, dossier.symptom.end_ms
    supported_rivals = [r for r in hypotheses if r.evidence_for]
    for h in hypotheses:
        if not h.testable or not h.evidence_for:
            continue
        options = [c for c in candidates if c.mutation_type in EDITS_FOR_CAUSE[h.cause_type] and edit_fits_cause(h.cause_type, c, dossier.beats)]
        if not options:
            continue

        def fit(c: Any) -> tuple[int, int]:
            target = _candidate_target(c)
            near = _overlap(target[0], target[1], s - 500, e + 500) if target else 0
            return (-near, len(getattr(c, "ops", []) or []))

        ordered = sorted(options, key=fit)
        fresh = [c for c in ordered if c.plan.content_hash() not in by_plan]
        cand = fresh[0] if fresh else ordered[0]
        phash = cand.plan.content_hash()
        if phash in by_plan:  # the same executable edit as a stronger hypothesis: one intervention, not a second experiment
            shared = by_plan[phash]
            shared.also_tests.append(h.id)
            shared.also_causes.append(h.cause_type)
            shared.information_gain, shared.discriminating = "low", False
            continue
        changes, unchanged = plan_changes(base_plan, cand.plan)
        rivals = [r for r in supported_rivals if r.id != h.id]
        fits_rival = [r for r in rivals if cand.mutation_type in EDITS_FOR_CAUSE[r.cause_type] and edit_fits_cause(r.cause_type, cand, dossier.beats)]
        gain = "low" if fits_rival else "high" if rivals else "medium"
        if policy is not None:
            prior = prior_support(policy, h.cause_type, cand.mutation_type, dossier.conditions, target_artifact_hash=target_artifact_hash, include_mocked=include_mocked)
        else:
            prior = PriorSupport()
        here = [{**x, "same_edit": x.get("plan_hash") == phash} for x in prior.excluded if x.get("reason", "").startswith("same source file")]
        x = Experiment(id="", hypothesis_id=h.id, cause_type=h.cause_type, intervention_id=f"{cand.key}@{phash[:12]}", edit_key=cand.key, plan_hash=phash,
                       mutation_type=cand.mutation_type, description=cand.description, changes=changes, unchanged=unchanged,
                       side_effects=list(getattr(cand, "secondary_changes", []) or []), isolation=_isolation(changes), information_gain=gain,  # type: ignore[arg-type]
                       discriminating=gain != "low", evidence_strength=h.strength, prior=prior, already_tested_here=here)
        by_plan[phash] = x
        experiments.append(x)
    for x in experiments:
        base = 2 * STRENGTH_LEVEL[x.evidence_strength] + GAIN_LEVEL[x.information_gain] + (0.5 if x.isolation == "single-variable" else 0.0)
        x.priority = round(base + 1.5 * x.prior.score, 2)
        x.priority_without_policy = round(base, 2)
    for n, x in enumerate(sorted(experiments, key=lambda z: (-z.priority_without_policy, z.hypothesis_id)), start=1):
        x.rank_without_policy = n
    experiments.sort(key=lambda z: (-z.priority, z.hypothesis_id))
    for n, x in enumerate(experiments, start=1):
        x.rank = n
        x.id = f"X{n}"
    top = hypotheses[0] if hypotheses else None
    uncertainty, test = "", ""
    supported = [h for h in hypotheses if h.evidence_for]
    if len(supported) >= 2:
        a, b = supported[0], supported[1]
        uncertainty = f"{a.id} ({a.cause_type}) and {b.id} ({b.cause_type}) both have supporting evidence; the evidence alone does not separate them"
        xa = next((x for x in experiments if a.id in (x.hypothesis_id, *x.also_tests)), None)
        xb = next((x for x in experiments if b.id in (x.hypothesis_id, *x.also_tests)), None)
        if xa is not None and xa is xb:
            uncertainty += f"; the only available edit for both is the same ({xa.intervention_id}), so no experiment here can separate them"
        elif xa and xb and xa.discriminating and xb.discriminating:
            test = f"run {xa.mutation_type} alone and {xb.mutation_type} alone against the same original"
    elif supported:
        uncertainty = f"only {supported[0].id} ({supported[0].cause_type}) has supporting evidence; its strength is {supported[0].strength}"
    effect = policy_effect(experiments, arms)  # stated for every plan, including one that decides not to edit
    if top is None or not supported:
        return ExperimentPlan(hypotheses=hypotheses, experiments=experiments, decision="do_not_edit", decision_reason="no cause hypothesis has supporting evidence", uncertainty=uncertainty,
                              policy_effect=effect)
    if not any(STRENGTH_LEVEL[h.strength] >= 1 for h in supported):
        return ExperimentPlan(hypotheses=hypotheses, experiments=experiments, decision="do_not_edit", uncertainty=uncertainty, best_discriminating_test=test, policy_effect=effect,
                              decision_reason="every cause hypothesis is weak (at most one supporting item); an edit would not test a defensible explanation")
    if not experiments:
        return ExperimentPlan(hypotheses=hypotheses, experiments=experiments, decision="do_not_edit", uncertainty=uncertainty, policy_effect=effect,
                              decision_reason="no executable single-variable edit on this file tests the supported causes: " + ", ".join(sorted({h.cause_type for h in supported})))
    if not any(STRENGTH_LEVEL[x.evidence_strength] >= 1 for x in experiments):
        strong = [h for h in supported if STRENGTH_LEVEL[h.strength] >= 1]
        return ExperimentPlan(hypotheses=hypotheses, experiments=experiments, decision="do_not_edit", uncertainty=uncertainty, best_discriminating_test=test, policy_effect=effect,
                              decision_reason="no executable single-variable edit on this file tests the best-supported cause"
                              + ("s " if len(strong) > 1 else " ") + ", ".join(f"{h.id} ({h.cause_type}, {h.strength})" for h in strong)
                              + "; the only executable experiments test weakly supported causes ("
                              + ", ".join(f"{x.hypothesis_id} {x.cause_type}" for x in experiments) + "), so a result would not test a defensible explanation")
    if arms <= 0:
        return ExperimentPlan(hypotheses=hypotheses, experiments=experiments, decision="do_not_edit", uncertainty=uncertainty, best_discriminating_test=test,
                              decision_reason="no render budget", policy_effect=effect)
    chosen = select_experiments(experiments, arms)
    return ExperimentPlan(hypotheses=hypotheses, experiments=experiments, decision="run_experiments", decision_reason=selection_reason(chosen), chosen=[x.id for x in chosen],
                          chosen_without_policy=[x.id for x in select_experiments(sorted(experiments, key=lambda z: z.rank_without_policy), arms)],
                          uncertainty=uncertainty, best_discriminating_test=test, policy_effect=effect)


def selection_reason(chosen: list[Experiment]) -> str:
    first = chosen[0]
    reason = f"{first.id} tests {first.hypothesis_id} ({first.cause_type}) with priority {first.priority}"
    if first.also_tests:
        reason += f"; the same edit also addresses {', '.join(f'{h} ({c})' for h, c in zip(first.also_tests, first.also_causes, strict=True))}, so its result cannot tell those apart"
    for x in chosen[1:]:
        if x.discriminating and first.discriminating:
            reason += f"; {x.id} tests the competing {x.hypothesis_id} ({x.cause_type}) with a different edit, so the two results can be told apart"
        else:
            reason += f"; {x.id} tests {x.hypothesis_id} ({x.cause_type}), but an edit here also fits another supported cause, so the results do not separate them"
    return reason


def select_experiments(ordered: list[Experiment], arms: int) -> list[Experiment]:
    """The top experiment, then the best-ranked experiments for causes not yet covered and edits not yet chosen, up to the render budget."""
    chosen: list[Experiment] = []
    causes: set[str] = set()
    plans: set[str] = set()
    for x in ordered:
        if len(chosen) >= max(0, arms):
            break
        mine = {x.cause_type, *x.also_causes}
        if mine & causes or (x.plan_hash and x.plan_hash in plans):
            continue
        chosen.append(x)
        causes |= mine
        plans.add(x.plan_hash)
    return chosen


def policy_effect(experiments: list[Experiment], arms: int) -> PolicyEffect:
    """What the policy prior changed, against the ranking the same evidence gives without it. Score, order, selected set and
    first choice are separate facts; interventions are named by their stable identity, not by their post-ranking ids."""
    records = sorted({m["record_id"] for x in experiments for m in x.prior.matches})
    effect = PolicyEffect(records_used=records)
    effect.score_changes = [f"{x.intervention_id} ({x.mutation_type} for {x.cause_type}): {1.5 * x.prior.score:+.2f} from {x.prior.records} record{'s' if x.prior.records != 1 else ''}"
                            for x in experiments if x.prior.records]
    effect.rank_changes = [f"{x.intervention_id} ({x.mutation_type} for {x.cause_type}): rank {x.rank} with policy, rank {x.rank_without_policy} without"
                           for x in experiments if x.rank != x.rank_without_policy]
    with_policy = [x.intervention_id for x in select_experiments(experiments, arms)]
    without = [x.intervention_id for x in select_experiments(sorted(experiments, key=lambda z: z.rank_without_policy), arms)]
    effect.order_changed = bool(effect.rank_changes)
    effect.selection_changed = set(with_policy) != set(without)
    effect.first_choice_with_policy = with_policy[0] if with_policy else None
    effect.first_choice_without_policy = without[0] if without else None
    effect.first_choice_changed = effect.first_choice_with_policy != effect.first_choice_without_policy
    if not experiments:
        effect.summary = "no eligible experiment, so the policy had nothing to rank"
    elif len(experiments) == 1:
        effect.summary = "only one eligible experiment, so there was no ranking choice for the policy to change"
    elif effect.first_choice_changed:
        effect.summary = f"the policy changed the first experiment: {effect.first_choice_with_policy} instead of {effect.first_choice_without_policy}"
    elif effect.selection_changed:
        effect.summary = f"the policy changed the selected experiments: {with_policy} instead of {without}"
    elif effect.order_changed:
        effect.summary = "the policy reordered the queue without changing the selected experiments"
    elif records:
        effect.summary = "the policy changed priority scores but not the order or the selection"
    else:
        effect.summary = "the policy did not change the ranking: no admitted earlier record matches these conditions for any queued edit"
    return effect


# ----------------------------------------------------------------------------- conditional policy


class PolicyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    created_at: str
    video_id: str
    run_id: str
    experiment_id: str
    cause_type: str
    mutation_type: str
    edit_key: str = ""
    plan_hash: str = ""
    artifact_hash: str = ""  # the original file the experiment ran on; transfer means a different file, not a different id
    variant_artifact_hash: str = ""
    evaluator: dict[str, Any] = Field(default_factory=dict)  # prompt version, provider, model and scan settings of both reviews
    mocked: bool = False  # produced with scripted test providers; never admitted into a real run's prior
    comparison_outcome: str = ""
    comparison_version: str = ""  # outcomes judged by an older comparison are kept but not admitted as priors
    conditions: dict[str, Any]
    outcome: Literal["win", "neutral", "loss", "rejected"]
    evidence: str = ""
    label: str = "MODEL-JUDGED offline experiment outcome under recorded conditions; not audience data"


class ConditionalPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 0
    records: list[PolicyRecord] = Field(default_factory=list)
    evaluation_receipts: dict[str, dict[str, Any]] = Field(default_factory=dict)  # separate from immutable historical record bodies; frozen into each plan


def policy_path(data_dir: Path) -> Path:
    return data_dir / "creative" / "conditional_policy.json"


def load_conditional_policy(path: Path) -> ConditionalPolicy:
    if not path.exists():
        return ConditionalPolicy()
    policy = ConditionalPolicy.model_validate_json(path.read_text(encoding="utf-8"))
    for record in policy.records:
        if record.id not in policy.evaluation_receipts or policy.evaluation_receipts[record.id].get("method") == "saved-run-and-candidate-audit-v1":
            policy.evaluation_receipts[record.id] = legacy_evaluation_receipt(record.model_dump(mode="json"), path.parent.parent)
    return policy


def save_conditional_policy(policy: ConditionalPolicy, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex[:8]}.tmp")
    tmp.write_text(policy.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(path)


@contextlib.contextmanager
def policy_lock(path: Path) -> Iterator[None]:
    """Exclusive lock around load-modify-save, so concurrent runs cannot lose each other's records."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def policy_sha256(policy: ConditionalPolicy) -> str:
    # Empty legacy snapshots retain their historical digest; populated receipts are part of every new frozen policy.
    return hashlib.sha256(policy.model_dump_json(exclude={"evaluation_receipts"} if not policy.evaluation_receipts else None).encode("utf-8")).hexdigest()


def similarity(a: dict[str, Any], b: dict[str, Any]) -> float:
    keys = [k for k in a if k in b and a[k] is not None and b[k] is not None]
    if not keys:
        return 0.0
    return sum(1 for k in keys if a[k] == b[k]) / len(keys)


def prior_support(policy: ConditionalPolicy, cause_type: str, mutation_type: str, conditions: dict[str, Any], *, target_artifact_hash: str | None = None,
                  include_mocked: bool = False) -> PriorSupport:
    """Admitted records: same cause and edit type, conditions matching at least half, from a different source file than the one
    being planned (a renamed copy of the same bytes is the same file), and not from scripted test runs unless this run is one.
    Every contributing record is listed with its weight; records of the same cause and edit that were not admitted are listed
    with the reason."""
    matches: list[tuple[PolicyRecord, float]] = []
    excluded: list[dict[str, Any]] = []
    for rec in policy.records:
        if rec.cause_type != cause_type or rec.mutation_type != mutation_type:
            continue
        ref = {"record_id": rec.id, "video_id": rec.video_id, "run_id": rec.run_id, "outcome": rec.outcome}
        if rec.mocked and not include_mocked:
            excluded.append({**ref, "reason": "scripted test run"})
            continue
        if rec.comparison_version != COMPARISON_VERSION:
            excluded.append({**ref, "reason": f"judged by comparison {rec.comparison_version or 'compare-v1'}, not the current {COMPARISON_VERSION}"})
            continue
        receipt = policy.evaluation_receipts.get(rec.id, {})
        if rec.comparison_outcome == "insufficient_evidence" or receipt.get("complete") is not True or receipt.get("blockers"):
            detail = "; ".join(receipt.get("blockers") or ["complete comparison evidence was not recorded"])
            excluded.append({**ref, "reason": f"evaluation incomplete for policy learning: {detail}"})
            continue
        if target_artifact_hash is not None and not rec.artifact_hash:
            excluded.append({**ref, "reason": "source file not recorded, so it cannot be shown to be a different video"})
            continue
        if target_artifact_hash is not None and rec.artifact_hash == target_artifact_hash:
            excluded.append({**ref, "reason": "same source file: an earlier experiment on this file, not transfer from another video", "plan_hash": rec.plan_hash})
            continue
        sim = similarity(conditions, rec.conditions)
        if sim < MIN_SIMILARITY:
            excluded.append({**ref, "reason": f"conditions match {sim:.2f}, below {MIN_SIMILARITY}"})
            continue
        matches.append((rec, sim))
    if not matches:
        return PriorSupport(excluded=excluded)
    total = sum(sim for _, sim in matches)
    score = sum(sim * OUTCOME_VALUE[rec.outcome] for rec, sim in matches) / total
    return PriorSupport(score=round(score, 2), records=len(matches), excluded=excluded,
                        matches=[{"record_id": rec.id, "video_id": rec.video_id, "artifact_hash": rec.artifact_hash, "run_id": rec.run_id, "outcome": rec.outcome,
                                  "similarity": round(sim, 2), "weight": round(sim / total, 4), "evaluator": rec.evaluator.get("cold_viewer_prompt_version", "")}
                                 for rec, sim in matches])


def policy_statements(policy: ConditionalPolicy) -> list[str]:
    """WHEN (conditions shared by every record of a cause and edit) THEN (outcome counts over distinct source files). Plain lines for people."""
    groups: dict[tuple[str, str], list[PolicyRecord]] = {}
    excluded = 0
    for rec in policy.records:
        receipt = policy.evaluation_receipts.get(rec.id, {})
        if receipt.get("complete") is not True or receipt.get("blockers") or rec.comparison_outcome == "insufficient_evidence" or rec.comparison_version != COMPARISON_VERSION:
            excluded += 1
            continue
        groups.setdefault((rec.cause_type, rec.mutation_type), []).append(rec)
    out = []
    for (cause, mtype), recs in sorted(groups.items()):
        shared = {k: v for k, v in recs[0].conditions.items() if v is not None and all(r.conditions.get(k) == v for r in recs)}
        counts = {o: sum(1 for r in recs if r.outcome == o) for o in ("win", "neutral", "loss", "rejected")}
        when = "; ".join(f"{k}={v}" for k, v in shared.items())
        files = len({r.artifact_hash for r in recs if r.artifact_hash})
        scripted = sum(1 for r in recs if r.mocked)
        out.append(f"WHEN {when} THEN {mtype} for {cause}: {counts['win']} win, {counts['neutral']} neutral, {counts['loss']} loss, {counts['rejected']} rejected"
                   + (f" (on {files} source file{'s' if files != 1 else ''})" if files else "") + (f"; {scripted} from scripted test runs" if scripted else ""))
    if excluded:
        out.append(f"{excluded} historical record(s) are preserved but excluded from learning because evaluation completion is unverified/incomplete or the comparison version differs.")
    return out


def new_policy_record(*, video_id: str, run_id: str, experiment: Experiment, conditions: dict[str, Any], outcome: str, evidence: str, artifact_hash: str = "",
                      variant_artifact_hash: str = "", evaluator: dict[str, Any] | None = None, mocked: bool = False, comparison_outcome: str = "",
                      comparison_version: str = COMPARISON_VERSION) -> PolicyRecord:
    return PolicyRecord(id=new_id("prec"), created_at=utc_now_iso(), video_id=video_id, run_id=run_id, experiment_id=experiment.id, cause_type=experiment.cause_type,
                        mutation_type=experiment.mutation_type, edit_key=experiment.edit_key, plan_hash=experiment.plan_hash, artifact_hash=artifact_hash,
                        variant_artifact_hash=variant_artifact_hash, evaluator=evaluator or {}, mocked=mocked, comparison_outcome=comparison_outcome,
                        comparison_version=comparison_version, conditions=conditions, outcome=outcome, evidence=evidence[:300])  # type: ignore[arg-type]


def dump_json(model: BaseModel) -> Any:
    return json.loads(model.model_dump_json())
