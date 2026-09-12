"""Strategy memory: scoped policy hypotheses with evidence, plus router statistics.

Persisted as JSON in the data directory (`policy_store.json`). Every experiment,
successful or not, updates the record. Statuses follow spec section 22.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..domain.brief import GoalProfile, RepairAction
from ..domain.decision import Comparison, Outcome, PromotionDecision
from ..domain.findings import FailureCategory, FailureFinding
from ..domain.ids import new_id, utc_now_iso
from ..domain.policy import PolicyEvidence, PolicyRule, PolicyStatus, PolicyStore
from ..domain.repair import RepairProposal
from ..observability.weave_ops import traced


def load_store(path: Path) -> PolicyStore:
    if not path.exists():
        return PolicyStore()
    return PolicyStore.model_validate(json.loads(path.read_text(encoding="utf-8")))


def save_store(store: PolicyStore, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(store.model_dump(mode="json"), indent=2), encoding="utf-8")
    tmp.replace(path)


RULE_TEXT: dict[tuple[FailureCategory, RepairAction], tuple[str, str, list[str]]] = {
    (FailureCategory.MISSING_ACTION_VISIBILITY, RepairAction.REPLACE_WITH_EXISTING_ASSET): (
        "a required physical action is not visibly identifiable in the shot that covers it, and another existing shot shows it",
        "replace the covering segment with the closer existing shot before adding explanation",
        ["the action is intentionally withheld for a later reveal", "the brief forbids changing the shot order or footage"],
    ),
    (FailureCategory.MISSING_ACTION_VISIBILITY, RepairAction.CROP_EXISTING_SHOT): (
        "a required physical action is present but small in a high-resolution wide shot",
        "crop into the motion region of the existing shot",
        ["source resolution would need more than 3x upscale", "the wide framing is itself the point of the shot"],
    ),
    (FailureCategory.MISSING_ACTION_VISIBILITY, RepairAction.GENERATE_MISSING_SHOT): (
        "a required physical action is not shown by any existing asset",
        "generate only the missing short shot and insert it at the point of the action",
        ["product appearance is protected", "generation latency exceeds the job deadline"],
    ),
    (FailureCategory.MISSING_REQUIRED_INFORMATION, RepairAction.REVISE_CAPTIONS): (
        "a required fact is neither said nor shown",
        "add or reword a caption that states the approved claim",
        ["the brief forbids new on-screen text", "the fact needs visual evidence, not text"],
    ),
    (FailureCategory.EVENT_ORDER_CONFUSION, RepairAction.REORDER_SEGMENTS): (
        "viewers confuse the order of events",
        "move the cause before the consequence in the timeline",
        ["a reveal must stay late", "narration timing is locked to the current order"],
    ),
}


def _status_for(rule: PolicyRule) -> PolicyStatus:
    support, counter = rule.support_count, rule.counter_count
    if rule.status == PolicyStatus.HUMAN_SUPPORTED:
        return rule.status
    if counter > support:
        return PolicyStatus.CONTRADICTED
    if support == 0:
        return PolicyStatus.REJECTED if counter else PolicyStatus.PROPOSED
    families = rule.story_families_supporting()
    if len(families) >= 2:
        return PolicyStatus.VALIDATED_ON_OTHER_STORIES
    if support >= 2:
        return PolicyStatus.SUPPORTED_ON_DEV
    return PolicyStatus.PROPOSED


@traced("propose_policy_rule", kind="tool")
def record_experiment(
    store: PolicyStore,
    *,
    experiment_id: str,
    project_id: str,
    story_family: str,
    split: str,
    profile: GoalProfile,
    finding: FailureFinding,
    proposal: RepairProposal,
    decision: PromotionDecision,
    comparison: Comparison | None,
    latency_ms: int,
    cost_usd: float,
    probe_config: str,
) -> PolicyRule:
    """Update router statistics and the matching policy rule with this experiment's evidence."""
    action = proposal.action
    success = decision.outcome == Outcome.PROMOTED
    stat = store.stat(profile, finding.category, action)
    stat.attempts += 1
    stat.successes += 1 if success else 0
    stat.total_latency_ms += int(latency_ms)
    stat.total_cost_usd += float(cost_usd)

    rule = next((r for r in store.rules if r.trigger_category == finding.category and r.recommended_action == action), None)
    now = utc_now_iso()
    if rule is None:
        trigger, detail, contra = RULE_TEXT.get(
            (finding.category, action),
            (f"{finding.category} is detected", f"apply {action}", []),
        )
        rule = PolicyRule(
            id=new_id("rule"),
            profile_scope=[profile],
            trigger_category=finding.category,
            trigger_condition=trigger,
            recommended_action=action,
            action_detail=detail,
            contraindications=contra,
            created_at=now,
            updated_at=now,
            probe_config=probe_config,
        )
        store.rules.append(rule)
    if profile not in rule.profile_scope:
        rule.profile_scope.append(profile)
    evidence = PolicyEvidence(
        experiment_id=experiment_id,
        project_id=project_id,
        story_family=story_family,
        split=split,
        outcome=str(decision.outcome),
        delta_questions=decision.delta_questions,
        delta_score=round(decision.delta_score, 3),
        regressions=len(decision.regressions),
        note=decision.reason[:200],
    )
    if success:
        rule.supporting.append(evidence)
    else:
        rule.counterexamples.append(evidence)
    rule.status = _status_for(rule)
    total = rule.support_count + rule.counter_count
    rule.confidence = round((rule.support_count + 1) / (total + 2), 2)  # Laplace-smoothed, small samples stay modest
    rule.updated_at = now
    rule.version += 1
    store.version += 1
    return rule


def policy_hints(store: PolicyStore, profile: GoalProfile, category: FailureCategory) -> list[dict[str, object]]:
    """Compact evidence the planner receives. Contains no answers, only strategy history."""
    hints = []
    for r in store.applicable_rules(profile, category):
        hints.append(
            {
                "rule_id": r.id,
                "status": str(r.status),
                "recommended_action": str(r.recommended_action),
                "when": r.trigger_condition,
                "do": r.action_detail,
                "do_not_apply_when": r.contraindications,
                "supporting_experiments": r.support_count,
                "counterexamples": r.counter_count,
                "confidence": r.confidence,
            }
        )
    return hints
