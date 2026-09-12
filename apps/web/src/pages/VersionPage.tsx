import { Link, useParams } from "react-router-dom";
import { getClient } from "../api/client";
import type { VersionDetail } from "../api/types";
import { Badge, modeBadge } from "../components/Badge";
import { JsonViewer } from "../components/JsonViewer";
import { ErrorState, Loading } from "../components/States";
import { useAsync } from "../hooks/useAsync";
import { fmtDate, fmtMs, fmtSeconds, humanize, shortHash } from "../lib/format";

export function VersionPage() {
  const { id } = useParams();
  const state = useAsync<VersionDetail>(async () => {
    const client = await getClient();
    return client.getVersion(id ?? "");
  }, [id]);

  if (state.loading) return <Loading what="version" />;
  if (state.error || !state.data) return <ErrorState title="Could not load version" detail={state.error ?? undefined} onRetry={state.reload} />;
  const v = state.data;
  const run = v.evaluation_run;
  const trialsByQuestion = new Map<string, typeof run extends null ? never : NonNullable<typeof run>["results"]>();
  if (run) {
    for (const r of run.results) {
      const list = trialsByQuestion.get(r.question_id) ?? [];
      list.push(r);
      trialsByQuestion.set(r.question_id, list);
    }
  }

  return (
    <div className="page">
      <div className="page-head">
        <h1>
          V{v.index} <span className="muted">{v.status.replace(/_/g, " ")}</span>
        </h1>
        <div className="page-actions">
          {run ? <Badge kind={modeBadge(run.mode)} /> : <Badge kind="unverified" label="UNEVALUATED" />}
          {run?.weave_url ? (
            <a className="btn btn-secondary" href={run.weave_url} target="_blank" rel="noreferrer">
              Open Weave trace
            </a>
          ) : (
            <span className="muted small">no Weave trace recorded</span>
          )}
          {v.job_id ? (
            <Link className="btn btn-secondary" to={`/runs/${v.job_id}`}>
              Open run
            </Link>
          ) : null}
        </div>
      </div>
      <div className="grid-two">
        <section className="panel">
          <h2>Artifact</h2>
          <video className="player-video" src={v.media_url} controls playsInline preload="metadata" />
          <dl className="kv">
            <dt>Hash</dt>
            <dd className="mono">{v.artifact_hash}</dd>
            <dt>Duration</dt>
            <dd className="mono">{fmtSeconds(v.duration_ms)}</dd>
            <dt>Size</dt>
            <dd className="mono">
              {v.width}x{v.height}
            </dd>
            <dt>Created</dt>
            <dd className="mono">{fmtDate(v.created_at)}</dd>
            <dt>Parent</dt>
            <dd className="mono">{v.parent_version_id ? <Link to={`/versions/${v.parent_version_id}`}>{v.parent_version_id}</Link> : "none (baseline)"}</dd>
          </dl>
          <h2>Diff from parent</h2>
          <ul className="diff-list">
            {v.diff_lines.map((l, i) => (
              <li key={i}>{l}</li>
            ))}
          </ul>
          <JsonViewer value={v.plan} title="Edit plan (typed)" />
          {v.plan_diff ? <JsonViewer value={v.plan_diff} title="Structural plan diff" /> : null}
        </section>
        <section className="panel">
          <h2>Evaluation run</h2>
          {run ? (
            <>
              <div className="kv-inline">
                <span>
                  provider <strong className="mono">{run.provider}</strong>
                </span>
                <span>
                  model <strong className="mono">{run.model}</strong>
                </span>
                <span>
                  modality <strong>{run.probe_modality.replace(/_/g, " ")}</strong>
                </span>
                <span>
                  trials <strong className="mono">{run.trials}</strong>
                </span>
                <span>
                  frames sampled <strong className="mono">{run.frames_sampled ?? "n/a (native video)"}</strong>
                </span>
                <span>
                  latency <strong className="mono">{fmtMs(run.latency_ms)}</strong>
                </span>
                <span>
                  suite <strong className="mono">{shortHash(run.suite_hash)}</strong>
                </span>
                <span>
                  artifact <strong className="mono">{shortHash(run.artifact_hash)}</strong>
                </span>
              </div>
              <div className="big-inline">
                <span className="metric-value mono">
                  {run.questions_passed}/{run.questions_total}
                </span>
                <Badge kind="probe" />
                <span className="muted">questions passed (majority of valid trials)</span>
              </div>
              <table className="table small">
                <thead>
                  <tr>
                    <th>Question</th>
                    <th>Modality</th>
                    <th>Guard</th>
                    <th>Trials</th>
                    <th>Result</th>
                  </tr>
                </thead>
                <tbody>
                  {run.question_summaries.map((q) => {
                    const question = v.questions.find((x) => x.id === q.question_id);
                    const trials = trialsByQuestion.get(q.question_id) ?? [];
                    return (
                      <tr key={q.question_id} className={q.passed ? "row-pass" : "row-fail"}>
                        <td>
                          <div>{question?.text ?? humanize(q.question_id)}</div>
                          <div className="muted small mono">{q.question_id}</div>
                        </td>
                        <td>{q.modality}</td>
                        <td>{q.regression_guard ? "yes" : ""}</td>
                        <td className="mono small">
                          {trials.map((t) => (
                            <span key={t.trial} className={`trial ${t.correct === null ? "trial-err" : t.correct ? "trial-ok" : "trial-bad"}`} title={t.error ?? t.chosen_option_id ?? ""}>
                              {t.correct === null ? "err" : t.correct ? "ok" : "miss"}
                            </span>
                          ))}
                        </td>
                        <td className="mono">
                          {q.correct}/{q.valid_trials} {q.passed ? "pass" : "fail"}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              <h2>Mechanical checks</h2>
              <ul className="gate-list">
                {run.mechanical.map((m) => (
                  <li key={m.id} className={`gate-row ${m.passed ? "gate-pass" : "gate-fail"}`}>
                    <span className="gate-dot" aria-hidden="true" />
                    <span className="gate-name">{m.id.replace(/_/g, " ")}</span>
                    <span className="gate-detail muted">{m.detail}</span>
                    <Badge kind="mechanical" />
                  </li>
                ))}
              </ul>
              <h2>Protected constraints</h2>
              <ul className="gate-list">
                {run.constraints.map((c) => (
                  <li key={c.constraint_id} className={`gate-row ${c.passed ? "gate-pass" : "gate-fail"}`}>
                    <span className="gate-dot" aria-hidden="true" />
                    <span className="gate-name">{c.kind.replace(/_/g, " ")}</span>
                    <span className="gate-detail muted">{c.detail}</span>
                  </li>
                ))}
              </ul>
              <h2>Transcript of rendered audio</h2>
              {run.transcript_text ? (
                <p className="transcript">
                  {run.transcript_text} <span className="muted small">({run.transcript_source})</span>
                </p>
              ) : (
                <div className="muted">No transcript (native video was sent to the probe).</div>
              )}
              {run.notes.length ? (
                <ul className="plain muted small">
                  {run.notes.map((n) => (
                    <li key={n}>{n}</li>
                  ))}
                </ul>
              ) : null}
              <JsonViewer value={run} title="Raw evaluation run" />
            </>
          ) : (
            <div className="muted">This version has not been evaluated.</div>
          )}
        </section>
      </div>
    </div>
  );
}
