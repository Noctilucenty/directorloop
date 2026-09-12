import { getClient } from "../api/client";
import type { PolicyStore } from "../api/types";
import { Badge } from "../components/Badge";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { useAsync } from "../hooks/useAsync";
import { fmtDate, fmtMs, fmtPct } from "../lib/format";

function statusKind(status: string): "ok" | "warn" | "bad" | "neutral" {
  if (status === "validated_on_other_stories" || status === "human_supported") return "ok";
  if (status === "supported_on_dev") return "warn";
  if (status === "contradicted" || status === "rejected" || status === "retired") return "bad";
  return "neutral";
}

export function PoliciesPage() {
  const state = useAsync<PolicyStore>(async () => (await getClient()).getPolicies(), []);
  if (state.loading) return <Loading what="policies" />;
  if (state.error || !state.data) return <ErrorState title="Could not load policies" detail={state.error ?? undefined} onRetry={state.reload} />;
  const store = state.data;
  return (
    <div className="page">
      <div className="page-head">
        <h1>Strategy memory</h1>
        <span className="muted mono">policy version {store.version}</span>
      </div>
      <p className="muted">
        Each rule is a scoped hypothesis with its trigger, action, supporting experiments and counterexamples. Statuses move only on
        evidence; a single successful edit stays proposed.
      </p>
      {store.rules.length === 0 ? <EmptyState title="No rules yet" detail="Rules appear after the first completed improvement run." /> : null}
      {store.rules.map((r) => (
        <section key={r.id} className="panel">
          <div className="panel-head">
            <h2>
              {r.id} <span className="muted">v{r.version}</span>
            </h2>
            <Badge kind={statusKind(r.status)} label={r.status.replace(/_/g, " ").toUpperCase()} />
          </div>
          <p className="policy-text">
            <strong>{r.trigger_category.replace(/_/g, " ")}</strong> in {r.profile_scope.join(", ")}: when <em>{r.trigger_condition}</em>, try{" "}
            <strong>{r.recommended_action.replace(/_/g, " ")}</strong>. {r.action_detail}
          </p>
          {r.contraindications.length ? <div className="muted small">Do not apply when: {r.contraindications.join("; ")}</div> : null}
          <div className="policy-counts">
            <span>
              <span className="mono">{r.supporting.length}</span> supporting
            </span>
            <span>
              <span className="mono">{r.counterexamples.length}</span> counterexamples
            </span>
            <span>
              <span className="mono">{fmtPct(r.confidence)}</span> confidence
            </span>
            <span className="muted small">
              {r.probe_config} / updated {fmtDate(r.updated_at)}
            </span>
          </div>
          <div className="grid-two">
            <div>
              <h3>Supporting</h3>
              {r.supporting.length === 0 ? <div className="muted small">none</div> : null}
              <ul className="plain small">
                {r.supporting.map((e) => (
                  <li key={e.experiment_id} className="mono">
                    {e.experiment_id} {e.story_family}/{e.split} {e.outcome} {e.delta_questions >= 0 ? "+" : ""}
                    {e.delta_questions}q {e.regressions} regressions {e.note ? `(${e.note})` : ""}
                  </li>
                ))}
              </ul>
            </div>
            <div>
              <h3>Counterexamples</h3>
              {r.counterexamples.length === 0 ? <div className="muted small">none</div> : null}
              <ul className="plain small">
                {r.counterexamples.map((e) => (
                  <li key={e.experiment_id} className="mono">
                    {e.experiment_id} {e.story_family}/{e.split} {e.outcome} {e.delta_questions >= 0 ? "+" : ""}
                    {e.delta_questions}q {e.regressions} regressions {e.note ? `(${e.note})` : ""}
                  </li>
                ))}
              </ul>
            </div>
          </div>
          {r.retired_reason ? <div className="muted small">Retired: {r.retired_reason}</div> : null}
        </section>
      ))}
      <section className="panel">
        <h2>Router statistics (outcome counts with shrinkage; not reinforcement learning)</h2>
        {store.router_stats.length === 0 ? (
          <div className="muted small">No routing outcomes recorded.</div>
        ) : (
          <table className="table small">
            <thead>
              <tr>
                <th>Profile</th>
                <th>Failure</th>
                <th>Action</th>
                <th>Attempts</th>
                <th>Successes</th>
                <th>Mean latency</th>
                <th>Total cost</th>
              </tr>
            </thead>
            <tbody>
              {store.router_stats.map((s) => (
                <tr key={`${s.profile}-${s.category}-${s.action}`}>
                  <td>{s.profile}</td>
                  <td>{s.category.replace(/_/g, " ")}</td>
                  <td>{s.action.replace(/_/g, " ")}</td>
                  <td className="mono">{s.attempts}</td>
                  <td className="mono">{s.successes}</td>
                  <td className="mono">{s.attempts ? fmtMs(s.total_latency_ms / s.attempts) : "n/a"}</td>
                  <td className="mono">${s.total_cost_usd.toFixed(3)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
