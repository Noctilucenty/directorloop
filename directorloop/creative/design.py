"""ExperimentDesigner: from competing hypotheses to a small, controlled experiment.

Every candidate is a validated single-variable mutation. Ranking combines how strongly the genome
supports the hypothesis, a weak reference prior, and our own experiment evidence for that mutation
type in this category. policy_mode="none" holds the experiment evidence at neutral, which is the
no-memory baseline for transfer tests: both modes are identical until experiments exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..domain.assets import AssetManifest
from ..domain.brief import CreativeBrief
from ..domain.creative import (
    CreativeGenome,
    CreativeHypothesis,
    CreativeMutation,
    HypothesisRanking,
    MutationType,
    ReferenceCorpus,
)
from ..domain.edit_plan import EditPlan, apply_ops
from ..observability.weave_ops import traced
from .mutate import build_mutation
from .patterns import MUTATION_PATTERN, pattern_lift
from .policy import CreativePolicyStore

W_DETECTION = 0.30
W_REFERENCE = 0.20
W_EVIDENCE = 0.50


@dataclass
class Candidate:
    hypothesis: CreativeHypothesis
    mutation: CreativeMutation
    ranking: HypothesisRanking
    plan_hash: str


@dataclass
class ExperimentDesign:
    candidates: list[Candidate]
    selected: list[Candidate]
    rankings: list[HypothesisRanking]
    policy_mode: str
    policy_version: int
    skipped: list[str] = field(default_factory=list)


def reference_component(mtype: MutationType, corpus: ReferenceCorpus | None) -> tuple[float, float | None]:
    pid = MUTATION_PATTERN.get(mtype)
    if corpus is None or pid is None:
        return 0.5, None
    lift = pattern_lift(corpus.pattern(pid))
    if lift is None:
        return 0.5, None
    return 0.5 + 0.125 * max(-1.0, min(1.0, lift)), lift


@traced("design_experiment", kind="agent")
def design_experiment(
    *,
    hypotheses: list[CreativeHypothesis],
    genome: CreativeGenome,
    plan: EditPlan,
    manifest: AssetManifest,
    brief: CreativeBrief,
    video_path: Path,
    parent_version_id: str,
    policy: CreativePolicyStore,
    corpus: ReferenceCorpus | None,
    policy_mode: str = "learned",
    max_arms: int = 3,
    category: str = "educational_short",
) -> ExperimentDesign:
    candidates: list[Candidate] = []
    skipped: list[str] = []
    seen_plans: dict[str, Candidate] = {}
    for hyp in hypotheses:
        for mtype in hyp.candidate_mutations:
            mutation = build_mutation(mtype, hyp, genome, plan, manifest, brief, parent_version_id, video_path)
            if mutation is None:
                skipped.append(f"{mtype.value} for {hyp.family.value}: precondition not met or plan invalid")
                continue
            plan_hash = apply_ops(plan, mutation.ops).content_hash()
            strat = policy.lookup(mtype, category)
            ref_score, lift = reference_component(mtype, corpus)
            if policy_mode == "learned" and strat is not None:
                ev_mean = strat.evidence_mean()
                wins, losses, neutral = strat.wins, strat.losses + strat.rejected, strat.neutral
            else:
                ev_mean, wins, losses, neutral = 0.5, 0, 0, 0
            score = W_DETECTION * hyp.detection_confidence + W_REFERENCE * ref_score + W_EVIDENCE * ev_mean
            reason = f"detection {hyp.detection_confidence:.2f}; reference prior {ref_score:.2f}" + (f" (lift {lift:+.2f})" if lift is not None else " (no reference split)")
            if policy_mode == "learned":
                reason += f"; experiment evidence {ev_mean:.2f} from {wins} win(s), {losses} loss(es), {neutral} neutral"
            else:
                reason += "; experiment evidence ignored (no-memory mode)"
            ranking = HypothesisRanking(
                mutation_type=mtype,
                hypothesis_id=hyp.id,
                score=round(score, 4),
                detection_confidence=hyp.detection_confidence,
                reference_support=round(ref_score, 3),
                policy_mean=round(ev_mean, 3),
                policy_wins=wins,
                policy_losses=losses,
                policy_neutral=neutral,
                reason=reason,
            )
            cand = Candidate(hypothesis=hyp, mutation=mutation, ranking=ranking, plan_hash=plan_hash)
            prior = seen_plans.get(plan_hash)
            if prior is not None:
                if prior.ranking.score >= score:
                    skipped.append(f"{mtype.value} renders the same plan as {prior.mutation.type.value}; kept the higher-ranked one")
                    continue
                candidates.remove(prior)
                skipped.append(f"{prior.mutation.type.value} renders the same plan as {mtype.value}; kept the higher-ranked one")
            seen_plans[plan_hash] = cand
            candidates.append(cand)
    candidates.sort(key=lambda c: (-c.ranking.score, c.mutation.type.value))
    selected: list[Candidate] = []
    families: set[str] = set()
    for c in candidates:  # first pass: one arm per hypothesis family, to distinguish explanations
        if len(selected) >= max_arms:
            break
        if c.hypothesis.family.value not in families:
            selected.append(c)
            families.add(c.hypothesis.family.value)
    for c in candidates:  # fill remaining slots by rank
        if len(selected) >= max_arms:
            break
        if c not in selected:
            selected.append(c)
    selected.sort(key=lambda c: -c.ranking.score)
    return ExperimentDesign(
        candidates=candidates,
        selected=selected,
        rankings=[c.ranking for c in candidates],
        policy_mode=policy_mode,
        policy_version=policy.version,
        skipped=skipped,
    )
