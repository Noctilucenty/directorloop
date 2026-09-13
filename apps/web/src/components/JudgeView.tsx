import { useMemo, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { getClient } from "../api/client";
import type { Audit, AuditFinding } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { fmtRange, fmtS } from "../lib/format";
import { issueName } from "../lib/labels";
import { AttentionChart } from "./Attention";
import { EvidenceBadge } from "./Badge";
import { HypothesisCard } from "./Experiment";
import { AudienceGrid, FindingDetail, KeepThese, ModelJudgment } from "./Judge";
import { VideoPlayer, type PlayerHandle } from "./VideoPlayer";

export function JudgeView({ audit, mediaUrl, cta }: { audit: Audit; mediaUrl: string | null; cta?: ReactNode }) {
  const player = useRef<PlayerHandle>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [cursorMs, setCursorMs] = useState(0);
  const [view, setView] = useState<"watch" | "hypotheses">("watch");
  const detail = useAsync(() => getClient().then(c => c.getVideo(audit.video_id)), [audit.video_id]);
  const findings = useMemo(() => [...audit.findings].sort((a, b) => a.start_ms - b.start_ms), [audit]);
  const window = audit.window_reactions.find(w => cursorMs >= w.start_ms && cursorMs < w.end_ms);
  const selected = findings.find(f => f.id === selectedId) ?? findings.find(f => cursorMs >= f.start_ms && cursorMs < f.end_ms) ?? null;
  // The genome describes the source file; never apply it to an edited version's timestamps.
  const genome = detail.data?.genome?.artifact_hash === audit.artifact_hash ? detail.data.genome : null;
  const hypotheses = genome ? detail.data?.investigation?.hypotheses ?? [] : [];
  const beat = genome?.beats.find(b => cursorMs >= b.start_ms && cursorMs < b.end_ms);
  const seek = (ms: number) => { setSelectedId(null); setCursorMs(ms); player.current?.seek(ms); };
  const playRange = (start: number, end: number) => { setSelectedId(null); player.current?.playInterval(start, end); };
  const selectFinding = (f: AuditFinding) => { setSelectedId(f.id); player.current?.seek(f.start_ms); };

  return <div className="review-studio">
    <nav className="chapter-nav" aria-label="Analysis chapters">
      <button className={view === "watch" ? "is-active" : ""} onClick={() => setView("watch")} aria-pressed={view === "watch"}><span>01</span> Watch & notice</button>
      <button className={view === "hypotheses" ? "is-active" : ""} onClick={() => setView("hypotheses")} aria-pressed={view === "hypotheses"}><span>02</span> Competing explanations</button>
      {genome ? <Link to={`/research/experiments/new/${encodeURIComponent(audit.video_id)}`}><span>03</span> Design an experiment <span aria-hidden="true">↗</span></Link> : null}
    </nav>
    <div className="review-stage">
      <div className="review-media">
        <div className="media-topline"><span className="eyebrow">The video</span><span className="mono">{fmtS(cursorMs)} / {fmtS(audit.duration_ms)}</span></div>
        <VideoPlayer ref={player} src={mediaUrl} onTime={setCursorMs} className="player-hero" />
        <p className="media-caption">{audit.version_id === "v0" ? "Original" : audit.version_id} <span>·</span> Cold-viewer review</p>
      </div>
      <div className="review-reading">
        {view === "watch" ? <>
          <div className="section-line"><h2 className="eyebrow">What the agent sees</h2><EvidenceBadge kind="model_eval" /></div>
          <div className="beat-reading">
            <span className="reading-time mono">{window ? fmtRange(window.start_ms, window.end_ms) : fmtS(cursorMs)}</span>
            <h2>{beat ? beat.role : window?.reaction?.replace(/_/g, " ") ?? "Ready to watch"}</h2>
            <p>{window?.error ? "This window could not be reviewed." : window?.understanding || "Play the video or select a region to inspect the chronological review."}</p>
            {beat ? <details className="transcript-reading"><summary>Transcript · {beat.role}</summary><p>“{beat.text}”</p></details> : null}
          </div>
          <AttentionChart windows={audit.window_reactions} durationMs={audit.duration_ms || audit.coverage.duration_ms} findings={findings} strengths={audit.strengths} cursorMs={cursorMs} selectedFindingId={selected?.id ?? null} onSelectFinding={selectFinding} onSeek={seek} onPlay={playRange} />
          {selected ? <div className="symptom-strip"><span className="eyebrow">Observed symptom</span><h3>{issueName(selected.issue_type)}</h3><p>{selected.weakness}</p><button className="text-button" onClick={() => setView("hypotheses")}>Why might this happen? <span aria-hidden="true">→</span></button></div> : null}
        </> : <>
          <div className="section-line"><span className="eyebrow">Why might attention weaken?</span><ModelJudgment text="Explanations to test" /></div>
          <h2 className="editorial-title">Competing<br /><span>explanations.</span></h2>
          {selected ? <p className="hypothesis-context"><span className="eyebrow">Cold-review symptom · {fmtRange(selected.start_ms,selected.end_ms)}</span>{selected.weakness}</p> : null}
          {hypotheses.length ? <p className="muted small">Structural hypotheses for this source video. These are possible explanations to test, separate from the cold-viewer observations.</p> : null}
          {hypotheses.length ? <div className="hyp-list">{hypotheses.map((h,i) => <HypothesisCard key={h.id} h={h} rank={i+1} onSeek={seek} />)}</div> : <>
            <p className="muted">This review records possible explanations. Separate evidence for and against each hypothesis has not been recorded.</p>
            {findings.map((f,i) => <article className="hyp" key={f.id}>
              <div className="hyp-head"><span className="hyp-rank mono">{String(i+1).padStart(2,"0")}</span><button className="text-button mono" onClick={() => selectFinding(f)}>{fmtRange(f.start_ms,f.end_ms)}</button></div>
              <h3 className="hyp-family">{issueName(f.issue_type)}</h3><p>{f.weakness}</p>
              <details className="disclosure"><summary>Observations and alternative explanations</summary><div className="disclosure-body"><FindingDetail finding={f} windows={audit.window_reactions} onSeek={seek} onPlay={playRange} /></div></details>
            </article>)}
          </>}
        </>}
      </div>
    </div>
    {audit.status === "incomplete" ? <p className="callout callout-warn">Incomplete review: {audit.incomplete_reasons.join("; ") || "The review did not finish."}</p> : null}
    {cta}
    <details className="disclosure review-support"><summary>Whole-video diagnosis, observations, and strengths to preserve</summary><div className="disclosure-body">
      <p className="take-text">{audit.overall_summary || "No whole-video diagnosis recorded."}</p>
      {findings.map(f => <FindingDetail key={f.id} finding={f} windows={audit.window_reactions} onSeek={seek} onPlay={playRange} />)}
      <KeepThese strengths={audit.strengths} onPlay={playRange} /><AudienceGrid audit={audit} />
    </div></details>
  </div>;
}
