import type { PolicyStore, TransferReport } from "../api/types";

/** Only evidence present at the saved policy version can explain a historical transfer. */
export function transferEvidence(report: TransferReport, policy: PolicyStore | null) {
  if (!policy) return [];
  const prior = policy.changes.filter(c => c.version <= report.policy_version);
  const candidates = policy.strategies.flatMap(strategy => strategy.evidence.flatMap(evidence => {
    const change = prior.find(c => c.strategy_id === strategy.id && c.experiment_id === evidence.experiment_id);
    return change && evidence.video_id !== report.video_id && report.modes.learned.order.includes(strategy.mutation)
      ? [{ ...evidence, mutation: strategy.mutation, scope: strategy.scope, policyVersion: change.version, snapshot: change.after }]
      : [];
  }));
  const baseline = report.modes.none.first_mutation;
  return candidates.sort((a,b) => Number(b.mutation === baseline) - Number(a.mutation === baseline) || b.policyVersion - a.policyVersion);
}

export function rankChange(mutation: string, before: string[], after: string[]) {
  const a = before.indexOf(mutation), b = after.indexOf(mutation);
  return { before: a < 0 ? null : a+1, after: b < 0 ? null : b+1, delta: a < 0 || b < 0 ? null : a-b };
}
