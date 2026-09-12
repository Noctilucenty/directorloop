import { useCallback, useEffect, useRef, useState } from "react";
import { errorMessage } from "../lib/format";

export interface AsyncState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
}

/** Runs an async loader on mount and whenever deps change; ignores stale results. */
export function useAsync<T>(loader: () => Promise<T>, deps: unknown[]): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const generation = useRef(0);

  useEffect(() => {
    const gen = ++generation.current;
    setLoading(true);
    setError(null);
    loader()
      .then((result) => {
        if (gen !== generation.current) return;
        setData(result);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (gen !== generation.current) return;
        setError(errorMessage(err));
        setLoading(false);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, loading, reload };
}
