import { useEffect, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { getClient, isNotFound } from "../api/client";
import type { ScreeningReport, ScreeningStatus, ScreenObservation } from "../api/screening";
import { isTerminal } from "../api/types";
import { ExternalIcon } from "../components/Icons";
import { Frame } from "../components/Shell";
import { StageRail } from "../components/StageRail";
import { VideoPlayer, type PlayerHandle } from "../components/VideoPlayer";
import { useAppState } from "../hooks/useAppState";
import { useJob } from "../hooks/useJob";
import { errorMessage } from "../lib/format";
import { screeningBudget, screeningCitation, screeningJobId, screeningJobMatches, screeningRisk, screeningTitle } from "../lib/screening";
import "./ScreeningPage.css";

const seconds = (ms: number) => `${(ms / 1000).toFixed(1)}s`;
const observationLabel: Record<ScreenObservation["kind"], string> = {
  visible_fact: "Model observation", caption_claim: "On-screen claim", asr_claim: "Transcript claim",
  inference: "Interpretation", unknown: "Not visible / unknown",
};

export function ScreeningPage() {
  const { screenId = "" } = useParams();
  const [params] = useSearchParams();
  const requestedJobId = params.get("job");
  const jobId = screeningJobId(screenId, requestedJobId);
  const tracker = useJob(jobId);
  const app = useAppState();
  const [storedReport, setReport] = useState<ScreeningReport | null>(null);
  const report = storedReport?.id === screenId ? storedReport : null;
  const [status, setStatus] = useState<ScreeningStatus | undefined>(app.health?.screening);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState(0);
  const [observation, setObservation] = useState(0);
  const [frameTime, setFrameTime] = useState<number | null>(null);
  const [canceling, setCanceling] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);
  const [cancelRequested, setCancelRequested] = useState(false);
  const player = useRef<PlayerHandle | null>(null);
  const jobMismatch = Boolean((requestedJobId !== null && !jobId) || (tracker.job && !screeningJobMatches(screenId, tracker.job, report?.video_id)));
  const job = !jobMismatch && screeningJobMatches(screenId, tracker.job, report?.video_id) ? tracker.job : null;

  useEffect(() => {
    setReport(null); setSelected(0); setObservation(0); setFrameTime(null);
    setError(null); setCancelError(null); setCancelRequested(false); setCanceling(false);
  }, [screenId]);

  useEffect(() => {
    let canceled = false;
    const refresh = async () => {
      try {
        const client = await getClient();
        const result = await client.getScreening(screenId);
        if (!canceled) { setReport(result); setError(null); }
      } catch (err) {
        if (!canceled && !(isNotFound(err) && jobId && !jobMismatch && !tracker.missing && (!job || !isTerminal(job.state)))) setError(errorMessage(err));
      }
    };
    void refresh();
    const polling = report?.status === "running" || (!report && jobId && !jobMismatch && !tracker.missing && (!job || !isTerminal(job.state)));
    const timer = polling ? window.setInterval(() => { void refresh(); }, 1800) : undefined;
    return () => { canceled = true; window.clearInterval(timer); };
  }, [screenId, jobId, jobMismatch, tracker.missing, job?.state, report?.status]);

  useEffect(() => {
    let canceled = false;
    getClient().then(c => c.getHealth()).then(h => { if (!canceled) setStatus(h.screening); }).catch(() => undefined);
    return () => { canceled = true; };
  }, [job?.state]);

  const videoId = report?.video_id ?? params.get("video") ?? "";
  const video = app.videos.find(v => v.video_id === videoId);
  const moment = report?.windows[selected];
  const judgment = moment?.judgment;
  const fact = judgment?.observations[observation];
  const citations = screeningCitation(moment, fact);
  const citedTime = frameTime !== null && citations.validTimes.includes(frameTime) ? frameTime : citations.validTimes[0];
  const frame = moment?.evidence_frames.filter(f => f.t_ms === citedTime).sort((a, b) => b.width - a.width)[0];
  const running = job ? !isTerminal(job.state) : report?.status === "running";
  const selectWindow = (index: number) => {
    setSelected(index); setObservation(0); setFrameTime(null);
    const moment = report?.windows[index];
    if (moment) player.current?.seek(moment.start_ms);
  };
  const inspect = (timestamp: number) => {
    if (!citations.validTimes.includes(timestamp)) return;
    setFrameTime(timestamp); player.current?.seek(timestamp);
  };
  const cancel = async () => {
    if (!job || !jobId || !screeningJobMatches(screenId, job, report?.video_id) || isTerminal(job.state) || canceling || cancelRequested || job.cancel_requested) return;
    setCanceling(true); setCancelError(null);
    try {
      const updated = await (await getClient()).cancelJob(jobId);
      setCancelRequested(Boolean(updated.cancel_requested || isTerminal(updated.state)));
    } catch (err) { setCancelError(errorMessage(err)); }
    finally { setCanceling(false); }
  };

  return <section className="quick-screen">
    <header className="screen-heading">
      <div><Frame>Quick screen · model suggestions</Frame><h1>{!report && job?.state === "FAILED" ? "Screening stopped" : !report && job?.state === "CANCELED" ? "Screening canceled" : screeningTitle(report)}</h1><p>{video?.title ?? videoId}</p></div>
      {report?.weave_url ? <a className="btn btn-quiet" href={report.weave_url} target="_blank" rel="noreferrer">View trace <ExternalIcon size={14} /></a> : null}
    </header>
    <div className="screen-workspace">
      <div className="screen-video"><VideoPlayer ref={player} src={report?.media_url ?? video?.media_url ?? null} /></div>
      <div className="screen-inspect">
        <p className="screen-boundary">Sampled moments. Check the evidence before making an edit.</p>
        {report?.windows.length ? <div className="screen-moments" aria-label="Sampled video moments">
          {report.windows.map((moment, index) => <button key={moment.end_ms} type="button" className={selected === index ? "is-selected" : ""} aria-pressed={selected === index} onClick={() => selectWindow(index)}>
            <span className="mono">{seconds(moment.start_ms)}–{seconds(moment.end_ms)}</span>
            <strong>{screeningRisk(moment)}</strong>
          </button>)}
        </div> : null}
        {moment?.validation_issues.map((issue, index) => <p key={index} className="callout callout-bad" role="alert">{issue}</p>)}
        {judgment ? <>
          <div className="screen-evidence">
            <div className="screen-observations" aria-label="Model evidence">
              {judgment.observations.map((item, index) => <button key={index} type="button" className={observation === index ? "is-selected" : ""} aria-pressed={observation === index} onClick={() => { setObservation(index); setFrameTime(null); }}>
                <span>{observationLabel[item.kind]}</span><p>{item.text}</p>
              </button>)}
            </div>
            <div className="screen-proof">
              {frame?.media_url ? <img src={frame.media_url} alt={`Frame supplied to the model at ${seconds(frame.t_ms)}`} /> : <div className="screen-proof-empty">{citations.quoteValid ? "Supplied transcript quote" : "No verified input citation"}</div>}
              <div className="screen-citations">{citations.validTimes.map(t => <button className="text-link" type="button" key={t} onClick={() => inspect(t)}>{seconds(t)}</button>)}
                {citations.invalidTimes.map(t => <span className="small muted" key={t}>{seconds(t)} · not supplied</span>)}
              </div>
              {fact?.asr_quote ? <>
                {citations.quoteValid === false ? <p className="callout callout-bad">Model quote absent from supplied transcript.</p> : null}
                <blockquote aria-label={citations.quoteValid ? "Quote found in supplied transcript" : "Unverified model quote"}>{fact.asr_quote}</blockquote>
              </> : null}
              <small>Input matches do not prove the interpretation.</small>
            </div>
          </div>
          <details className="disclosure screen-details"><summary>Model summary</summary><p>{judgment.understanding}</p></details>
          <details className="disclosure screen-details"><summary>Suggestion and uncertainty</summary>
            <p>{judgment.suggestion}</p>
            {judgment.uncertainties.map((uncertainty, index) => <p key={index}>{uncertainty}</p>)}
            {moment?.attention_context === "last_endcard" || moment?.attention_context === "last_signoff" ? <p>Leaving at the end does not, by itself, justify an edit.</p> : null}
          </details>
        </> : moment?.status === "needs_review" ? <p className="screen-boundary">The saved response needs review. Open the trace for details.</p> : running ? <p className="screen-awaiting">Reading the next moment…</p> : null}
        {jobMismatch ? <p className="callout callout-bad" role="alert">This job link belongs to another run. Its status and controls are hidden.</p> : null}
        {error || report?.error ? <p className="callout callout-bad" role="alert">{error ?? report?.error}</p> : null}
        {job && !isTerminal(job.state) ? <button className="btn btn-quiet btn-small" type="button" onClick={cancel} disabled={canceling || cancelRequested || job.cancel_requested}>{canceling || cancelRequested || job.cancel_requested ? "Cancel requested" : "Cancel screening"}</button> : null}
        {cancelError ? <p className="callout callout-bad" role="alert">{cancelError}</p> : null}
        {job && (running || job.state !== "COMPLETED") ? <StageRail job={job} events={tracker.events} elapsedMs={tracker.elapsedMs} transport={tracker.transport} error={tracker.error} expected={[]} /> : null}
        <details className="disclosure screen-details"><summary>Run details</summary>
          <p>{screeningBudget(status)}</p><p>These suggestions do not measure human retention or predict views.</p>
          {report ? <p>{report.model_calls} logical requests · {report.input_tokens ?? "Unknown"} input tokens · {report.output_tokens ?? "Unknown"} output tokens</p> : null}
          <p className="mono">{report?.protocol_fingerprint}</p>
          {moment?.prefix_asr_text ? <p>{moment.prefix_asr_text}</p> : null}
        </details>
      </div>
    </div>
    <footer className="screen-footer"><Link className="text-link" to="/">Screen another video</Link>
      {videoId ? <Link className="text-link" to={`/judge/${encodeURIComponent(videoId)}`}>Open full-review workspace · OpenAI</Link> : null}
    </footer>
  </section>;
}
