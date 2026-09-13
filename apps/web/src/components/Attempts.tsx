import type { Audit, IterationRecord, Repair, RunDetail, RunLesson } from "../api/types";
import { fmtElapsed, fmtRange } from "../lib/format";
import { decisionHeadline, OUTCOME_NAMES, ROUTE_NAMES } from "../lib/labels";
import { Frame } from "./Shell";

function intervalOf(it: IterationRecord): [number, number] | null {
  const c = it.considered.find((x) => x.finding_id === it.finding_id);
  return c ? c.interval_ms : null;
}

export function DecisionTag({ decision }: { decision: string }) {
  const d = decisionHeadline(decision);
  return <span className={`decision-tag tone-${d.tone}`}>{d.label}</span>;
}

export function AttemptCard({ it, repair, onCompare }: { it: IterationRecord; repair: Repair | null; onCompare: (index: number) => void }) {
  const span = intervalOf(it);
  const tone = decisionHeadline(it.decision).tone;
  const tried = Boolean(it.finding_id);
  return (
    <article className={`attempt tone-${tone}`} aria-labelledby={`attempt-${it.index}`}>
      <header className="attempt-head">
        <Frame>{`Attempt ${it.index}`}</Frame>
        <DecisionTag decision={it.decision} />
        {it.candidate_version_id ? <span className="mono muted small">{it.candidate_version_id}</span> : null}
      </header>
      {tried ? (
        <>
          <h3 id={`attempt-${it.index}`} className="attempt-moment">
            {span ? <span className="mono attempt-when">{fmtRange(span[0], span[1])}</span> : null}
            {it.finding}
          </h3>
          <dl className="attempt-facts">
            <dt>Edit tried</dt>
            <dd>{it.edit || "No edit description recorded."}</dd>
            {it.change_verified !== null ? (
              <>
                <dt>Render check</dt>
                <dd>{it.change_verified ? "The rendered file shows the intended change." : "The rendered file does not show the intended change."}</dd>
              </>
            ) : null}
            {it.outcome ? (
              <>
                <dt>Fresh review</dt>
                <dd>
                  {OUTCOME_NAMES[it.outcome] ?? it.outcome}
                  {it.target_resolved ? ` · target moment resolved: ${it.target_resolved}` : ""}
                </dd>
              </>
            ) : null}
            <dt>Why</dt>
            <dd className="attempt-reason">{it.reason}</dd>
            {it.next_action_reason ? (
              <>
                <dt>Next</dt>
                <dd>{it.next_action_reason}</dd>
              </>
            ) : null}
          </dl>
          {it.candidate_media_url ? (
            <button type="button" className="btn" onClick={() => onCompare(it.index)}>
              Compare original and revision
            </button>
          ) : null}
        </>
      ) : (
        <>
          <h3 id={`attempt-${it.index}`} className="attempt-moment">
            No edit this system can make fits the remaining weak moments.
          </h3>
          <ul className="considered">
            {it.considered.map((c) => (
              <li key={c.finding_id}>
                <span className="mono attempt-when">{fmtRange(c.interval_ms[0], c.interval_ms[1])}</span>
                <span>{c.weakness}</span>
                {c.route ? <span className={`route-chip route-${c.route}`}>{ROUTE_NAMES[c.route] ?? c.route}</span> : null}
              </li>
            ))}
          </ul>
        </>
      )}
      <details className="disclosure">
        <summary>Technical details</summary>
        <div className="disclosure-body">
          <dl className="kv">
            {it.mutation_type ? (
              <>
                <dt>Edit type</dt>
                <dd className="mono">{it.mutation_type}</dd>
              </>
            ) : null}
            {it.ranking_reason ? (
              <>
                <dt>Ranking</dt>
                <dd>{it.ranking_reason}</dd>
              </>
            ) : null}
            {it.selection_reason ? (
              <>
                <dt>Selector</dt>
                <dd>{it.selection_reason}</dd>
              </>
            ) : null}
            <dt>Decision</dt>
            <dd className="mono">
              {it.decision} / {it.next_action}
            </dd>
            <dt>Reviewed</dt>
            <dd className="mono">
              {it.current_version_id} audit {it.audit_id}
              {it.candidate_audit_id ? `, candidate audit ${it.candidate_audit_id}` : ""}
            </dd>
            {it.repair_run_id ? (
              <>
                <dt>Repair</dt>
                <dd className="mono">{it.repair_run_id}</dd>
              </>
            ) : null}
            {Object.keys(it.timings_ms).length ? (
              <>
                <dt>Timings</dt>
                <dd className="mono">
                  {Object.entries(it.timings_ms)
                    .map(([k, v]) => `${k.replace(/_ms$/, "").replace(/_/g, " ")} ${fmtElapsed(v)}`)
                    .join(" · ")}
                </dd>
              </>
            ) : null}
          </dl>
          {it.verification_checks.length ? (
            <>
              <h5 className="oip-sub">Render checks</h5>
              <ul className="plain-list mono small">
                {it.verification_checks.map((c) => (
                  <li key={c}>{c}</li>
                ))}
              </ul>
            </>
          ) : null}
          {!tried && it.considered.length ? (
            <>
              <h5 className="oip-sub">Why each weak moment was not editable</h5>
              <ul className="plain-list small">
                {it.considered.map((c) => (
                  <li key={c.finding_id}>
                    {fmtRange(c.interval_ms[0], c.interval_ms[1])}: {c.why_not_runnable ?? c.skipped ?? c.selection_reason}
                  </li>
                ))}
              </ul>
            </>
          ) : null}
          {repair?.repair.secondary_changes.length ? (
            <>
              <h5 className="oip-sub">Side effects the selector expected</h5>
              <ul className="plain-list small">
                {repair.repair.secondary_changes.map((c) => (
                  <li key={c}>{c}</li>
                ))}
              </ul>
            </>
          ) : null}
        </div>
      </details>
    </article>
  );
}

export function Lessons({ lessons }: { lessons: RunLesson[] }) {
  if (!lessons.length) return null;
  return (
    <section className="lessons" aria-labelledby="lessons-title">
      <div className="section-line">
        <h3 id="lessons-title" className="section-name">
          What this run recorded
        </h3>
      </div>
      {lessons.map((l) => (
        <article key={l.lesson_id} className="lesson">
          <p className="lesson-label">{l.label ?? "Lesson"}</p>
          <p>
            {l.repair?.edit ? <strong>{l.repair.edit}</strong> : null} {l.finding?.weakness ? <span className="muted">for: {l.finding.weakness}</span> : null}
          </p>
          <p>
            Result: {l.result?.status ?? "unknown"}
            {l.result?.outcome ? `, ${OUTCOME_NAMES[l.result.outcome] ?? l.result.outcome}` : ""}
            {l.result?.target_resolved ? `, target resolved: ${l.result.target_resolved}` : ""}
          </p>
          {l.applies_when ? <p className="muted small">Applies when: {l.applies_when}</p> : null}
        </article>
      ))}
    </section>
  );
}

export function summarizeRun(run: RunDetail, audits: Record<string, Audit>) {
  const attempts = run.iterations.filter((i) => i.finding_id);
  const kept = attempts.filter((i) => i.decision === "accept").length;
  const firstAudit = audits[run.iterations[0]?.audit_id ?? run.audit_ids[0] ?? ""] ?? null;
  return { attempts, kept, firstAudit };
}
