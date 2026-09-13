import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import type { ABComparison, ABCRun, AlignedUnit, CAttempt, DimensionJudgment, PairComparison } from "../api/types";
import { fmtRange, fmtS } from "../lib/format";
import { OUTCOME_NAMES } from "../lib/labels";
import { Frame } from "./Shell";
import { VideoPlayer, type PlayerHandle } from "./VideoPlayer";

const DIMENSION_NAMES: Record<string, string> = {
  opening_interest: "Opening interest",
  comprehension: "Understanding",
  information_progression: "New information at a good rate",
  pacing: "Pacing",
  visual_narration_alignment: "Pictures match the words",
  emotional_effect: "Curiosity and surprise",
  payoff: "Payoff",
  memorability: "Memorable",
  clarity: "Clarity",
  curiosity_or_anticipation: "Curiosity or anticipation",
  overall: "Overall",
};

export const dimensionName = (d: string) => DIMENSION_NAMES[d] ?? d.replace(/_/g, " ");

/** "pacing vs A: unstable" reads as "Pacing vs A: unstable". Only a leading dimension key is renamed; the rest is kept as recorded. */
export function deltaText(item: string): string {
  const m = /^([a-z][a-z_]*) vs (\S+?)(:.*)?$/.exec(item);
  return m ? `${dimensionName(m[1])} vs ${m[2]}${m[3] ?? ""}` : item;
}

