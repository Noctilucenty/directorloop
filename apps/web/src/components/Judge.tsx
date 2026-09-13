import { useState } from "react";
import { AUDIENCE_KEYS, type Audit, type AuditFinding, type AuditStrength, type EvidenceFrame, type WindowReaction } from "../api/types";
import { fmtRange, fmtS } from "../lib/format";
import { AUDIENCE_NAMES, focusName, issueName, ROUTE_NAMES } from "../lib/labels";
import { VerdictPill } from "./Badge";
import { ExternalIcon } from "./Icons";
import { Frame } from "./Shell";

export function ModelJudgment({ text }: { text?: string }) {
  return (
    <span className="judgment">
      <Frame tone="model">Model eval</Frame>
      <span className="judgment-text">{text ?? "predicted, not measured audience behavior"}</span>
    </span>
  );
}

function Level({ name, level }: { name: string; level: string }) {
  const n = level === "high" ? 3 : level === "medium" ? 2 : level === "low" ? 1 : 0;
  return (
    <span className={`level level-${level}`} aria-label={`${name} ${level}`}>
      <span className="level-name">{name}</span>
      <span className="level-bars" aria-hidden="true">
        {[1, 2, 3].map((i) => (
          <i key={i} className={i <= n ? "on" : ""} />
        ))}
      </span>
      <span className="level-value">{level}</span>
    </span>
  );
}

function FrameThumb({ frame, onSeek }: { frame: EvidenceFrame; onSeek: (ms: number) => void }) {
  const [failed, setFailed] = useState(false);
  const usable = frame.url && !failed;
  return (
    <button type="button" className="thumb" onClick={() => onSeek(frame.t_ms)} aria-label={`Frame at ${fmtS(frame.t_ms)}; seek the video`}>
      {usable ? <img src={frame.url as string} alt="" loading="lazy" onError={() => setFailed(true)} /> : <span className="thumb-empty" aria-hidden="true" />}
      <span className="thumb-time mono">{fmtS(frame.t_ms)}</span>
      {usable ? (
        <span className="thumb-preview" aria-hidden="true">
          <img src={frame.url as string} alt="" />
        </span>
      ) : null}
    </button>
  );
}

export function FrameStrip({ frames, onSeek, label }: { frames: EvidenceFrame[]; onSeek: (ms: number) => void; label: string }) {
  if (!frames.length) return null;
  return (
    <div className="frames" aria-label={label}>
      {frames.map((f) => (
        <FrameThumb key={`${f.t_ms}-${f.url}`} frame={f} onSeek={onSeek} />
      ))}
    </div>
  );
}

const SEVERITY_RANK: Record<string, number> = { high: 0, medium: 1, low: 2 };
const UNCERTAINTY_RANK: Record<string, number> = { low: 0, medium: 1, high: 2 };

/** The finding to open first: the most severe, then the least uncertain, then the earliest. */
export function strongestFinding(findings: AuditFinding[]): AuditFinding | null {
  return (
    [...findings].sort(
      (a, b) => (SEVERITY_RANK[a.severity] ?? 1) - (SEVERITY_RANK[b.severity] ?? 1) || (UNCERTAINTY_RANK[a.uncertainty] ?? 1) - (UNCERTAINTY_RANK[b.uncertainty] ?? 1) || a.start_ms - b.start_ms,
    )[0] ?? null
  );
}

export function FindingList({ findings, selectedId, strongestId, onSelect }: { findings: AuditFinding[]; selectedId: string | null; strongestId?: string | null; onSelect: (f: AuditFinding) => void }) {
  return (
    <ol className="finding-rows">
      {findings.map((f, i) => (
        <li key={f.id}>
          <button type="button" className={`finding-row sev-${f.severity}${selectedId === f.id ? " is-selected" : ""}`} onClick={() => onSelect(f)} aria-pressed={selectedId === f.id}>
            <span className="finding-n mono">{i + 1}</span>
            <span className="finding-when mono">{fmtRange(f.start_ms, f.end_ms)}</span>
            <span className="finding-issue">
              {issueName(f.issue_type)}
              {strongestId === f.id && findings.length > 1 ? <span className="finding-strongest">Most severe</span> : null}
            </span>
            <span className="finding-meta">
              <Level name="severity" level={f.severity} />
              <Level name="uncertainty" level={f.uncertainty} />
            </span>
          </button>
        </li>
      ))}
    </ol>
  );
}

