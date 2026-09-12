"""RetentionInvestigator: competing, evidence-backed hypotheses about where and why a video may lose people.

Structural detectors read the CreativeGenome. When a retention curve exists (historical or real)
the steepest measured drop becomes the weak region and hypotheses overlapping it gain that
evidence. Without a curve, regions come from creative structure and say so. Reference patterns
add counts, never verdicts. Every output is a hypothesis with evidence and counterevidence.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..domain.creative import (
    BeatRole,
    CreativeGenome,
    CreativeHypothesis,
    EvidenceClass,
    EvidenceItem,
    FeatureSource,
    HookType,
    HypothesisFamily,
    MutationType,
    ReferenceCorpus,
    RetentionSeries,
)
from ..domain.ids import new_id
from ..observability.weave_ops import traced
from .patterns import FAMILY_PATTERN, pattern_lift


class WeakRegion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_ms: int
    end_ms: int
    basis: str  # measured retention drop | creative structure
    evidence_class: EvidenceClass
    description: str
    drop_fraction: float | None = None


class Investigation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weak_region: WeakRegion | None
    hypotheses: list[CreativeHypothesis]
    retention_note: str
    observations: list[str] = Field(default_factory=list)


def steepest_drop(series: RetentionSeries, skip_first_ms: int = 3000) -> tuple[int, int, float] | None:
    pts = [p for p in series.points if p.t_ms >= skip_first_ms]
    if len(pts) < 2:
        return None
    best: tuple[int, int, float] | None = None
    for a, b in zip(pts, pts[1:], strict=False):
        drop = a.remaining_fraction - b.remaining_fraction
        if best is None or drop > best[2]:
            best = (a.t_ms, b.t_ms, drop)
    return best


def _ref_evidence(corpus: ReferenceCorpus | None, family: HypothesisFamily) -> list[EvidenceItem]:
    pid = FAMILY_PATTERN.get(family)
    if corpus is None or pid is None:
        return []
    obs = corpus.pattern(pid)
    if obs is None or obs.total_comparable == 0:
        return []
    text = f"reference corpus: '{obs.description}' in {obs.count} of {obs.total_comparable} comparable references"
    lift = pattern_lift(obs)
    if lift is not None:
        text += (
            f"; {obs.top_group_count}/{obs.top_group_total} of the higher-{obs.performance_metric} half vs "
            f"{obs.bottom_group_count}/{obs.bottom_group_total} of the lower half (descriptive, not causal)"
        )
    return [EvidenceItem(kind=EvidenceClass.REFERENCE, text=text, ref=pid)]


@traced("identify_weak_region", kind="tool")
def investigate(
    genome: CreativeGenome,
    retention: RetentionSeries | None = None,
    corpus: ReferenceCorpus | None = None,
    scope: str = "educational_short",
) -> Investigation:
    d = genome.duration_ms
    hyps: list[CreativeHypothesis] = []
    observations: list[str] = []
    beats = genome.beats

    def add(family: HypothesisFamily, statement: str, start: int, end: int, evidence: list[EvidenceItem], counter: list[EvidenceItem], conf: float, muts: list[MutationType], variable: str) -> None:
        hyps.append(
            CreativeHypothesis(
                id=new_id("hyp"),
                family=family,
                statement=statement,
                region_start_ms=max(0, start),
                region_end_ms=min(d, max(start + 1, end)),
                region_basis="creative structure",
                evidence=evidence + _ref_evidence(corpus, family),
                counterevidence=counter,
                scope=scope,
                detection_confidence=round(max(0.05, min(0.95, conf)), 2),
                candidate_mutations=muts,
                changed_variable=variable,
            )
        )

    hook_type = str(genome.hook.hook_type.value)
    proof_ms = genome.first_proof_ms.value if isinstance(genome.first_proof_ms.value, int) else None
    payoff_ms = genome.first_payoff_ms.value if isinstance(genome.first_payoff_ms.value, int) else None
    anchor = min([v for v in (proof_ms, payoff_ms) if v is not None], default=None)
    hook_end = beats[0].end_ms if beats else 3000

    # H: proof latency
    late_threshold = max(3500, int(d * 0.33))
    if anchor is not None and anchor > late_threshold:
        which = "proof" if proof_ms == anchor else "payoff"
        ev = [EvidenceItem(kind=EvidenceClass.MODEL_EVAL, source=FeatureSource.LANGUAGE_MODEL, text=f"first {which} beat starts at {anchor / 1000:.1f}s of {d / 1000:.1f}s (beat roles are language-model labels)")]
        counter = []
        if hook_type in (HookType.SURPRISING_CLAIM.value, HookType.RESULT_FIRST.value, HookType.CONTRADICTION.value):
            counter.append(EvidenceItem(kind=EvidenceClass.MODEL_EVAL, source=FeatureSource.LANGUAGE_MODEL, text=f"the hook is labeled {hook_type}, so the opening may already carry the surprise"))
        muts = [MutationType.PROOF_EARLIER, MutationType.PAYOFF_EARLIER, MutationType.RESULT_FIRST] if proof_ms is not None else [MutationType.PAYOFF_EARLIER, MutationType.RESULT_FIRST]
        add(
            HypothesisFamily.PROOF_LATENCY,
            f"The evidence or answer arrives late ({anchor / 1000:.1f}s); people may leave before the video pays off its opening.",
            hook_end, anchor, ev, counter, 0.35 + (anchor - late_threshold) / max(1, d), muts, "when the proof or payoff appears",
        )

    # H: context interruption
    ctx = genome.context_before_proof_ms.value
    ctx_beats = [b for b in beats if b.role in (BeatRole.SETUP, BeatRole.CONTEXT, BeatRole.PROBLEM) and anchor is not None and b.start_ms < anchor and b.index > 0]
    if isinstance(ctx, int) and ctx >= max(1800, int(0.15 * d)) and ctx_beats:
        ev = [EvidenceItem(kind=EvidenceClass.MODEL_EVAL, source=FeatureSource.LANGUAGE_MODEL, text=f"{ctx / 1000:.1f}s of setup/context between the hook and the first proof or payoff: " + "; ".join(f"'{b.text[:40]}'" for b in ctx_beats[:2]), ref=ctx_beats[0].id)]
        counter = [EvidenceItem(kind=EvidenceClass.MODEL_EVAL, text="context may be needed to understand the payoff; the comprehension suite guards against removing it")]
        add(
            HypothesisFamily.CONTEXT_INTERRUPTION,
            "Background context interrupts the momentum between the hook and the payoff.",
            ctx_beats[0].start_ms, ctx_beats[-1].end_ms, ev, counter, 0.3 + min(0.4, ctx / max(1, d)), [MutationType.CONTEXT_COMPRESSION], "whether one context beat is present",
        )

    # H: visual stagnation
    span = genome.longest_static_span_ms.value
    span_start = genome.longest_static_span_start_ms.value
    if isinstance(span, int) and isinstance(span_start, int) and span >= 2500:
        ev = [EvidenceItem(kind=EvidenceClass.MECHANICAL, source=FeatureSource.MECHANICAL, text=f"no shot cut and frame difference under threshold for {span / 1000:.1f}s from {span_start / 1000:.1f}s")]
        counter = [EvidenceItem(kind=EvidenceClass.MECHANICAL, text="low frame difference can hide slow meaningful motion; a held shot can be intentional")]
        add(
            HypothesisFamily.VISUAL_STAGNATION,
            f"The picture barely changes for {span / 1000:.1f}s, which may lose attention.",
            span_start, span_start + span, ev, counter, 0.3 + min(0.45, (span - 2500) / 8000), [MutationType.PATTERN_INTERRUPT], "framing during the static stretch",
        )

    # H: weak hook tension
    if beats and (beats[0].role in (BeatRole.SETUP, BeatRole.CONTEXT) or hook_type in (HookType.CONTEXT_FIRST.value, HookType.UNKNOWN.value)):
        ev = [EvidenceItem(kind=EvidenceClass.MODEL_EVAL, source=FeatureSource.LANGUAGE_MODEL, text=f"the first beat is labeled {beats[0].role.value} (hook type {hook_type}): '{beats[0].text[:60]}'", ref=beats[0].id)]
        add(
            HypothesisFamily.WEAK_HOOK_TENSION,
            "The opening sets up context instead of creating a question or showing a result.",
            0, beats[0].end_ms, ev, [], 0.45 + 0.2 * beats[0].role_confidence, [MutationType.RESULT_FIRST], "which beat opens the video",
        )

    # H: open loop held too long
    loops = [lp for lp in genome.open_loops if lp.opened_ms <= 3500]
    if loops:
        lp = loops[0]
        resolved = lp.resolved_ms if lp.resolved_ms is not None else d
        if resolved - lp.opened_ms > 0.55 * d:
            ev = [EvidenceItem(kind=EvidenceClass.MODEL_EVAL, source=FeatureSource.LANGUAGE_MODEL, text=f"the opening promise '{lp.description[:70]}' resolves at {resolved / 1000:.1f}s")]
            counter = [EvidenceItem(kind=EvidenceClass.MODEL_EVAL, text="a long open loop can also sustain curiosity; resolving it early can remove the reason to keep watching")]
            add(
                HypothesisFamily.OPEN_LOOP_TOO_LONG,
                "The question raised at the start is held open for most of the video.",
                lp.opened_ms, resolved, ev, counter, 0.3 + 0.3 * lp.confidence, [MutationType.PAYOFF_EARLIER], "when the opening question is answered",
            )

    # H: redundant beat
    red = next((b for b in beats if b.redundant_with_beat_id), None)
    if red is not None:
        add(
            HypothesisFamily.REDUNDANT_BEAT,
            f"'{red.text[:50]}' repeats information from {red.redundant_with_beat_id}.",
            red.start_ms, red.end_ms,
            [EvidenceItem(kind=EvidenceClass.MODEL_EVAL, source=FeatureSource.LANGUAGE_MODEL, text=f"beat {red.id} marked as repeating {red.redundant_with_beat_id}", ref=red.id)],
            [], 0.4, [MutationType.REMOVE_REDUNDANT_BEAT], "whether the repeated beat is present",
        )

    # Retention overlay
    weak: WeakRegion | None = None
    retention_note = "no retention data: regions come from creative structure, not from measured drops"
    if retention is not None and retention.points:
        drop = steepest_drop(retention)
        cls = retention.evidence_class
        if drop is not None:
            a, b, frac = drop
            weak = WeakRegion(start_ms=a, end_ms=b, basis="measured retention drop", evidence_class=cls, drop_fraction=round(frac, 4),
                              description=f"steepest measured drop: {frac * 100:.1f} points of remaining viewers between {a / 1000:.1f}s and {b / 1000:.1f}s ({retention.platform}, {cls.value})")
            retention_note = weak.description + ". Caveats: " + "; ".join(retention.caveats[:2])
            for h in hyps:
                if h.region_start_ms <= b + 1000 and h.region_end_ms >= a - 1000:
                    h.evidence.append(EvidenceItem(kind=cls, text=weak.description))
                    h.detection_confidence = round(min(0.95, h.detection_confidence + 0.1), 2)
                    h.region_basis = "creative structure overlapping a measured retention drop"
                else:
                    h.counterevidence.append(EvidenceItem(kind=cls, text=f"the steepest measured drop ({a / 1000:.1f}-{b / 1000:.1f}s) is outside this region"))
    elif retention is not None:
        retention_note = f"{retention.platform} summary metrics only ({retention.source_type.value}); no curve, so no measured drop region"
    if weak is None and hyps:
        top = max(hyps, key=lambda h: h.detection_confidence)
        weak = WeakRegion(start_ms=top.region_start_ms, end_ms=top.region_end_ms, basis="creative structure", evidence_class=EvidenceClass.MODEL_EVAL,
                          description=f"potential weak region from structure: {top.family.value} at {top.region_start_ms / 1000:.1f}-{top.region_end_ms / 1000:.1f}s")
    if not hyps:
        observations.append("no structural weakness detected by the current detectors")
    hyps.sort(key=lambda h: -h.detection_confidence)
    return Investigation(weak_region=weak, hypotheses=hyps, retention_note=retention_note, observations=observations)