/** Splits a recorded proposal description into the edit and the weak moment it was aimed at, when it has that shape. */
function splitProposal(description: string): { edit: string; aimedAt: string | null } {
  const m = /^([\s\S]*?)\s*\(aimed at ([\s\S]+)\)\s*$/.exec(description);
  if (!m || !m[1].trim()) return { edit: description, aimedAt: null };
  const target = m[2].trim();
  return { edit: m[1].trim(), aimedAt: /[.!?"')]$/.test(target) ? target : `${target}...` };
}

const KIND_NAMES: Record<AlignedUnit["kind"], string> = {
  shared: "Same line",
  reworded: "Reworded",
  only_a: "Only in A",
  only_b: "Only in B",
  time_aligned: "Matched by time",
};

export function VerdictChip({ verdict }: { verdict: string }) {
  const cls = verdict === "unstable" ? "v-unstable" : verdict === "same" ? "v-same" : verdict === "unclear" ? "v-unclear" : "v-version";
  const text = verdict === "unstable" ? "Unstable" : verdict === "same" ? "Same" : verdict === "unclear" ? "Unclear" : verdict;
  return <span className={`vchip ${cls}`}>{text}</span>;
}

function votesText(votes: Record<string, number>): string {
  return Object.entries(votes)
    .map(([k, n]) => `${k === "same" ? "same" : k} ${n}`)
    .join(" · ");
}

export function DimensionTable({ comparison, questions }: { comparison: PairComparison; questions?: Record<string, string> }) {
  const rows: DimensionJudgment[] = [...comparison.dimensions, comparison.overall];
  return (
    <table className="dims">
      <caption className="sr-only">
        {comparison.first} compared with {comparison.second}
      </caption>
      <thead>
        <tr>
          <th scope="col">Dimension</th>
          <th scope="col">Better</th>
          <th scope="col">Votes</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((d) => (
          <tr key={d.dimension} className={d.dimension === "overall" ? "is-overall" : ""}>
            <th scope="row">
              <details className="dim-details">
                <summary title={questions?.[d.dimension]}>{dimensionName(d.dimension)}</summary>
                {questions?.[d.dimension] ? <p className="muted small">{questions[d.dimension]}</p> : null}
                <ul className="plain-list small">
                  {d.reasons.map((r, i) => (
                    <li key={i}>{r}</li>
                  ))}
                </ul>
              </details>
            </th>
            <td>
              <VerdictChip verdict={d.verdict} />
            </td>
            <td className="mono muted">{votesText(d.votes)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function PairPlayers({ run }: { run: ABCRun }) {
  const a = useRef<PlayerHandle>(null);
  const b = useRef<PlayerHandle>(null);
  const [focus, setFocus] = useState<number | null>(null);
  const va = run.versions.A;
  const vb = run.versions.B;
  const align = run.comparison?.alignment ?? [];
  const seekUnit = (u: AlignedUnit) => {
    setFocus(u.index);
    if (u.a_start_ms !== null) a.current?.seek(u.a_start_ms);
    if (u.b_start_ms !== null) b.current?.seek(u.b_start_ms);
  };
  return (
    <div className="pair-view">
      <div className="pair-players">
        <VideoPlayer ref={a} src={va?.media_url ?? null} label={<Frame title={va?.video_id}>{`A · ${va?.video_id ?? "missing"}`}</Frame>} />
        <VideoPlayer ref={b} src={vb?.media_url ?? null} label={<Frame title={vb?.video_id}>{`B · ${vb?.video_id ?? "missing"}`}</Frame>} muted />
      </div>
      {va?.audit_id || vb?.audit_id ? (
        <p className="pair-reviews">
          Each was reviewed cold on its own:{" "}
          {[va, vb].map((v, i) =>
            v?.audit_id ? (
              <Link key={v.label} className="text-link" to={`/judge/${encodeURIComponent(v.video_id)}?audit=${encodeURIComponent(v.audit_id)}`}>
                {i === 0 ? "A" : "B"}: open its attention timeline
              </Link>
            ) : null,
          )}
        </p>
      ) : null}
      {align.length ? (
        <ol className="align-units" aria-label="How the two scripts line up">
          {align.map((u) => (
            <li key={u.index}>
              <button type="button" className={`unit${focus === u.index ? " is-on" : ""}`} onClick={() => seekUnit(u)}>
                <span className={`unit-kind kind-${u.kind}`}>{KIND_NAMES[u.kind]}</span>
                <span className="unit-times mono">
                  {u.a_start_ms !== null && u.a_end_ms !== null ? `A ${fmtRange(u.a_start_ms, u.a_end_ms)}` : "A none"}
                  {"  ·  "}
                  {u.b_start_ms !== null && u.b_end_ms !== null ? `B ${fmtRange(u.b_start_ms, u.b_end_ms)}` : "B none"}
                </span>
                <span className="unit-text">{u.text_a || u.text_b}</span>
                {u.kind === "reworded" && u.text_b ? <span className="unit-text muted">B: {u.text_b}</span> : null}
              </button>
            </li>
          ))}
        </ol>
      ) : null}
    </div>
  );
}

export function WhatWorks({ comparison }: { comparison: ABComparison }) {
  const groups = ["A", "B"].map((label) => ({
    label,
    strengths: Object.entries(comparison.strengths_by_unit)
      .filter(([k]) => k.startsWith(`${label}:`))
      .flatMap(([, v]) => v),
    findings: Object.entries(comparison.findings_by_unit)
      .filter(([k]) => k.startsWith(`${label}:`))
      .flatMap(([, v]) => v),
  }));
  return (
    <div className="works-grid">
      {groups.map((g) => (
        <section key={g.label} className="works" aria-label={`What works in ${g.label}`}>
          <Frame>{g.label}</Frame>
          <h4 className="oip-title">Works</h4>
          {g.strengths.length ? (
            <ul className="plain-list">
              {g.strengths.map((s) => (
                <li key={s}>{s}</li>
              ))}
            </ul>
          ) : (
            <p className="muted">Nothing specific recorded.</p>
          )}
          <h4 className="oip-title">Weak</h4>
          {g.findings.length ? (
            <ul className="plain-list">
              {g.findings.map((s) => (
                <li key={s}>{s}</li>
              ))}
            </ul>
          ) : (
            <p className="muted">No weak moments recorded.</p>
          )}
        </section>
      ))}
    </div>
  );
}

const ABC_DECISION: Record<string, { label: string; tone: string }> = {
  accept: { label: "C kept", tone: "good" },
  reject_keep_inputs: { label: "C not kept", tone: "bad" },
  incomplete: { label: "Could not be judged", tone: "warn" },
  stop: { label: "Stopped", tone: "neutral" },
};

export function CAttemptCard({ attempt: a, run }: { attempt: CAttempt; run: ABCRun }) {
  const d = ABC_DECISION[a.decision] ?? { label: a.decision, tone: "neutral" };
  const ev = a.evaluation;
  const p = a.proposal;
  const proposal = p ? splitProposal(p.description) : null;
  return (
    <article className={`attempt tone-${d.tone}`}>
      <header className="attempt-head">
        <Frame>{`Attempt ${a.index}`}</Frame>
        <span className={`decision-tag tone-${d.tone}`}>{d.label}</span>
        {a.version_id ? <span className="mono muted small">{a.version_id}</span> : null}
      </header>
      {p ? (
        <div className="c-attempt">
          {a.render_media_url ? <VideoPlayer src={a.render_media_url} label={<Frame tone="accent">{`C${a.index}`}</Frame>} className="player-c" /> : null}
          <div className="c-body">
            <h3 className="attempt-moment">{proposal?.edit ?? p.description}</h3>
            <dl className="attempt-facts">
              <dt>Built from</dt>
              <dd>
                {p.base}
                {p.donor ? `, with a part of ${p.donor}` : ""}
              </dd>
              {proposal?.aimedAt ? (
                <>
                  <dt>Aimed at</dt>
                  <dd>{proposal.aimedAt}</dd>
                </>
              ) : null}
              <dt>Should improve</dt>
              <dd>{p.target_dimensions.map(dimensionName).join(", ") || "not stated"}</dd>
              {p.expected_improvement ? (
                <>
                  <dt>Expected</dt>
                  <dd>{p.expected_improvement}</dd>
                </>
              ) : null}
              {ev ? (
                <>
                  <dt>Fresh review</dt>
                  <dd>
                    {OUTCOME_NAMES[ev.outcome] ?? ev.outcome}. {ev.outcome_reason}
                  </dd>
                </>
              ) : null}
              {!ev || a.reason.trim() !== ev.outcome_reason.trim() ? (
                <>
                  <dt>Why</dt>
                  <dd className="attempt-reason">{a.reason}</dd>
                </>
              ) : null}
            </dl>
            {p.protected_strengths.length ? (
              <>
                <h5 className="oip-sub">Strengths it had to keep</h5>
                <ul className="protected-list">
                  {p.protected_strengths.map((s, i) => {
                    const status = ev?.protected.find((x) => x.item === s.item);
                    return (
                      <li key={i}>
                        <span className="mono muted">{s.source}</span>
                        <span>{s.item}</span>
                        {status?.status ? <span className={`keep-status status-${status.status}`}>{status.status}</span> : null}
                      </li>
                    );
                  })}
                </ul>
              </>
            ) : null}
            {p.tradeoffs.length ? (
              <>
                <h5 className="oip-sub">Tradeoffs it accepted</h5>
                <ul className="plain-list">
                  {p.tradeoffs.map((t) => (
                    <li key={t}>{t}</li>
                  ))}
                </ul>
              </>
            ) : null}
            {ev ? (
              <div className="cmp-grid">
                {(
                  [
                    ["Better", ev.improved, "good", "Nothing got better."],
                    ["Worse", ev.regressed, "bad", "Nothing got worse."],
                    ["Uncertain", ev.uncertain, "warn", "Nothing uncertain."],
                    ["New weak moments", ev.new_weaknesses, "warn", "None reported."],
                  ] as const
                ).map(([title, items, tone, empty]) => (
                  <div key={title} className={`cmp-block tone-${tone}`}>
                    <div className="cmp-title">
                      {title}
                      <span className="mono cmp-count">{items.length}</span>
                    </div>
                    {items.length ? (
                      <ul className="plain-list">
                        {items.map((t) => (
                          <li key={t}>{deltaText(t)}</li>
                        ))}
                      </ul>
                    ) : (
                      <p className="muted">{empty}</p>
                    )}
                  </div>
                ))}
              </div>
            ) : null}
            <details className="disclosure">
              <summary>Evidence</summary>
              <div className="disclosure-body">
                {ev?.change_verification ? (
                  <>
                    <h5 className="oip-sub">Render check: {ev.change_verification.verified ? "the intended change is present" : "the intended change is missing"}</h5>
                    <ul className="plain-list mono small">
                      {ev.change_verification.checks.map((c) => (
                        <li key={c}>{c}</li>
                      ))}
                    </ul>
                  </>
                ) : null}
                {ev?.vs_base ? (
                  <>
                    <h5 className="oip-sub">
                      C against {ev.vs_base.second === "C" ? ev.vs_base.first : ev.vs_base.second}
                    </h5>
                    <DimensionTable comparison={ev.vs_base} questions={run.rubric.whole_dimensions} />
                  </>
                ) : null}
                {ev?.vs_other ? (
                  <>
                    <h5 className="oip-sub">
                      C against {ev.vs_other.second === "C" ? ev.vs_other.first : ev.vs_other.second}
                    </h5>
                    <DimensionTable comparison={ev.vs_other} questions={run.rubric.whole_dimensions} />
                  </>
                ) : null}
                {ev?.target_region ? (
                  <>
                    <h5 className="oip-sub">The changed stretch</h5>
                    <DimensionTable comparison={ev.target_region} questions={run.rubric.region_dimensions} />
                  </>
                ) : null}
                {ev?.protected.length ? (
                  <>
                    <h5 className="oip-sub">Protected strengths, checked</h5>
                    <ul className="plain-list small">
                      {ev.protected.map((x, i) => (
                        <li key={i}>
                          {x.status ?? "unknown"}: {x.item}
                          {x.evidence ? <span className="muted"> {x.evidence}</span> : null}
                        </li>
                      ))}
                    </ul>
                  </>
                ) : null}
              </div>
            </details>
            <details className="disclosure">
              <summary>Technical details</summary>
              <div className="disclosure-body">
                <dl className="kv">
                  <dt>Option</dt>
                  <dd className="mono">{p.option_key}</dd>
                  <dt>Selector</dt>
                  <dd>{p.selector_reason}</dd>
                  <dt>Why smallest</dt>
                  <dd>{p.why_smallest}</dd>
                  <dt>Next</dt>
                  <dd>
                    {a.next_action}: {a.next_action_reason}
                  </dd>
                </dl>
                {p.options_offered.length ? (
                  <>
                    <h5 className="oip-sub">Options offered</h5>
                    <p className="mono small">{p.options_offered.join(", ")}</p>
                  </>
                ) : null}
              </div>
            </details>
          </div>
        </div>
      ) : (
        <p className="attempt-moment">{a.reason}</p>
      )}
    </article>
  );
}

export function finalWords(run: ABCRun): { title: string; tone: "good" | "bad" | "neutral" } {
  if (run.status === "running") return { title: "Still running", tone: "neutral" };
  if (run.status === "failed") return { title: "The comparison did not finish", tone: "bad" };
  const accepted = run.attempts.some((a) => a.decision === "accept");
  if (accepted && run.final_version.startsWith("C")) return { title: `Keep ${run.final_version}`, tone: "good" };
  if (run.final_version === "A" || run.final_version === "B") return { title: `Keep ${run.final_version}. No C was better.`, tone: "bad" };
  return { title: "Keep both inputs. No C was better.", tone: "bad" };
}

export function usageLine(run: ABCRun): string {
  const u = run.usage;
  const parts: string[] = [];
  if (typeof u.model_calls === "number") parts.push(`${u.model_calls}${u.max_model_calls ? ` of ${u.max_model_calls}` : ""} model calls`);
  if (typeof u.input_tokens === "number") parts.push(`${u.input_tokens.toLocaleString()} input and ${(u.output_tokens ?? 0).toLocaleString()} output tokens`);
  if (typeof u.elapsed_s === "number") parts.push(fmtS(u.elapsed_s * 1000));
  if (u.cost_note) parts.push(u.cost_note);
  return parts.join(" · ");
}
