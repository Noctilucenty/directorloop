import type { RepairProposal } from "../api/types";
import { fmtMs, fmtUsd, fmtPct } from "../lib/format";

interface Props {
  proposal: RepairProposal | null;
  diffLines: string[];
}

export function EditDiff({ proposal, diffLines }: Props) {
  return (
    <section className="panel" aria-label="Proposed edit">
      <h2>What the agent changed</h2>
      {diffLines.length ? (
        <ul className="diff-list">
          {diffLines.map((line, i) => (
            <li key={i}>{line}</li>
          ))}
        </ul>
      ) : (
        <div className="muted">No edit yet.</div>
      )}
      {proposal ? (
        <>
          <div className="proposal-action">
            {proposal.action.replace(/_/g, " ")}
            <span className="muted small"> via {proposal.planner}</span>
          </div>
          <p className="proposal-summary">{proposal.decision_summary}</p>
          <div className="muted small">Predicted: {proposal.predicted_benefit}</div>
          <details className="details">
            <summary>Routing table ({proposal.routing_table.length} actions considered)</summary>
            <table className="table small">
              <thead>
                <tr>
                  <th>Action</th>
                  <th>Allowed</th>
                  <th>Feasible</th>
                  <th>Expected</th>
                  <th>Latency</th>
                  <th>Cost</th>
                  <th>Risk</th>
                  <th>Prior</th>
                  <th>Note</th>
                </tr>
              </thead>
              <tbody>
                {proposal.routing_table.map((r) => (
                  <tr key={r.action} className={r.action === proposal.action ? "row-selected" : ""}>
                    <td>{r.action.replace(/_/g, " ")}</td>
                    <td>{r.allowed ? "yes" : "no"}</td>
                    <td>{r.feasible ? "yes" : "no"}</td>
                    <td>{r.expected_improvement}</td>
                    <td className="mono">
                      {fmtMs(r.latency.expected_ms)} <span className="muted">({r.latency.source})</span>
                    </td>
                    <td className="mono">{fmtUsd(r.cost_usd)}</td>
                    <td>
                      {r.creative_risk}/{r.regression_risk}
                    </td>
                    <td className="mono">
                      {r.prior_success_rate === null ? "none" : `${fmtPct(r.prior_success_rate)} (n=${r.prior_samples})`}
                    </td>
                    <td>{r.note}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
          {proposal.rejected_alternatives.length ? (
            <details className="details">
              <summary>Rejected alternatives ({proposal.rejected_alternatives.length})</summary>
              <ul className="plain">
                {proposal.rejected_alternatives.map((r) => (
                  <li key={r.action}>
                    <strong>{r.action.replace(/_/g, " ")}</strong>: {r.reason}
                  </li>
                ))}
              </ul>
            </details>
          ) : null}
          {proposal.requires_generation && proposal.generation_request ? (
            <div className="callout callout-warn">
              Generation requested: {proposal.generation_request.duration_ms} ms, {proposal.generation_request.width}x
              {proposal.generation_request.height}
            </div>
          ) : null}
          <div className="muted small mono">
            expected {fmtMs(proposal.expected_latency.expected_ms)} ({proposal.expected_latency.source}), est. {fmtUsd(proposal.estimated_cost_usd)}, risk{" "}
            {proposal.creative_risk}/{proposal.regression_risk}
            {proposal.policy_rule_ids_used.length ? `, rules ${proposal.policy_rule_ids_used.join(", ")}` : ""}
          </div>
        </>
      ) : null}
    </section>
  );
}
