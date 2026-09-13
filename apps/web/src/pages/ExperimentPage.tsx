import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { apiMode, getClient } from "../api/client";
import { EXPERIMENT_STAGES, isTerminal, type DesignPreview, type ExperimentDetail, type VideoDetail } from "../api/types";
import { RunBadge, OutcomePill } from "../components/Badge";
import { HypothesisCard, MemoryComparison, PolicyUpdates, RankingList, WeakRegionLine } from "../components/Experiment";
import { ExperimentComparison } from "../components/ExperimentComparison";
import { StageList } from "../components/StageList";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { GenomeTimeline } from "../components/Timelines";
import { VideoPlayer, type PlayerHandle } from "../components/VideoPlayer";
import { useAppState } from "../hooks/useAppState";
import { useJob } from "../hooks/useJob";
import { errorMessage, fmtDateTime, fmtElapsed, fmtS, newIdempotencyKey } from "../lib/format";
import { armName, decisionName, familyName, mutationName } from "../lib/labels";

export function ExperimentPage() {
  const { id, videoId: routeVideoId } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const app = useAppState();
  const [exp, setExp] = useState<ExperimentDetail | null>(null);
  const [expError, setExpError] = useState<string | null>(null);
  const [video, setVideo] = useState<VideoDetail | null>(null);
  const [videoError, setVideoError] = useState<string | null>(null);
  const [designs, setDesigns] = useState<{ none: DesignPreview | null; learned: DesignPreview | null }>({ none: null, learned: null });
  const [chapter, setChapter] = useState(0);
  const [starting, setStarting] = useState(false);
  const [armLabel, setArmLabel] = useState("control");
  const [cursorMs, setCursorMs] = useState(0);
  const [jobId, setJobId] = useState<string | null>((location.state as { jobId?: string } | null)?.jobId ?? null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const player = useRef<PlayerHandle>(null);
  const tracker = useJob(jobId);

  const videoId = routeVideoId ?? (exp && exp.id === id ? exp.video_id : null);

  useEffect(() => {
    setChapter(0);
    setCursorMs(0);
    setVideo(null);
    setDesigns({ none: null, learned: null });
    setActionError(null);
    setJobId((location.state as { jobId?: string } | null)?.jobId ?? null);
  }, [id, routeVideoId]);

  useEffect(() => { window.scrollTo({ top: 0, behavior: "instant" }); }, [chapter]);

  useEffect(() => {
    if (!id) {
      setExp(null);
      return;
    }
    let cancelled = false;
    setExpError(null);
    setExp(null);
    getClient()
      .then((c) => c.getExperiment(id))
      .then((e) => {
        if (cancelled) return;
        setExp(e);
        setArmLabel("control");
        app.setVideoId(e.video_id);
      })
      .catch((err: unknown) => !cancelled && setExpError(errorMessage(err)));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, tick]);

  useEffect(() => {
    if (!videoId) return;
    let cancelled = false;
    setVideoError(null);
    if (routeVideoId) app.setVideoId(routeVideoId);
    getClient()
      .then(async (c) => {
        const [v, none, learned] = await Promise.allSettled([c.getVideo(videoId), c.getDesign(videoId, "none"), c.getDesign(videoId, "learned")]);
        if (cancelled) return;
        if (v.status === "fulfilled") setVideo(v.value);
        else setVideoError(errorMessage(v.reason));
        setDesigns({ none: none.status === "fulfilled" ? none.value : null, learned: learned.status === "fulfilled" ? learned.value : null });
      })
      .catch((err: unknown) => !cancelled && setVideoError(errorMessage(err)));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [videoId, tick]);

  // When a run finishes, show its experiment and refresh the policy version.
  useEffect(() => {
    const job = tracker.job;
    if (!job || job.state !== "COMPLETED" || !job.experiment_id) return;
    app.markSessionExperiment(job.experiment_id);
    if (job.experiment_id !== id) navigate(`/research/experiments/${encodeURIComponent(job.experiment_id)}`, { replace: true, state: { jobId: job.id } });
    else setTick((t) => t + 1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tracker.job?.state, tracker.job?.experiment_id]);

  const running = tracker.job !== null && !isTerminal(tracker.job.state);
  const arms = exp?.arms ?? [];
  const control = arms.find((a) => a.label === "control") ?? null;
  const hypotheses = exp?.hypotheses ?? video?.investigation?.hypotheses ?? [];
  const rankings = exp?.ranking ?? designs.learned?.rankings ?? [];
  const runKind = exp ? (app.sessionExperiments.has(exp.id) ? (apiMode() === "live" ? "live" : "replay") : "recorded") : null;
  const question = useMemo(() => (exp ? exp.question.replace(/[a-z]+(?:_[a-z]+)+/g, (m) => familyName(m).toLowerCase()) : null), [exp]);

  const run = async () => {
    if (!videoId || starting || running) return;
    setStarting(true);
    setActionError(null);
    try {
      const c = await getClient();
      const { job_id } = await c.startExperiment({ video_id: videoId, policy_mode: "learned", max_arms: 3, record_policy: true, idempotency_key: newIdempotencyKey() });
      setJobId(job_id);
    } catch (err) {
      setActionError(errorMessage(err));
    } finally { setStarting(false); }
  };

  if (expError) return <ErrorState title="Could not load the experiment" detail={expError} onRetry={() => setTick((t) => t + 1)} />;
  if (id && !exp) return <Loading what="the experiment" />;
  if (!videoId) return <EmptyState title="No video selected" />;

  const decisionText = exp?.decision ? arms.reduce((text, a) => text.split(a.id).join(armName(a.label)), exp.decision.reason) : null;

  const selectedRanking = rankings.find(r => r.selected) ?? rankings[0];
  const variants = arms.filter(a => a.label !== "control");
  const candidate = variants.find(a => a.label === armLabel) ?? variants[0] ?? null;
  const beat = video?.genome?.beats.find(b => cursorMs >= b.start_ms && cursorMs < b.end_ms);

  return <div className="page experiment-studio">
    <div className="study-header"><div><span className="eyebrow">Controlled creative experiment</span><h1>{video?.title ?? videoId}</h1></div>{runKind ? <RunBadge kind={runKind} /> : null}</div>
    <nav className="chapter-nav" aria-label="Experiment chapters">{(["Observe", "Hypothesize", "Test & compare", "Learn"] as const).map((name,i) => <button key={name} className={chapter === i ? "is-active" : ""} aria-pressed={chapter === i} onClick={() => setChapter(i)}><span>{String(i+1).padStart(2,"0")}</span>{name}</button>)}</nav>
    {videoError ? <ErrorState title="Could not load the video" detail={videoError} onRetry={() => setTick(t => t+1)} /> : null}
    {chapter === 0 ? <div className="study-observe">
      <div className="study-video"><VideoPlayer ref={player} src={control?.media_url ?? video?.media_url ?? null} onTime={setCursorMs} label="Original" /><p className="media-caption">{fmtS(cursorMs)} / {fmtS(video?.duration_ms ?? 0)}</p></div>
      <div className="study-reading"><span className="eyebrow">What the agent sees</span><h2 className="editorial-title">{beat?.role ?? "Watch the original."}</h2>{video?.genome ? <p className="study-transcript">{beat ? `“${beat.text}”` : "Select a beat to inspect the transcript and creative structure."}</p> : null}
        <WeakRegionLine region={video?.investigation?.weak_region ?? null} retentionNote={video?.investigation?.retention_note ?? null} />
        {video?.genome ? <GenomeTimeline genome={video.genome} weakRegion={video.investigation?.weak_region ?? null} cursorMs={cursorMs} onSeek={ms => player.current?.seek(ms)} /> : <p className="muted">No creative analysis is saved for this video. A controlled experiment creates it when you explicitly start one.</p>}
        <p className="muted">Structural observations identify a region to investigate. They do not measure audience attention.</p>
        <div className="study-next"><Link className="text-link" to={`/judge/${encodeURIComponent(videoId)}`}>{video?.genome ? "Open chronological attention review" : "Open experiment controls"}</Link><button className="btn btn-primary" onClick={() => setChapter(1)}>Explore hypotheses <span aria-hidden="true">→</span></button></div>
      </div>
    </div> : null}
    {chapter === 1 ? <div className="study-hypotheses"><div className="study-intro"><span className="eyebrow">Separate symptom, cause, and edit</span><h2 className="editorial-title">Why might interest<br /><span>weaken here?</span></h2><p>{question ?? "Compare the evidence before choosing what to change."}</p></div>
      <div className="hypothesis-spread">{hypotheses.map((h,i) => <HypothesisCard key={h.id} h={h} rank={i+1} />)}{!hypotheses.length ? <EmptyState title="No competing hypotheses recorded" /> : null}</div>
      <div className="study-ranking"><span className="eyebrow">Experiments · ranked by the agent</span><RankingList rankings={rankings} arms={exp ? arms : null} /></div>
      {selectedRanking ? <div className="selected-experiment"><div><span className="eyebrow">{exp ? "First experiment selected" : "First proposed experiment"}</span><h3>{mutationName(selectedRanking.mutation)}</h3><p>{selectedRanking.description}</p><details className="disclosure"><summary>Why this experiment?</summary><p>{selectedRanking.reason}</p></details></div><div className="frozen"><span className="eyebrow">Everything else frozen</span><p>Controlled edit plan</p><ul>{selectedRanking.protected_variables.map(v => <li key={v}>{v}</li>)}</ul></div></div> : null}
      <button className="btn btn-primary" onClick={() => setChapter(2)}>{exp ? "Inspect the actual variants" : "Continue to experiment"} <span aria-hidden="true">→</span></button>
    </div> : null}
    {chapter === 2 ? <>
      {variants.length ? <div className="variant-tabs" role="group" aria-label="Variant to compare">{variants.map(a => <button className={candidate?.id === a.id ? "is-active" : ""} key={a.id} onClick={() => setArmLabel(a.label)} aria-pressed={candidate?.id === a.id}>Variant {a.label}<span>{mutationName(a.mutation?.type)}</span><OutcomePill outcome={a.outcome} /></button>)}</div> : null}
      {candidate ? <ExperimentComparison key={candidate.id} original={control} candidate={candidate} /> : <div className="experiment-start"><h2 className="editorial-title">Ready to test<br /><span>one variable at a time.</span></h2><p>Render the ranked edits, evaluate each against the original, and record the result.</p><button className="btn btn-primary" onClick={run} disabled={starting || running || !video}>{starting || running ? "Experiment running" : apiMode() === "mock" ? "Replay recorded experiment" : "Run experiment"}</button><p className="muted">Up to three variants. Each is an independent controlled edit.</p></div>}
      {actionError ? <p className="callout callout-bad" role="alert">{actionError}</p> : null}
      {tracker.job ? <StageList job={tracker.job} events={tracker.events} elapsedMs={tracker.elapsedMs} transport={tracker.transport} error={tracker.error} expected={EXPERIMENT_STAGES} title="Experiment execution" /> : null}
      {exp ? <div className="study-next"><span className="muted">Every outcome is evidence for the next decision.</span><button className="btn btn-primary" onClick={() => setChapter(3)}>What did DirectorLoop learn? <span aria-hidden="true">→</span></button></div> : null}
    </> : null}
    {chapter === 3 ? <div className="study-learning"><span className="eyebrow">DirectorLoop learned</span><h2 className="editorial-title">The result becomes<br /><span>the next decision.</span></h2>
      {exp ? <><p className="learning-decision">{decisionName(exp.decision?.outcome)}</p><p>{decisionText ?? "No completed decision recorded."}</p><PolicyUpdates updates={exp.policy_updates} /></> : <p className="muted">No experiment has been completed for this video yet.</p>}
      <details className="disclosure"><summary>Current ranking with and without memory</summary><MemoryComparison none={designs.none} learned={designs.learned} /></details>
      <div className="study-next"><Link className="btn btn-primary" to="/research/transfer">See how evidence changes the next video <span aria-hidden="true">→</span></Link><Link className="text-link" to="/">Analyze another video</Link></div>
    </div> : null}
    {exp ? <details className="disclosure study-technical"><summary>Run provenance, frozen suite, and exact evidence</summary><div className="disclosure-body"><p>{fmtDateTime(exp.created_at)} · {fmtElapsed(exp.total_ms)} · {exp.model_calls} model calls · Policy v{exp.policy_version_before} → v{exp.policy_version_after ?? exp.policy_version_before}</p>{exp.weave_url ? <a className="text-link" href={exp.weave_url} target="_blank" rel="noreferrer">Open Weave trace</a> : <p>No trace recorded.</p>}<p className="mono">Suite {exp.suite.id} · {exp.suite.hash}</p><ul>{exp.notes.map(n => <li key={n}>{n}</li>)}</ul><details><summary>Experiment JSON</summary><pre>{JSON.stringify(exp,null,2)}</pre></details></div></details> : null}
  </div>;
}
