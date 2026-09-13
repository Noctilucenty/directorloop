import { getClient } from "../api/client";
import type { PolicyStore, StrategySnapshot } from "../api/types";
import { OutcomePill, StrategyStatusPill } from "../components/Badge";
import { ExperimentLink } from "../components/Experiment";
import { ResearchNav } from "../components/Shell";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { useAppState } from "../hooks/useAppState";
import { useAsync } from "../hooks/useAsync";
import { fmtDateTime } from "../lib/format";
import { mutationName } from "../lib/labels";

function Counts({ s }: { s: { wins?: number; losses?: number; neutral?: number; rejected?: number } }) {
  return (
    <span className="wlnr" aria-label={`${s.wins ?? 0} wins, ${s.losses ?? 0} losses, ${s.neutral ?? 0} neutral, ${s.rejected ?? 0} rejected`}>
      <span className={`wlnr-cell w${s.wins ? "" : " is-zero"}`}>
        <b>{s.wins ?? 0}</b>W
      </span>
      <span className={`wlnr-cell l${s.losses ? "" : " is-zero"}`}>
        <b>{s.losses ?? 0}</b>L
      </span>
      <span className={`wlnr-cell n${s.neutral ? "" : " is-zero"}`}>
        <b>{s.neutral ?? 0}</b>N
      </span>
      <span className={`wlnr-cell r${s.rejected ? "" : " is-zero"}`}>
        <b>{s.rejected ?? 0}</b>R
      </span>
    </span>
  );
}

function snapshotStatus(s: StrategySnapshot): string {
  return String(s.status ?? "PROPOSED");
}

export function PolicyPage() {
  const policy = useAsync<PolicyStore>(() => getClient().then((c) => c.getPolicy()), []);
  const { videos } = useAppState();
  const titleOf = (id: string) => videos.find((v) => v.video_id === id)?.title ?? id;
  const p = policy.data;
  return (
    <div className="page page-narrow-wide">
      <ResearchNav />
      <div className="page-head">
        <div>
          <div className="eyebrow">Creative policy</div>
          <h1 className="page-title">What the experiments taught it{p ? <span className="title-version mono"> v{p.version}</span> : null}</h1>
        </div>
      </div>
      {policy.loading && !p ? <Loading what="the policy" /> : null}
      {policy.error ? <ErrorState title="Could not load the policy" detail={policy.error} onRetry={policy.reload} /> : null}
      {p && p.strategies.length === 0 ? <EmptyState title="No strategies yet" detail="The policy fills in as experiments record wins, losses, neutral and rejected arms." /> : null}
      {p && p.strategies.length ? (
        <div className="policy-layout">
          <section aria-labelledby="strategies-title">
            <h2 id="strategies-title" className="section-title">
              Strategies
            </h2>
            <p className="muted small">Statuses follow recorded counts. Evidence here is model eval unless a row says otherwise.</p>
            <div className="strategy-list">
              {p.strategies.map((s) => (
                <article key={s.id} className="strategy">
                  <header className="strategy-head">
                    <div>
                      <h3 className="strategy-name">{mutationName(s.mutation)}</h3>
                      <div className="mono muted small">
                        {s.mutation} · {s.scope}
                      </div>
                    </div>
                    <StrategyStatusPill status={s.status} />
                  </header>
                  <div className="strategy-stats">
                    <Counts s={s} />
                    <span className="confidence" aria-label={`Confidence ${s.confidence.toFixed(2)}`}>
                      <span className="confidence-label">confidence</span>
                      <span className="confidence-track">
                        <span className="confidence-fill" style={{ width: `${Math.round(s.confidence * 100)}%` }} />
                      </span>
                      <span className="mono">{s.confidence.toFixed(2)}</span>
                    </span>
                    <span className="muted small">human test: {s.human ?? "none yet"}</span>
                  </div>
                  <table className="table compact evidence-table">
                    <caption className="sr-only">Evidence for {mutationName(s.mutation)}</caption>
                    <tbody>
                      {s.evidence.map((e, i) => (
                        <tr key={`${e.experiment_id}-${i}`}>
                          <td>
                            <OutcomePill outcome={e.outcome} />
                          </td>
                          <td>
                            <div>{titleOf(e.video_id)}</div>
                            <div className="muted small">{e.note}</div>
                          </td>
                          <td className="nowrap">
                            <ExperimentLink id={e.experiment_id}>experiment</ExperimentLink>
                            {e.weave_url ? (
                              <>
                                {" · "}
                                <a className="link" href={e.weave_url} target="_blank" rel="noreferrer">
                                  Weave
                                </a>
                              </>
                            ) : null}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </article>
              ))}
            </div>
          </section>
          <section aria-labelledby="history-title">
            <h2 id="history-title" className="section-title">
              Change history
            </h2>
            {p.changes.length === 0 ? <p className="muted">No changes recorded.</p> : null}
            <ol className="history">
              {[...p.changes].reverse().map((c) => {
                const s = p.strategies.find((x) => x.id === c.strategy_id);
                return (
                  <li key={c.version} className="history-item">
                    <span className="history-v mono">v{c.version}</span>
                    <div className="history-body">
                      <div className="history-title">{s ? mutationName(s.mutation) : c.strategy_id}</div>
                      <div className="history-change">
                        <StrategyStatusPill status={snapshotStatus(c.before)} />
                        <span className="arrow">to</span>
                        <StrategyStatusPill status={snapshotStatus(c.after)} />
                      </div>
                      <div className="history-counts">
                        <Counts s={c.before} /> <span className="arrow">to</span> <Counts s={c.after} />
                      </div>
                      <div className="muted small">
                        {fmtDateTime(c.created_at)} · <ExperimentLink id={c.experiment_id} />
                      </div>
                    </div>
                  </li>
                );
              })}
            </ol>
          </section>
        </div>
      ) : null}
    </div>
  );
}
