"""Structural pattern predicates over a CreativeGenome, and descriptive corpus statistics.

The same predicates describe reference creatives (to count how often a pattern appears among
better- and worse-performing references) and the video under study (to decide which hypotheses
apply). Counts are descriptive; they are a research prior, never proof of cause.
"""

from __future__ import annotations

from collections.abc import Callable

from ..domain.creative import (
    BeatRole,
    CreativeGenome,
    HypothesisFamily,
    MutationType,
    PatternObservation,
    ReferenceCreative,
)


def _anchor_ms(g: CreativeGenome) -> int | None:
    vals = [v for v in (g.first_proof_ms.value, g.first_payoff_ms.value) if isinstance(v, int)]
    return min(vals) if vals else None


def early_proof_or_payoff(g: CreativeGenome) -> bool | None:
    a = _anchor_ms(g)
    return None if a is None else a <= g.duration_ms / 3


def short_context_before_payoff(g: CreativeGenome) -> bool | None:
    v = g.context_before_proof_ms.value
    return None if not isinstance(v, int) else v < 2000


def no_long_static_span(g: CreativeGenome) -> bool | None:
    v = g.longest_static_span_ms.value
    return None if not isinstance(v, int) else v < 2500


def claim_question_or_result_hook(g: CreativeGenome) -> bool | None:
    if not g.beats or g.beats[0].role_confidence == 0.0:
        return None
    first = g.beats[0]
    return first.role in (BeatRole.HOOK, BeatRole.PROOF, BeatRole.PAYOFF) and (first.is_claim or first.is_question or first.role != BeatRole.HOOK)


def loop_resolved_by_60pct(g: CreativeGenome) -> bool | None:
    loops = [lp for lp in g.open_loops if lp.opened_ms <= 3500]
    if not loops:
        return None
    lp = loops[0]
    if lp.resolved_ms is None:
        return False
    return lp.resolved_ms <= 0.6 * g.duration_ms


def visual_change_within_1s(g: CreativeGenome) -> bool | None:
    v = g.hook.first_visual_change_ms.value
    return False if v is None else v <= 1000


PATTERNS: dict[str, tuple[str, Callable[[CreativeGenome], bool | None], str]] = {
    "early_proof_or_payoff": ("A proof or payoff beat starts within the first third of the video", early_proof_or_payoff, "avg_watch_fraction"),
    "short_context_before_payoff": ("Less than 2 s of setup or context before the first proof or payoff", short_context_before_payoff, "avg_watch_fraction"),
    "no_long_static_span": ("No static stretch (no cut, little motion) of 2.5 s or longer", no_long_static_span, "avg_watch_fraction"),
    "claim_question_or_result_hook": ("The first beat is a claim, a question or a result rather than setup", claim_question_or_result_hook, "hold_3s"),
    "loop_resolved_by_60pct": ("The opening question or promise is resolved by 60% of the runtime", loop_resolved_by_60pct, "avg_watch_fraction"),
    "visual_change_within_1s": ("The first visual change happens within 1 s", visual_change_within_1s, "hold_3s"),
}

FAMILY_PATTERN: dict[HypothesisFamily, str] = {
    HypothesisFamily.PROOF_LATENCY: "early_proof_or_payoff",
    HypothesisFamily.CONTEXT_INTERRUPTION: "short_context_before_payoff",
    HypothesisFamily.VISUAL_STAGNATION: "no_long_static_span",
    HypothesisFamily.WEAK_HOOK_TENSION: "claim_question_or_result_hook",
    HypothesisFamily.OPEN_LOOP_TOO_LONG: "loop_resolved_by_60pct",
}

MUTATION_PATTERN: dict[MutationType, str] = {
    MutationType.PROOF_EARLIER: "early_proof_or_payoff",
    MutationType.PAYOFF_EARLIER: "early_proof_or_payoff",
    MutationType.RESULT_FIRST: "claim_question_or_result_hook",
    MutationType.CONTEXT_COMPRESSION: "short_context_before_payoff",
    MutationType.PATTERN_INTERRUPT: "no_long_static_span",
}


def performance_value(ref: ReferenceCreative, metric: str, duration_ms: int | None) -> float | None:
    perf = ref.performance
    if perf is None:
        return None
    if metric == "hold_3s":
        return perf.hold_3s
    if metric == "avg_watch_fraction":
        if perf.avg_watch_time_ms is None or not duration_ms:
            return None
        return perf.avg_watch_time_ms / duration_ms
    return None


def extract_patterns(refs: list[tuple[ReferenceCreative, CreativeGenome]], scope: str) -> list[PatternObservation]:
    """Count each pattern across references; split by the median of the pattern's performance metric."""
    out: list[PatternObservation] = []
    for pid, (desc, fn, metric) in PATTERNS.items():
        rows = [(ref, fn(g), performance_value(ref, metric, g.duration_ms)) for ref, g in refs]
        judged = [(ref, has, perf) for ref, has, perf in rows if has is not None]
        present = [ref.id for ref, has, _ in judged if has]
        absent = [ref.id for ref, has, _ in judged if not has]
        with_perf = [(ref, has, perf) for ref, has, perf in judged if perf is not None]
        top_c = top_t = bot_c = bot_t = None
        note = "descriptive counts; not causal"
        if len(with_perf) >= 6:
            ordered = sorted(with_perf, key=lambda r: r[2])
            half = len(ordered) // 2
            bottom, top = ordered[:half], ordered[len(ordered) - half :]
            top_c, top_t = sum(1 for _, has, _ in top if has), len(top)
            bot_c, bot_t = sum(1 for _, has, _ in bottom if has), len(bottom)
        else:
            note += f"; only {len(with_perf)} references with {metric}, no performance split"
        out.append(
            PatternObservation(
                pattern_id=pid,
                description=desc,
                scope=scope,
                count=len(present),
                total_comparable=len(judged),
                support_ratio=round(len(present) / len(judged), 3) if judged else 0.0,
                example_ids=present[:8],
                counterexample_ids=absent[:8],
                top_group_count=top_c,
                top_group_total=top_t,
                bottom_group_count=bot_c,
                bottom_group_total=bot_t,
                performance_metric=metric,
                note=note,
            )
        )
    return out


def pattern_lift(obs: PatternObservation | None) -> float | None:
    if obs is None or not obs.top_group_total or not obs.bottom_group_total:
        return None
    return (obs.top_group_count or 0) / obs.top_group_total - (obs.bottom_group_count or 0) / obs.bottom_group_total
