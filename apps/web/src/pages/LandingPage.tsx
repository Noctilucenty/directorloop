import { useEffect, useRef, useState, type DragEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ApiError, apiMode, getClient } from "../api/client";
import type { ABCSummary, RunSummary, TransferReport, VideoSummary } from "../api/types";
import { DecisionTag } from "../components/Attempts";
import { LoopSculpture } from "../components/Motion";
import { LinkIcon } from "../components/Icons";
import { Frame } from "../components/Shell";
import { stageWord } from "../components/StageRail";
import { useAppState } from "../hooks/useAppState";
import { errorMessage, fmtDateTime, newIdempotencyKey } from "../lib/format";
import { eventValue, waitForJob } from "../lib/jobs";
import { AUDIENCES, DEFAULT_OBJECTIVE, mutationName, PLATFORMS } from "../lib/labels";
import { screeningBudget } from "../lib/screening";

const ACCEPTED = [".mp4", ".mov", ".m4v", ".webm"];
const DEFAULT_MAX_BYTES = 200 * 1024 * 1024;
const MB = (n: number) => `${(n / (1024 * 1024)).toFixed(1)} MB`;

type Source = { kind: "none" } | { kind: "file"; file: File } | { kind: "link"; url: string } | { kind: "stored"; videoId: string };

function checkFile(file: File, maxBytes: number): string | null {
  const ext = file.name.slice(file.name.lastIndexOf(".")).toLowerCase();
  if (!ACCEPTED.includes(ext)) return `That file type is not supported. Use ${ACCEPTED.join(", ")}.`;
  if (file.size > maxBytes) return `That file is ${MB(file.size)}; the limit is ${MB(maxBytes)}.`;
  return null;
}

function looksLikeUrl(u: string): boolean {
  try {
    const x = new URL(u.trim());
    return x.protocol === "https:" || x.protocol === "http:";
  } catch {
    return false;
  }
}