const REACTION_TEXT: Record<string, string> = { engaged: "engaged", neutral: "neutral", losing_interest: "losing interest", confused: "confused", unknown: "no reading" };

export function FindingDetail({ finding: f, windows = [], onSeek, onPlay }: { finding: AuditFinding; windows?: WindowReaction[]; onSeek: (ms: number) => void; onPlay: (start: number, end: number) => void }) {
  const repair = f.repair;
  // The moment-by-moment readings that overlap this finding, at the granularity the review returned.
  const during = windows.filter((w) => !w.error && w.start_ms < f.end_ms && w.end_ms > f.start_ms).sort((a, b) => a.start_ms - b.start_ms);
  return (
    <article className="finding-detail" aria-labelledby={`weak-${f.id}`}>
      <div className="detail-head">
        <span className="mono detail-when">{fmtRange(f.start_ms, f.end_ms)}</span>
        {f.objective ? <Frame>{focusName(f.objective)}</Frame> : null}
        <Level name="severity" level={f.severity} />
        <Level name="uncertainty" level={f.uncertainty} />
        <span className={`closeup-chip${f.verified_closeup ? " is-verified" : ""}`}>{f.verified_closeup ? "Checked up close" : "Not checked up close"}</span>
        <button type="button" className="btn btn-quiet btn-small" onClick={() => onPlay(f.start_ms, f.end_ms)}>
          Play this moment
        </button>
      </div>
      <h3 id={`weak-${f.id}`} className="weakness">
        {f.weakness}
      </h3>
      <FrameStrip frames={f.evidence_frames} onSeek={onSeek} label="Frames the reviewer inspected for this moment" />
      {during.length ? (
        <div className="during">
          <h4 className="oip-title">The cold viewer at this point</h4>
          <ul className="during-list">
            {during.slice(0, 3).map((w) => (
              <li key={`${w.start_ms}-${w.end_ms}`}>
                <button type="button" className="obs-time mono" onClick={() => onSeek(w.start_ms)}>
                  {fmtRange(w.start_ms, w.end_ms)}
                </button>
                <span className={`attn-risk-tag risk-${w.attention_risk}`}>{w.attention_risk} risk</span>
                <span className="muted">{REACTION_TEXT[w.reaction] ?? w.reaction}</span>
                {w.expectation ? (
                  <p className="during-line">
                    <span className="during-key">Expected</span> {w.expectation}
                  </p>
                ) : null}
                {w.cause ? (
                  <p className="during-line">
                    <span className="during-key">Why</span> {w.cause}
                  </p>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      <div className="oip">
        <section className="oip-col">
          <h4 className="oip-title">What happens</h4>
          <ul className="obs">
            {f.observed.map((o, i) => (
              <li key={i}>
                <span className="obs-kind">{o.kind}</span>
                {o.t_ms !== null ? (
                  <button type="button" className="obs-time mono" onClick={() => onSeek(o.t_ms as number)}>
                    {fmtS(o.t_ms)}
                  </button>
                ) : null}
                <span className="obs-text">{o.text}</span>
              </li>
            ))}
          </ul>
        </section>
        <section className="oip-col">
          <h4 className="oip-title">What the viewer understands</h4>
          <p>{f.viewer_understanding || "Not stated."}</p>
          {f.alternatives.length ? (
            <>
              <h5 className="oip-sub">Other readings</h5>
              <ul className="plain-list">
                {f.alternatives.map((a) => (
                  <li key={a}>{a}</li>
                ))}
              </ul>
            </>
          ) : null}
        </section>
        <section className="oip-col">
          <h4 className="oip-title">Likely reaction</h4>
          <p>{f.predicted_reaction || "Not stated."}</p>
          <ModelJudgment text="predicted reaction" />
        </section>
      </div>
      <div className="fix">
        <div className="fix-row">
          <span className="fix-label">Suggested fix</span>
          <span>{f.proposed_repair || repair?.summary || "None suggested."}</span>
        </div>
        {repair ? (
          <div className="fix-row">
            <span className="fix-label">Feasibility</span>
            <span>
              <span className={`route-chip route-${repair.route}`}>{ROUTE_NAMES[repair.route] ?? repair.route}</span>
              {repair.runnable && repair.edit_description ? <span className="muted"> {repair.edit_description}</span> : null}
            </span>
          </div>
        ) : null}
        {repair?.required_materials.length ? (
          <div className="fix-row">
            <span className="fix-label">Would need</span>
            <ul className="plain-list">
              {repair.required_materials.map((m) => (
                <li key={m}>{m}</li>
              ))}
            </ul>
          </div>
        ) : null}
      </div>
      <details className="disclosure">
        <summary>Evidence</summary>
        <div className="disclosure-body">
          {f.keep_unchanged.length ? (
            <>
              <h5 className="oip-sub">Keep unchanged</h5>
              <ul className="plain-list">
                {f.keep_unchanged.map((k) => (
                  <li key={k}>{k}</li>
                ))}
              </ul>
            </>
          ) : null}
          <h5 className="oip-sub">Close-up check</h5>
          <p>{f.verified_closeup ? "A closer look at this moment was reviewed." : "No closer look was taken."}</p>
          {f.closeup_note ? <p className="muted">{f.closeup_note}</p> : null}
        </div>
      </details>
      {repair ? (
        <details className="disclosure">
          <summary>Technical details</summary>
          <div className="disclosure-body">
            <dl className="kv">
              <dt>Route</dt>
              <dd>
                {repair.route} {repair.runnable ? "(runnable)" : "(not runnable)"}
              </dd>
              {repair.mutation_type ? (
                <>
                  <dt>Edit type</dt>
                  <dd className="mono">{repair.mutation_type}</dd>
                </>
              ) : null}
              {repair.why_not_runnable ? (
                <>
                  <dt>Why not</dt>
                  <dd>{repair.why_not_runnable}</dd>
                </>
              ) : null}
              {repair.selection_reason ? (
                <>
                  <dt>Selector</dt>
                  <dd>{repair.selection_reason}</dd>
                </>
              ) : null}
              <dt>Issue type</dt>
              <dd className="mono">{f.issue_type}</dd>
              {repair.dependency_state ? (
                <>
                  <dt>Dependencies</dt>
                  <dd>{repair.dependency_state}</dd>
                </>
              ) : null}
            </dl>
            {repair.candidates_offered.length ? (
              <>
                <h5 className="oip-sub">Edits the selector could choose from</h5>
                <ul className="plain-list small">
                  {repair.candidates_offered.map((c) => (
                    <li key={c}>{c}</li>
                  ))}
                </ul>
              </>
            ) : null}
          </div>
        </details>
      ) : null}
    </article>
  );
}

export function AudienceGrid({ audit }: { audit: Audit }) {
  return (
    <section className="audience" aria-labelledby="audience-title">
      <div className="section-line">
        <h3 id="audience-title" className="section-name">
          What a cold viewer would do
        </h3>
        <ModelJudgment />
      </div>
      <div className="audience-grid">
        {AUDIENCE_KEYS.map((key) => {
          const v = audit.audience[key];
          return (
            <div key={key} className="audience-cell">
              <div className="audience-top">
                <span className="audience-name">{AUDIENCE_NAMES[key]}</span>
                {v ? <VerdictPill verdict={v.verdict} /> : null}
              </div>
              {v?.reason ? <p className="audience-reason">{v.reason}</p> : null}
              {v?.to_whom ? <p className="audience-whom">To: {v.to_whom}</p> : null}
            </div>
          );
        })}
      </div>
    </section>
  );
}

function KeepThumb({ frame }: { frame: EvidenceFrame | undefined }) {
  const [failed, setFailed] = useState(false);
  if (!frame?.url || failed) return <span className="keep-thumb keep-thumb-empty" aria-hidden="true" />;
  return (
    <span className="keep-thumb" aria-hidden="true">
      <img src={frame.url} alt="" loading="lazy" onError={() => setFailed(true)} />
    </span>
  );
}

export function KeepThese({ strengths, onPlay }: { strengths: AuditStrength[]; onPlay: (start: number, end: number) => void }) {
  if (!strengths.length) return null;
  return (
    <section className="keep" aria-labelledby="keep-title">
      <div className="section-line">
        <h3 id="keep-title" className="section-name">
          Keep these
        </h3>
        <span className="muted small">what already works, protected from edits</span>
      </div>
      <ul className="keep-list">
        {[...strengths]
          .sort((a, b) => a.start_ms - b.start_ms)
          .map((s) => (
            <li key={s.id} className="keep-item">
              <button type="button" className="keep-play" onClick={() => onPlay(s.start_ms, s.end_ms)} aria-label={`Play ${fmtRange(s.start_ms, s.end_ms)}: ${s.what}`}>
                <KeepThumb frame={s.evidence_frames[0]} />
                <span className="keep-time mono">{fmtRange(s.start_ms, s.end_ms)}</span>
              </button>
              <div>
                <p className="keep-what">{s.what}</p>
                <p className="keep-why">{s.why}</p>
              </div>
            </li>
          ))}
      </ul>
    </section>
  );
}

const MODE_NAMES: Record<string, string> = {
  sampled_frames_plus_asr: "Sampled frames plus a speech transcript",
  native_video: "Native video input",
  transcript_only: "Transcript only",
};

export function EvaluationProtocol({ audit, title }: { audit: Audit; title: string }) {
  const c = audit.coverage;
  const kinds = ["prefix", "diagnostic", "closeup"].map((k) => ({ k, windows: c.windows.filter((w) => w.kind === k) })).filter((x) => x.windows.length);
  const kindName: Record<string, string> = { prefix: "Watched in order, two seconds at a time", diagnostic: "Whole-video pass", closeup: "Closer looks" };
  return (
    <div className="protocol">
      <h5 className="oip-sub">{title}</h5>
      <dl className="kv">
        <dt>Input</dt>
        <dd>{MODE_NAMES[c.mode] ?? c.mode}</dd>
        <dt>Frames sent</dt>
        <dd className="mono">{c.total_frames_sent}</dd>
        <dt>Transcript</dt>
        <dd className="mono">{c.transcript_source}</dd>
        <dt>Audio</dt>
        <dd>{c.audio_inspection}</dd>
        <dt>Reviewer</dt>
        <dd className="mono">
          {c.provider} {c.model}
        </dd>
        <dt>Primed with</dt>
        <dd>{audit.primed_with.length ? audit.primed_with.join(", ") : "nothing: no objective, notes or labels"}</dd>
        <dt>Status</dt>
        <dd>
          {audit.status}
          {audit.incomplete_reasons.length ? `: ${audit.incomplete_reasons.join("; ")}` : ""}
        </dd>
      </dl>
      <ul className="plain-list">
        {kinds.map(({ k, windows }) => (
          <li key={k}>
            {kindName[k]}: {windows.length} window{windows.length === 1 ? "" : "s"}, {windows.reduce((n, w) => n + w.frame_timestamps_ms.length, 0)} frames at {windows[0].frame_width} px
          </li>
        ))}
      </ul>
      {c.limitations.length ? (
        <>
          <h5 className="oip-sub">Limitations</h5>
          <ul className="plain-list muted">
            {c.limitations.map((l) => (
              <li key={l}>{l}</li>
            ))}
          </ul>
        </>
      ) : null}
      {audit.weave_url ? (
        <a className="text-link" href={audit.weave_url} target="_blank" rel="noreferrer">
          View this review in Weave <ExternalIcon size={14} />
        </a>
      ) : null}
    </div>
  );
}
