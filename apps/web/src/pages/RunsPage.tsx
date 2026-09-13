import { Link } from "react-router-dom";
import { getClient } from "../api/client";
import type { CausalSummary } from "../api/causal";
import { causalOutcome } from "../lib/causal";
import type { ABCSummary, RunSummary } from "../api/types";
import { DecisionTag } from "../components/Attempts";
import { Frame } from "../components/Shell";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { useAppState } from "../hooks/useAppState";
import { useAsync } from "../hooks/useAsync";
import { fmtDateTime, fmtElapsed } from "../lib/format";

function outcomeLine(r: RunSummary): { text: string; decision: string } {
  if (r.status === "running") return { text: "Running", decision: "stop" };
  if (r.status === "failed") return { text: "Did not finish", decision: "incomplete_keep_current" };
  if (r.decisions.includes("accept")) return { text: "A revision was kept", decision: "accept" };
  if (r.iterations > 0) return { text: `${r.iterations} edit${r.iterations === 1 ? "" : "s"} tried, original kept`, decision: "reject_keep_current" };
  return { text: "No edit was possible, original kept", decision: "stop" };
}

export function RunsPage() {
  const causal = useAsync<CausalSummary[]>(() => getClient().then((c) => c.listCausal()), []);
  const runs = useAsync<RunSummary[]>(() => getClient().then((c) => c.listRuns()), []);
  const abcs = useAsync<ABCSummary[]>(() => getClient().then((c) => c.listAbc()), []);
  const { videos } = useAppState();
  return (
    <div className="page">
      <header className="page-head">
        <Frame>Runs</Frame>
        <h1 className="page-title">Every experiment, and what we learned</h1>
        <Link className="text-link" to="/research/experiments">Browse earlier research experiments</Link>
      </header>
      <header className="page-head page-head-sub"><Frame>Controlled experiments</Frame><p className="muted">Competing explanations, isolated edits, and recorded outcomes. Model comparisons do not measure human retention.</p></header>
      {causal.loading && !causal.data ? <Loading what="controlled sessions" /> : null}
      {causal.error ? <ErrorState title="Could not load controlled sessions" detail={causal.error} onRetry={causal.reload} /> : null}
      {causal.data?.length === 0 ? <EmptyState title="No controlled sessions yet" detail="Open a video and choose Test explanations." /> : null}
      <ul className="run-list">{causal.data?.map((r) => <li key={r.id}><Link to={`/causal/${encodeURIComponent(r.id)}`} className="run-row"><span className="run-row-title">{videos.find((v) => v.video_id === r.video_id)?.title ?? r.video_id}</span><span className="run-row-outcome">{causalOutcome(r)}</span><span className="run-row-reason">{r.stop_reason}</span><span className="run-row-meta mono">{fmtDateTime(r.created_at)} · {r.audit_source === "recorded" ? "Saved baseline" : r.audit_source === "fresh" ? "Baseline reviewed in session" : "No baseline recorded"}{r.total_ms !== null ? ` · ${fmtElapsed(r.total_ms)}` : ""}</span></Link></li>)}</ul>
      <header className="page-head page-head-sub"><Frame>Earlier improvement runs</Frame></header>
      {runs.loading && !runs.data ? <Loading what="runs" /> : null}
      {runs.error ? <ErrorState title="Could not load runs" detail={runs.error} onRetry={runs.reload} /> : null}
      {runs.data && runs.data.length === 0 ? <EmptyState title="No runs yet" detail="Judge a video to start one." action={<Link to="/" className="btn btn-primary">New video</Link>} /> : null}
      {runs.data?.length ? (
        <ul className="run-list">
          {runs.data.map((r) => {
            const o = outcomeLine(r);
            return (
              <li key={r.id}>
                <Link to={`/runs/${encodeURIComponent(r.id)}`} className="run-row">
                  <span className="run-row-title">{videos.find((v) => v.video_id === r.video_id)?.title ?? r.video_id}</span>
                  <span className="run-row-outcome">
                    <DecisionTag decision={o.decision} />
                    {o.text}
                  </span>
                  <span className="run-row-reason">{r.status === "completed" ? r.final_decision : r.stop_reason}</span>
                  <span className="run-row-meta mono">
                    {fmtDateTime(r.created_at)}
                    {r.total_ms ? ` · ${fmtElapsed(r.total_ms)}` : ""}
                  </span>
                </Link>
              </li>
            );
          })}
        </ul>
      ) : null}
      <header className="page-head page-head-sub">
        <Frame>A/B comparisons</Frame>
      </header>
      {abcs.loading && !abcs.data ? <Loading what="comparisons" /> : null}
      {abcs.error ? <ErrorState title="Could not load comparisons" detail={abcs.error} onRetry={abcs.reload} /> : null}
      {abcs.data && abcs.data.length === 0 ? <EmptyState title="No comparisons yet" /> : null}
      {abcs.data?.length ? (
        <ul className="run-list">
          {abcs.data.map((a) => (
            <li key={a.id}>
              <Link to={`/compare/${encodeURIComponent(a.id)}`} className="run-row">
                <span className="run-row-title">
                  {a.a_video_id} and {a.b_video_id}
                </span>
                <span className="run-row-outcome">
                  <DecisionTag decision={a.status === "failed" ? "incomplete_keep_current" : a.decisions.includes("accept") ? "accept" : a.attempts ? "reject_keep_current" : "stop"} />
                  {a.status === "failed" ? "Did not finish" : a.ab_overall ? `Preferred overall: ${a.ab_overall}` : "No comparison"}
                </span>
                <span className="run-row-reason">{a.status === "failed" ? a.stop_reason : a.final_decision}</span>
                <span className="run-row-meta mono">
                  {fmtDateTime(a.created_at)}
                  {a.total_ms ? ` · ${fmtElapsed(a.total_ms)}` : ""}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
