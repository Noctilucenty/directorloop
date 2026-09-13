import { Link } from "react-router-dom";
import { apiMode, getClient } from "../api/client";
import type { ExperimentSummary } from "../api/types";
import { RunBadge } from "../components/Badge";
import { ResearchNav } from "../components/Shell";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { useAppState } from "../hooks/useAppState";
import { useAsync } from "../hooks/useAsync";
import { fmtDateTime, fmtElapsed } from "../lib/format";
import { decisionName } from "../lib/labels";

export function ExperimentsPage() {
  const { videos, video, sessionExperiments } = useAppState();
  const list = useAsync<ExperimentSummary[]>(() => getClient().then((c) => c.listExperiments()), []);
  const titleOf = (id: string) => videos.find((v) => v.video_id === id)?.title ?? id;

  return (
    <div className="page page-narrow-wide">
      <ResearchNav />
      <div className="page-head">
        <div>
          <div className="eyebrow">Experiments</div>
          <h1 className="page-title">Every edit is a question.</h1>
        </div>
        <div className="page-head-actions">
          {video ? (
            <Link className="btn btn-primary" to={video.latest_experiment_id ? `/research/experiments/${encodeURIComponent(video.latest_experiment_id)}` : `/research/experiments/new/${encodeURIComponent(video.video_id)}`}>
              Open current video
            </Link>
          ) : null}
        </div>
      </div>
      {list.loading && !list.data ? <Loading what="experiments" /> : null}
      {list.error ? <ErrorState title="Could not load experiments" detail={list.error} onRetry={list.reload} /> : null}
      {list.data && list.data.length === 0 ? <EmptyState title="No experiments yet" detail="Run one from the workbench." /> : null}
      {list.data && list.data.length ? (
        <ol className="experiment-library">
          {list.data.map((e,i) => <li key={e.id}>
            <span className="library-number mono">{String(i+1).padStart(2,"0")}</span>
            <div className="library-video"><span className="eyebrow">{fmtDateTime(e.created_at)} · {e.arms} arms including control</span><h2><Link to={`/research/experiments/${encodeURIComponent(e.id)}`}>{titleOf(e.video_id)}</Link></h2><p>Policy v{e.policy_version_before} → v{e.policy_version_after ?? e.policy_version_before} <span>·</span> {fmtElapsed(e.total_ms)}</p><details className="disclosure"><summary>Run provenance</summary><p className="mono">{e.id}</p>{sessionExperiments.has(e.id) ? <RunBadge kind={apiMode() === "live" ? "live" : "replay"} /> : e.recorded ? <RunBadge kind="recorded" /> : <span>No run provenance supplied.</span>}{e.weave_url ? <a className="text-link" href={e.weave_url} target="_blank" rel="noreferrer">Open Weave trace</a> : null}</details></div>
            <div className="library-outcome"><span className="eyebrow">Experiment decision</span><p>{decisionName(e.outcome)}{e.winner_label ? ` · ${e.winner_label}` : ""}</p><Link className="text-link" to={`/research/experiments/${encodeURIComponent(e.id)}`}>Inspect experiment <span aria-hidden="true">↗</span></Link></div>
          </li>)}
        </ol>
      ) : null}
    </div>
  );
}
