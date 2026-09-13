import { getClient, isNotFound } from "../api/client";
import type { Corpus } from "../api/types";
import { EvidenceBadge } from "../components/Badge";
import { ResearchNav } from "../components/Shell";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { useAsync } from "../hooks/useAsync";

const METRIC: Record<string, string> = { avg_watch_fraction: "average watch fraction", hold_3s: "3-second hold" };

export function CorpusPage() {
  const corpus = useAsync<Corpus | null>(
    () =>
      getClient()
        .then((c) => c.getCorpus())
        .catch((err: unknown) => {
          if (isNotFound(err)) return null;
          throw err;
        }),
    [],
  );
  const c = corpus.data;
  return (
    <div className="page page-narrow-wide">
      <ResearchNav />
      <div className="page-head">
        <div>
          <div className="eyebrow">Reference corpus</div>
          <h1 className="page-title">How often patterns appear in owned shorts</h1>
        </div>
      </div>
      <div className="label-banner">
        <EvidenceBadge kind="reference" />
        <span>REFERENCE CREATIVE (owned Curio shorts; descriptive counts)</span>
      </div>
      {corpus.loading && corpus.data === null && !corpus.error ? <Loading what="the corpus" /> : null}
      {corpus.error ? <ErrorState title="Could not load the corpus" detail={corpus.error} onRetry={corpus.reload} /> : null}
      {!corpus.loading && !corpus.error && c === null ? (
        <EmptyState title="The reference corpus is not built yet" detail="Pattern counts appear here after the corpus analysis has run over the owned shorts." />
      ) : null}
      {c ? (
        <>
          <p className="muted">
            {c.references} references analyzed, {c.with_metrics} with platform metrics. Counts describe the corpus; they are a weak prior, not proof that a pattern causes
            anything.
          </p>
          {c.patterns.length === 0 ? <EmptyState title="No patterns counted" /> : null}
          <div className="pattern-list">
            {c.patterns.map((p) => (
              <article key={p.pattern_id} className="pattern">
                <div className="pattern-head">
                  <h3 className="pattern-name">{p.description}</h3>
                  <span className="pattern-count">
                    <b className="mono">{p.count}</b> of <span className="mono">{p.total_comparable}</span>
                  </span>
                </div>
                <div className="bar" aria-label={`${Math.round(p.support_ratio * 100)} percent`}>
                  <span className="bar-fill" style={{ width: `${Math.round(p.support_ratio * 100)}%` }} />
                </div>
                <div className="pattern-meta">
                  <span className="mono muted small">{p.pattern_id}</span>
                  {p.top_group_total ? (
                    <span className="small">
                      higher {METRIC[p.performance_metric] ?? p.performance_metric} half {p.top_group_count}/{p.top_group_total}, lower half {p.bottom_group_count}/
                      {p.bottom_group_total}
                    </span>
                  ) : (
                    <span className="muted small">no performance split ({METRIC[p.performance_metric] ?? p.performance_metric})</span>
                  )}
                </div>
                <p className="muted small">{p.note}</p>
              </article>
            ))}
          </div>
        </>
      ) : null}
    </div>
  );
}
