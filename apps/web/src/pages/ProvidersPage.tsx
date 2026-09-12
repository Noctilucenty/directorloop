import { getClient } from "../api/client";
import type { HealthReady, ProviderEntry } from "../api/types";
import { ProviderStatus } from "../components/ProviderStatus";
import { ErrorState, Loading } from "../components/States";
import { useAsync } from "../hooks/useAsync";

export function ProvidersPage() {
  const state = useAsync<{ providers: ProviderEntry[]; health: HealthReady | null }>(async () => {
    const client = await getClient();
    const providers = await client.getProviders();
    let health: HealthReady | null = null;
    try {
      health = await client.healthReady();
    } catch {
      health = null;
    }
    return { providers, health };
  }, []);
  if (state.loading) return <Loading what="providers" />;
  if (state.error || !state.data) return <ErrorState title="Could not load providers" detail={state.error ?? undefined} onRetry={state.reload} />;
  return (
    <div className="page">
      <div className="page-head">
        <h1>Providers and capabilities</h1>
      </div>
      <p className="muted">
        A key being present does not mean an integration works. State changes to verified only after a real smoke test, and the result is
        shown here.
      </p>
      <ProviderStatus providers={state.data.providers} health={state.data.health} />
    </div>
  );
}
