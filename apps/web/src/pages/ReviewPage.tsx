import { useEffect, useState } from "react";
import { getClient } from "../api/client";
import type { ReviewQr, ReviewSummary } from "../api/types";
import { EvidenceBadge } from "../components/Badge";
import { ExperimentLink } from "../components/Experiment";
import { ResearchNav } from "../components/Shell";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { errorMessage } from "../lib/format";
import { armName } from "../lib/labels";

export function ReviewPage() {
  const [summary, setSummary] = useState<ReviewSummary | null>(null);
  const [qr, setQr] = useState<ReviewQr | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const c = await getClient();
        const [s, q] = await Promise.all([c.getReviewSummary(), c.getReviewQr()]);
        if (cancelled) return;
        setSummary(s);
        setQr(q);
        setError(null);
      } catch (err) {
        if (!cancelled) setError(errorMessage(err));
      }
    };
    void load();
    const timer = window.setInterval(load, 5000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [tick]);

  return (
    <div className="page page-narrow-wide">
      <ResearchNav />
      <div className="page-head">
        <div>
          <div className="eyebrow">Human test</div>
          <h1 className="page-title">Which version would people keep watching?</h1>
        </div>
      </div>
      <div className="label-banner">
        <EvidenceBadge kind="human_test" />
        <span>BLINDED CONTINUE-WATCHING PREFERENCE (human test; small convenience sample, not retention)</span>
      </div>
      {error ? <ErrorState title="Could not load the human test" detail={error} onRetry={() => setTick((t) => t + 1)} /> : null}
      {!summary && !error ? <Loading what="the human test" /> : null}
      {summary ? (
        <div className="review-layout">
          <section className="panel review-link">
            <div className="qr-box">{qr?.qr_png ? <img src={qr.qr_png} alt="QR code for the blinded review link" /> : <div className="qr-empty">No review link</div>}</div>
            {qr?.url ? <div className="review-url mono">{qr.url}</div> : null}
            {qr?.note ? <p className="muted small">{qr.note}</p> : null}
            <div className="live-n">
              <span className="live-n-value">{summary.total_responses}</span>
              <span className="live-n-label">responses; refreshes every 5 s</span>
            </div>
          </section>
          <section>
            {summary.pairs.length === 0 ? (
              <EmptyState title="No responses yet" detail="Each response compares the original with one variant, shown side by side in random order with no labels." />
            ) : (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th scope="col">Experiment</th>
                      <th scope="col">Clip</th>
                      <th scope="col">Pair</th>
                      <th scope="col">n</th>
                      <th scope="col">Preferred</th>
                      <th scope="col">No preference</th>
                    </tr>
                  </thead>
                  <tbody>
                    {summary.pairs.map((p) => (
                      <tr key={`${p.experiment_id}-${p.test_type}-${p.arms.join()}`}>
                        <td>
                          <ExperimentLink id={p.experiment_id}>{p.experiment_id.slice(-8)}</ExperimentLink>
                        </td>
                        <td>{p.test_type === "hook" ? "first 3 s" : "full video"}</td>
                        <td>{p.arms.map((a) => armName(p.arm_labels[a] ?? a)).join(" vs ")}</td>
                        <td className="mono">n = {p.n}</td>
                        <td>
                          {p.arms.map((a) => (
                            <div key={a}>
                              {armName(p.arm_labels[a] ?? a)} <span className="mono">{p.preferred[a] ?? 0}</span>
                            </div>
                          ))}
                        </td>
                        <td className="mono">{p.no_preference}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </div>
      ) : null}
    </div>
  );
}