function SourcePicker({ label, value, onChange, disabled, videos, linksOn, maxBytes, compact, onError }: { label: string; value: Source; onChange: (s: Source) => void; disabled: boolean; videos: VideoSummary[]; linksOn: boolean; maxBytes: number; compact?: boolean; onError: (m: string | null) => void }) {
  const input = useRef<HTMLInputElement | null>(null);
  const [over, setOver] = useState(false);
  const pick = (f: File | null) => {
    if (!f) return onChange({ kind: "none" });
    const problem = checkFile(f, maxBytes);
    onError(problem);
    if (!problem) onChange({ kind: "file", file: f });
  };
  const onDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setOver(false);
    if (!disabled) pick(e.dataTransfer.files?.[0] ?? null);
  };
  const demo = videos.filter((v) => v.role !== "reference");
  const refs = videos.filter((v) => v.role === "reference");
  return (
    <div className={`source${compact ? " source-compact" : ""}`} data-tilt>
      <div
        className={`drop${over ? " is-over" : ""}${value.kind === "file" ? " has-file" : ""}${disabled ? " is-disabled" : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          if (!disabled) setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={onDrop}
      >
        <LoopSculpture compact />
        {value.kind === "file" ? (
          <span className="drop-file">
            <span className="drop-name">{value.file.name}</span>
            <span className="mono muted">{MB(value.file.size)}</span>
          </span>
        ) : (
          <span className="drop-hint">{value.kind === "stored" ? videos.find(v => v.video_id === value.videoId)?.title : label}<small>MP4, MOV, M4V or WebM · up to {MB(maxBytes)}</small></span>
        )}
        <button type="button" className="btn btn-small" onClick={() => input.current?.click()} disabled={disabled}>
          {value.kind === "file" ? "Replace" : "Choose file"}
        </button>
        {value.kind === "file" ? (
          <button type="button" className="btn btn-small btn-quiet" onClick={() => onChange({ kind: "none" })} disabled={disabled}>
            Remove
          </button>
        ) : null}
        <input ref={input} type="file" accept={ACCEPTED.join(",") + ",video/*"} className="sr-only" tabIndex={-1} onChange={(e) => {
          pick(e.target.files?.[0] ?? null);
          e.target.value = "";
        }} />
      </div>
      <div className="source-row">
        <div className={`url-field${linksOn ? "" : " is-disabled"}${value.kind === "link" ? " has-value" : ""}`}>
          <LinkIcon size={18} />
          <input
            type="url"
            value={value.kind === "link" ? value.url : ""}
            onChange={(e) => onChange(e.target.value ? { kind: "link", url: e.target.value } : { kind: "none" })}
            placeholder={linksOn ? "Or paste a video URL" : "Video link import unavailable"}
            disabled={!linksOn || disabled}
            aria-label={`${label}: video link`}
          />
          {!linksOn ? <span className="sr-only">Links are not available yet</span> : null}
        </div>
        <label className="stored">
          <span className="sr-only">{`${label}: or a stored video`}</span>
          <select value={value.kind === "stored" ? value.videoId : ""} onChange={(e) => onChange(e.target.value ? { kind: "stored", videoId: e.target.value } : { kind: "none" })} disabled={disabled}>
            <option value="">Or choose a stored video</option>
            {demo.length ? (
              <optgroup label="Demo, uploaded and linked">
                {demo.map((v) => (
                  <option key={v.video_id} value={v.video_id}>
                    {v.title}
                  </option>
                ))}
              </optgroup>
            ) : null}
            {refs.length ? (
              <optgroup label="Reference shorts">
                {refs.map((v) => (
                  <option key={v.video_id} value={v.video_id}>
                    {v.title}
                  </option>
                ))}
              </optgroup>
            ) : null}
          </select>
        </label>
      </div>
      {linksOn && value.kind === "link" ? (
        <p className="link-note">A linked video can be judged right away. Making an edited version of it later asks you to confirm that you own it or may edit it, or to upload the original file.</p>
      ) : null}
    </div>
  );
}

function runLine(r: RunSummary): string {
  const attempts = r.iterations;
  if (r.status === "failed") return "did not finish";
  if (r.decisions.includes("accept")) return `${attempts} edit${attempts === 1 ? "" : "s"} tried, a revision was kept`;
  if (attempts > 0) return `${attempts} edit${attempts === 1 ? "" : "s"} tried, the original was kept`;
  return "no edit was possible, the original was kept";
}

export function LandingPage() {
  const app = useAppState();
  const navigate = useNavigate();
  const [mode, setMode] = useState<"single" | "ab">("single");
  const [single, setSingle] = useState<Source>({ kind: "none" });
  const [sideA, setSideA] = useState<Source>({ kind: "none" });
  const [sideB, setSideB] = useState<Source>({ kind: "none" });
  const [objective, setObjective] = useState(DEFAULT_OBJECTIVE);
  const [platform, setPlatform] = useState(PLATFORMS[0]);
  const [audience, setAudience] = useState(AUDIENCES[0]);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [rightsNeeded, setRightsNeeded] = useState<string | null>(null);
  const [rightsConfirmed, setRightsConfirmed] = useState(false);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [abcs, setAbcs] = useState<ABCSummary[]>([]);
  const [transfer, setTransfer] = useState<TransferReport | null>(null);
  const [latestAudit, setLatestAudit] = useState<string | null>(null);
  const [latestScreen, setLatestScreen] = useState<string | null>(null);
  const screenAttempt = useRef<{ videoId: string; key: string } | null>(null);
  const abort = useRef<AbortController | null>(null);

  const runsOn = app.feature("runs");
  const linksOn = app.feature("url_ingest");
  const abcOn = app.feature("abc");
  const screeningOn = app.feature("screening");

  useEffect(() => {
    let cancelled = false;
    getClient()
      .then(async (c) => {
        const [r, t, a] = await Promise.allSettled([c.listRuns(), c.listTransfers(), c.listAbc()]);
        if (cancelled) return;
        if (r.status === "fulfilled") setRuns(r.value.slice(0, 3));
        if (t.status === "fulfilled") setTransfer(t.value[0] ?? null);
        if (a.status === "fulfilled") setAbcs(a.value.slice(0, 2));
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
      abort.current?.abort();
    };
  }, []);

  // A stored video that was already judged can be opened without starting anything.
  useEffect(() => {
    setLatestAudit(null);
    setLatestScreen(null);
    if (single.kind !== "stored") return;
    let cancelled = false;
    getClient()
      .then((c) => c.listAudits(single.videoId))
      .then((list) => !cancelled && setLatestAudit(list[0]?.id ?? null))
      .catch(() => undefined);
    getClient().then(c => c.listScreenings(single.videoId)).then(list => { if (!cancelled) setLatestScreen(list[0]?.id ?? null); }).catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [single]);

  /** Turns a source into a registered video id: uploads a file, ingests a link and waits for it, or passes a stored id through. */
  const register = async (source: Source, name: string): Promise<string> => {
    const client = await getClient();
    if (source.kind === "stored") return source.videoId;
    if (source.kind === "file") {
      abort.current = new AbortController();
      const up = await client.uploadVideo(source.file, (sent, total) => setProgress(`${name}: uploading ${MB(sent)} of ${MB(total)}`), abort.current.signal);
      app.addVideo({ video_id: up.video_id, title: up.title, duration_ms: up.duration_ms, category: "educational_short", source: "upload", media_url: up.media_url, role: "upload", has_genome: false, retention_class: null, latest_experiment_id: null, edit_permission: "owner_upload" });
      return up.video_id;
    }
    if (source.kind === "link") {
      if (!looksLikeUrl(source.url)) throw new ApiError(422, "That does not look like a web address.");
      const started = await client.ingestUrl(source.url.trim(), newIdempotencyKey());
      setProgress(`${name}: ${started.platform ? `${started.platform} link` : "video link"} accepted`);
      const { job, events } = await waitForJob(started.job_id, (e) => setProgress(`${name}: ${stageWord(e.stage)}`), abort.current?.signal);
      if (job.state !== "COMPLETED") throw new ApiError(422, job.error || "The link could not be fetched.");
      const videoId = eventValue(events, "video_id");
      if (!videoId) throw new ApiError(500, "The link was fetched but no video was registered.");
      app.reloadVideos();
      return videoId;
    }
    throw new ApiError(422, "Choose a video first.");
  };

  const judge = async () => {
    setError(null);
    setBusy(true);
    try {
      const videoId = await register(single, "Video");
      setProgress("Starting the review");
      const client = await getClient();
      const { job_id } = await client.startAudit(videoId, newIdempotencyKey());
      navigate(`/judge/${encodeURIComponent(videoId)}`, { state: { jobId: job_id, settings: { objective, platform, audience } } });
    } catch (err) {
      setError(errorMessage(err));
      setProgress(null);
      setBusy(false);
    }
  };

  const screen = async () => {
    setError(null); setBusy(true);
    try {
      const videoId = await register(single, "Video");
      if (screenAttempt.current?.videoId !== videoId) screenAttempt.current = { videoId, key: newIdempotencyKey() };
      setProgress("Starting the quick screen");
      const client = await getClient();
      const result = await client.startScreening(videoId, screenAttempt.current.key);
      navigate(`/screen/${encodeURIComponent(result.screen_id)}?video=${encodeURIComponent(videoId)}&job=${encodeURIComponent(result.job_id)}`);
    } catch (err) {
      setError(errorMessage(err)); setProgress(null); setBusy(false);
    }
  };

  const compare = async () => {
    setError(null);
    setBusy(true);
    try {
      const a = await register(sideA, "A");
      const b = await register(sideB, "B");
      if (a === b) throw new ApiError(422, "A and B must be two different videos.");
      setProgress("Starting the comparison");
      const client = await getClient();
      const started = await client.startAbc({ a_video_id: a, b_video_id: b, objective: objective.trim().slice(0, 400), audience: audience === AUDIENCES[0] ? undefined : audience, constraints: [], iteration_budget: 2, owner_confirms_rights: rightsConfirmed || undefined, idempotency_key: newIdempotencyKey() });
      navigate(`/compare/${encodeURIComponent(started.abc_id)}`, { state: { jobId: started.job_id } });
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) setRightsNeeded(err.message);
      else setError(errorMessage(err));
      setProgress(null);
      setBusy(false);
    }
  };

  const hasSingle = single.kind === "file" || single.kind === "stored" || (single.kind === "link" && single.url.trim().length > 0);
  const hasSide = (s: Source) => s.kind === "file" || s.kind === "stored" || (s.kind === "link" && s.url.trim().length > 0);
  const judgeReady = runsOn && !busy && hasSingle && (single.kind !== "link" || linksOn);
  const compareReady = abcOn && !busy && hasSide(sideA) && hasSide(sideB) && objective.trim().length >= 3 && (!rightsNeeded || rightsConfirmed);
  const mockNote = apiMode() === "mock" ? "Recorded preview. Choose a stored video to explore an existing review." : null;

  return (
    <div className="landing">
      <section className="hero">
        <Frame>An AI creative research laboratory</Frame>
        <h1 className="hero-title">Find what loses the viewer.</h1>
        <p className="hero-sub">DirectorLoop watches your video like a new viewer, tests competing explanations, edits one variable at a time, and learns from the result.</p>
      </section>

      <section className={`input-card intake-${mode}`} aria-label="Video to analyze">
        <div className="input-card-head">
          <span className="seg seg-lg" role="radiogroup" aria-label="Mode">
            <button type="button" role="radio" aria-checked={mode === "single"} className={`seg-btn${mode === "single" ? " is-on" : ""}`} onClick={() => setMode("single")} disabled={busy}>
              Single video
            </button>
            <button type="button" role="radio" aria-checked={mode === "ab"} className={`seg-btn${mode === "ab" ? " is-on" : ""}`} onClick={() => setMode("ab")} disabled={busy}>
              Compare A/B
            </button>
          </span>
          <Frame>Video input</Frame>
        </div>

        {!linksOn && app.health?.capability_notes?.url_ingest ? <p className="callout">{app.health.capability_notes.url_ingest}</p> : null}
        {mode === "single" ? (
          <SourcePicker label="Drop a video here" value={single} onChange={setSingle} disabled={busy} videos={app.videos} linksOn={linksOn} maxBytes={app.health?.limits?.upload_max_bytes ?? DEFAULT_MAX_BYTES} onError={setError} />
        ) : (
          <div className="ab-grid">
            <div>
              <Frame>A</Frame>
              <SourcePicker label="Version A" value={sideA} onChange={setSideA} disabled={busy || !abcOn} videos={app.videos} linksOn={linksOn} maxBytes={app.health?.limits?.upload_max_bytes ?? DEFAULT_MAX_BYTES} compact onError={setError} />
            </div>
            <div>
              <Frame>B</Frame>
              <SourcePicker label="Version B" value={sideB} onChange={setSideB} disabled={busy || !abcOn} videos={app.videos} linksOn={linksOn} maxBytes={app.health?.limits?.upload_max_bytes ?? DEFAULT_MAX_BYTES} compact onError={setError} />
            </div>
          </div>
        )}

        <details className="settings-more">
          <summary>
            <span className="settings-more-title">Advanced controls</span>
            <span className="settings-more-values">
              {objective === DEFAULT_OBJECTIVE ? "Default goal" : "Custom goal"} · {platform} · {audience}
            </span>
          </summary>
          <div className="settings">
            <label className="setting setting-wide">
              <span className="field-label">Objective</span>
              <input value={objective} maxLength={400} onChange={(e) => setObjective(e.target.value)} disabled={busy} />
            </label>
            <label className="setting">
              <span className="field-label">Platform</span>
              <select value={platform} onChange={(e) => setPlatform(e.target.value)} disabled={busy}>
                {PLATFORMS.map((p) => (
                  <option key={p}>{p}</option>
                ))}
              </select>
            </label>
            <label className="setting">
              <span className="field-label">Audience</span>
              <select value={audience} onChange={(e) => setAudience(e.target.value)} disabled={busy}>
                {AUDIENCES.map((p) => (
                  <option key={p}>{p}</option>
                ))}
              </select>
            </label>
          </div>
          <p className="settings-note">
            {mode === "single"
              ? "Used only to choose which edits to try. The cold review never sees them, so it cannot be led."
              : "Used by the A/B comparison and the choice of C. The cold reviews of A, B and C never see them."}
          </p>
        </details>

        {mode === "ab" && rightsNeeded ? (
          <div className="rights">
            <p>{rightsNeeded}</p>
            <label className="check">
              <input type="checkbox" checked={rightsConfirmed} onChange={(e) => setRightsConfirmed(e.target.checked)} />
              <span>I own these videos or have the right to edit them</span>
            </label>
          </div>
        ) : null}

        <div className="input-actions">
          <span className="muted">
            {progress ??
              mockNote ??
              (mode === "single"
                ? single.kind === "stored" && latestAudit
                  ? "This video was judged before. Open that review, or judge it again."
                  : screeningOn ? screeningBudget(app.health?.screening) : "Begin with an unprimed, chronological review."
                : abcOn
                  ? "Both are reviewed cold, compared on the same questions, and a C is built only if the evidence supports one."
                  : "Compare A/B is not available yet.")}
          </span>
          <span className="input-buttons">
            {mode === "single" && single.kind === "stored" && latestScreen ? <Link className="btn btn-quiet" to={`/screen/${encodeURIComponent(latestScreen)}`}>Open screening</Link> : null}
            {mode === "single" && single.kind === "stored" && latestAudit ? (
              <Link className="btn btn-hero" to={`/judge/${encodeURIComponent(single.videoId)}`}>
                Open the review
              </Link>
            ) : null}
            {mode === "single" && screeningOn ? (
              <>
                <button type="button" className="btn btn-quiet" onClick={judge} disabled={!judgeReady}>Full review · OpenAI</button>
                <button type="button" className="btn btn-primary btn-hero" onClick={screen} disabled={busy || !hasSingle || !app.health?.screening?.available}>
                  {busy ? "Working" : "Quick screen"}
                </button>
              </>
            ) : mode === "single" ? (
              <button type="button" className="btn btn-primary btn-hero" onClick={judge} disabled={!judgeReady}>
                {busy ? "Working" : "Analyze video"}
              </button>
            ) : (
              <button type="button" className="btn btn-primary btn-hero" onClick={compare} disabled={!compareReady}>
                {busy ? "Working" : "Find what works in each"}
              </button>
            )}
          </span>
        </div>
        {error ? (
          <p className="callout callout-bad" role="alert">
            {error}
          </p>
        ) : null}
      </section>

      <div className="landing-loop" aria-label="The creative research loop">
        <span>Watch</span><i /><span>Notice</span><i /><span>Hypothesize</span><i /><span>Test</span><i /><span>Compare</span><i /><span className="accent-text">Learn</span>
      </div>
      <div className="landing-footnote"><span>Every experiment informs the next.</span><Link to="/research/transfer" className="text-link">Explore evidence memory <span aria-hidden="true">↗</span></Link></div>

      <section className="loop-story" aria-label="How the learning loop works">
        <div className="loop-story-art"><span className="eyebrow">A creative research loop</span><LoopSculpture /><p>Watch. Test. Learn.<br /><span>Then try something smarter.</span></p></div>
        <div className="loop-story-copy">
          <article className="loop-story-step"><span className="eyebrow">01 · Watch</span><h2>A fresh pair<br />of eyes.</h2><p>The agent encounters the video in order. It separates what happened from why it might have happened.</p></article>
          <article className="loop-story-step"><span className="eyebrow">02 · Test</span><h2>One change.<br />A clearer answer.</h2><p>Competing explanations become controlled edits. Real variants face a blind comparison and regression checks.</p></article>
          <article className="loop-story-step"><span className="eyebrow">03 · Learn</span><h2>The outcome<br />travels forward.</h2><p>A win, a loss, or an inconclusive result informs what the agent should try on the next video.</p><Link className="text-link" to="/research/transfer">See the recorded memory transfer <span aria-hidden="true">↗</span></Link></article>
        </div>
      </section>

      {runs.length || transfer || abcs.length ? (
        <details className="recent-disclosure"><summary>Explore recorded work</summary><section className="recent" aria-label="Recent results">
          <div className="recent-col">
            <div className="section-line">
              <h2 className="section-name">Recent</h2>
              <Link to="/runs" className="text-link">
                All runs
              </Link>
            </div>
            <ul className="recent-list">
              {runs.map((r) => (
                <li key={r.id}>
                  <Link to={`/runs/${encodeURIComponent(r.id)}`} className="recent-row">
                    <span className="recent-title">{app.videos.find((v) => v.video_id === r.video_id)?.title ?? r.video_id}</span>
                    <span className="recent-line">{runLine(r)}</span>
                    <span className="mono muted small">{fmtDateTime(r.created_at)}</span>
                    <DecisionTag decision={r.decisions.includes("accept") ? "accept" : r.status === "failed" ? "incomplete_keep_current" : r.iterations ? "reject_keep_current" : "stop"} />
                  </Link>
                </li>
              ))}
              {abcs.map((a) => (
                <li key={a.id}>
                  <Link to={`/compare/${encodeURIComponent(a.id)}`} className="recent-row">
                    <span className="recent-title">
                      A/B: {a.a_video_id} and {a.b_video_id}
                    </span>
                    <span className="recent-line">{a.status === "failed" ? a.stop_reason || "did not finish" : a.final_decision}</span>
                    <span className="mono muted small">{fmtDateTime(a.created_at)}</span>
                    <DecisionTag decision={a.status === "failed" ? "incomplete_keep_current" : a.decisions.includes("accept") ? "accept" : a.attempts ? "reject_keep_current" : "stop"} />
                  </Link>
                </li>
              ))}
            </ul>
          </div>
          {transfer ? (
            <div className="recent-col">
              <div className="section-line">
                <h2 className="section-name">What memory changed</h2>
                <Link to="/research/transfer" className="text-link">
                  Details
                </Link>
              </div>
              <p className="loss-note">
                On an unseen video, past results changed the first edit tried from {mutationName(transfer.modes.none.first_mutation)} to {mutationName(transfer.modes.learned.first_mutation)}. Recorded candidate wins:{" "}
                {Object.values(transfer.outcomes_on_b).filter((o) => o === "win").length} of {Object.keys(transfer.outcomes_on_b).length} candidate edits won.
              </p>
            </div>
          ) : null}
        </section></details>
      ) : null}
    </div>
  );
}
