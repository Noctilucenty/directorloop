import { useEffect, useState } from "react";
import { Link, useLocation, useParams, useSearchParams } from "react-router-dom";
import { getClient } from "../api/client";
import { isTerminal, type Audit, type AuditSummary, type RunSummary } from "../api/types";
import { CausalLaunch } from "../components/CausalLaunch";
import { DecisionTag } from "../components/Attempts";
import { ExternalIcon } from "../components/Icons";
import { EvaluationProtocol } from "../components/Judge";
import { JudgeView } from "../components/JudgeView";
import { StageRail } from "../components/StageRail";
import { Frame, useVideoTitle } from "../components/Shell";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { VideoPlayer } from "../components/VideoPlayer";
import { useAppState } from "../hooks/useAppState";
import { useJob } from "../hooks/useJob";
import { capitalize, errorMessage, fmtDateTime, newIdempotencyKey } from "../lib/format";
import { eventValue } from "../lib/jobs";
import { AUDIENCES, DEFAULT_OBJECTIVE, PLATFORMS } from "../lib/labels";

interface Settings {
  objective: string;
  platform: string;
  audience: string;
}

/** One line per earlier run: how many edits were tried and what was kept. */
function runHeadline(r: RunSummary): string {
  if (r.status === "failed") return "The run did not finish";
  if (r.status === "running") return "Still running";
  const tried = r.decisions.filter((d) => d !== "stop").length;
  if (r.decisions.includes("accept")) return `${tried} edit${tried === 1 ? "" : "s"} tried, a revision was kept`;
  if (tried > 0) return `${tried} edit${tried === 1 ? "" : "s"} tried, the original was kept`;
  return "No edit was possible, the original was kept";
}

