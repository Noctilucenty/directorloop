import { getClient } from "../api/client";
import type { BenchmarkRun } from "../api/types";
import { BenchmarkTable } from "../components/BenchmarkTable";
import { ErrorState, Loading } from "../components/States";
import { useAsync } from "../hooks/useAsync";

export function BenchmarksPage() {
  const state = useAsync<BenchmarkRun[]>(async () => (await getClient()).getBenchmarks(), []);
  if (state.loading) return <Loading what="benchmarks" />;
  if (state.error || !state.data) return <ErrorState title="Could not load benchmarks" detail={state.error ?? undefined} onRetry={state.reload} />;
  return (
    <div className="page">
      <div className="page-head">
        <h1>GPU generation benchmarks</h1>
      </div>
      <p className="muted">
        Every row is a stored run with its sample size. Output length is not generation time. Live generation is enabled only when
        end-to-end time to browser playback fits the loop budget and the quality checks pass.
      </p>
      <BenchmarkTable runs={state.data} />
    </div>
  );
}