export function JudgePage() {
  const { videoId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const app = useAppState();
  const state = location.state as { jobId?: string; settings?: Settings } | null;
  const [jobId, setJobId] = useState<string | null>(state?.jobId ?? null);
  const tracker = useJob(jobId);
  const [audits, setAudits] = useState<AuditSummary[] | null>(null);
  const [audit, setAudit] = useState<Audit | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [tick, setTick] = useState(0);
  const [actionError, setActionError] = useState<string | null>(null);

  const video = app.videos.find((v) => v.video_id === videoId) ?? null;
  const title = useVideoTitle(videoId);
  const needsRights = video?.edit_permission === "requires_owner_confirmation";
  const auditParam = params.get("audit");

  useEffect(() => {
    let cancelled = false;
    getClient()
      .then(async (c) => {
        const [a, r] = await Promise.allSettled([c.listAudits(videoId), c.listRuns()]);
        if (cancelled) return;
        if (a.status === "fulfilled") setAudits(a.value);
        else setError(errorMessage(a.reason));
        if (r.status === "fulfilled") setRuns(r.value.filter((x) => x.video_id === videoId));
      })
      .catch((err: unknown) => !cancelled && setError(errorMessage(err)));
    return () => {
      cancelled = true;
    };
  }, [videoId, tick]);

  // The audit to show: the one a finished job produced, the one in the URL, or the newest stored review.
  const jobAuditId = eventValue(tracker.events, "audit_id");
  const chosenId = jobAuditId ?? auditParam ?? audits?.[0]?.id ?? null;
  useEffect(() => {
    if (!chosenId) return;
    let cancelled = false;
    getClient()
      .then((c) => c.getAudit(chosenId))
      .then((a) => !cancelled && setAudit(a))
      .catch((err: unknown) => !cancelled && setError(errorMessage(err)));
    return () => {
      cancelled = true;
    };
  }, [chosenId]);

  useEffect(() => {
    if (tracker.job && isTerminal(tracker.job.state)) setTick((t) => t + 1);
  }, [tracker.job?.state]);

  const judging = tracker.job !== null && !isTerminal(tracker.job.state);
  const judgeFailed = tracker.job?.state === "FAILED" || tracker.job?.state === "CANCELED";

  const judgeAgain = async () => {
    setActionError(null);
    try {
      const c = await getClient();
      const { job_id } = await c.startAudit(videoId, newIdempotencyKey());
      setAudit(null);
      setParams({}, { replace: true });
      setJobId(job_id);
    } catch (err) {
      setActionError(errorMessage(err));
    }
  };

  const mediaUrl = audit?.media_url ?? video?.media_url ?? `/media/source/${videoId}.mp4`;
  const outsideLibrary = !video && !app.videosLoading && !app.videosError;
  const initialObjective = [state?.settings?.objective || DEFAULT_OBJECTIVE, state?.settings?.platform && state.settings.platform !== PLATFORMS[0] ? `Platform: ${state.settings.platform}.` : "", state?.settings?.audience && state.settings.audience !== AUDIENCES[0] ? `Audience: ${state.settings.audience}.` : ""].filter(Boolean).join(" ").slice(0, 500);
  const reviewingRevision = /^v[1-9]\d*(?:-|$)/.test(audit?.version_id ?? "");
  const cta = outsideLibrary || reviewingRevision ? <p className="callout">{reviewingRevision ? "This is a saved revision review. Upload this revision to start a new experiment on those exact bytes." : "Upload the source file to start a controlled experiment on this video."} <Link className="text-link" to="/">Choose source video</Link></p> : <CausalLaunch key={videoId} videoId={videoId} auditId={audit?.status === "complete" ? audit.id : undefined} needsRights={needsRights} disabled={judging} initialObjective={initialObjective} />;

  return (
    <div className="run">
      <header className="run-head">
        <div className="run-title-row">
          <h1 className="run-title">{title || videoId}</h1>
          {audit ? <span className={`status-chip status-${audit.status === "complete" ? "completed" : "failed"}`}>{audit.status === "complete" ? "Judged" : "Incomplete review"}</span> : null}
          {audit?.weave_url ? (
            <a className="btn btn-quiet btn-small run-trace" href={audit.weave_url} target="_blank" rel="noreferrer">
              View trace <ExternalIcon size={14} />
            </a>
          ) : null}
        </div>
        <div className="run-meta">
          {audit ? <span className="mono">{audit.id}</span> : null}
          {audit ? <span>reviewed {fmtDateTime(audit.created_at)}</span> : null}
          {video?.platform ? <span>from {video.platform}</span> : null}
          {audits && audits.length > 1 ? (
            <label className="inline-select">
              <span className="muted">Review</span>
              <select value={chosenId ?? ""} onChange={(e) => setParams({ audit: e.target.value }, { replace: true })}>
                {audits.map((a) => (
                  <option key={a.id} value={a.id}>
                    {fmtDateTime(a.created_at)} · {a.findings} weak moment{a.findings === 1 ? "" : "s"}
                    {a.version_id !== "v0" ? ` · ${a.version_id}` : ""}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
          {audit && app.feature("runs") && !outsideLibrary ? (
            <button type="button" className="btn btn-quiet btn-small" onClick={judgeAgain} disabled={judging}>
              Judge again
            </button>
          ) : null}
        </div>
      </header>

      {judging || (judgeFailed && !audit) ? (
        <div className="judge">
          <div className="judge-media">
            <VideoPlayer src={mediaUrl} className="player-hero" />
          </div>
          <div className="judge-panel">
            <Frame>01 Judge</Frame>
            <h2 className="working-title">{judging ? "Watching it like a cold viewer" : "The review did not finish"}</h2>
            {tracker.job ? <StageRail job={tracker.job} events={tracker.events} elapsedMs={tracker.elapsedMs} transport={tracker.transport} error={tracker.error} expected={["ANALYZING", "COLD_REVIEW", "DIAGNOSING", "VERIFYING", "AUDIT_DONE"]} /> : null}
          </div>
        </div>
      ) : audit ? (
        <JudgeView audit={audit} mediaUrl={mediaUrl} cta={cta} />
      ) : error ? (
        <ErrorState title="Could not load the review" detail={error} onRetry={() => setTick((t) => t + 1)} />
      ) : audits === null ? (
        <Loading what="the review" />
      ) : !video && !app.videosLoading && !app.videosError && audits.length === 0 ? (
        <EmptyState
          title="Video not found"
          detail={`There is no stored video called ${videoId}. Upload it or paste its link to judge it.`}
          action={
            <Link className="btn" to="/">
              New video
            </Link>
          }
        />
      ) : (
        <div className="judge">
          <div className="judge-media">
            <VideoPlayer src={mediaUrl} className="player-hero" />
          </div>
          <div className="judge-panel">
            <EmptyState
              title="Not judged yet"
              detail="A cold viewer watches it, marks the moments where attention or understanding may weaken, and explains why with the frames it saw."
              action={
                <button type="button" className="btn btn-primary btn-hero" onClick={judgeAgain} disabled={!app.feature("runs")}>
                  Judge Video
                </button>
              }
            />
            {actionError ? (
              <p className="callout callout-bad" role="alert">
                {actionError}
              </p>
            ) : null}
          </div>
        </div>
      )}

      {!audit && !judging && video ? cta : null}
      <p className="small"><Link className="text-link" to="/runs">Browse controlled experiment sessions and previous runs</Link></p>

      {runs.length ? (
        <section className="run-history" aria-label="Earlier improvement runs">
          <div className="section-line">
            <h2 className="section-name">Earlier improvement runs on this video</h2>
          </div>
          <ul className="recent-list">
            {runs.map((r) => (
              <li key={r.id}>
                <Link to={`/runs/${encodeURIComponent(r.id)}`} className="recent-row">
                  <span className="recent-title">{runHeadline(r)}</span>
                  <span className="recent-line">{capitalize(r.final_decision || r.stop_reason || r.status)}</span>
                  <span className="mono muted small">{fmtDateTime(r.created_at)}</span>
                  <DecisionTag decision={r.decisions.includes("accept") ? "accept" : r.status === "failed" ? "incomplete_keep_current" : r.iterations ? "reject_keep_current" : "stop"} />
                </Link>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {audit ? (
        <section className="details" aria-label="Details">
          <details className="disclosure">
            <summary>Evaluation protocol</summary>
            <div className="disclosure-body protocol-grid">
              <EvaluationProtocol audit={audit} title="What this review received" />
            </div>
          </details>
        </section>
      ) : null}
    </div>
  );
}
