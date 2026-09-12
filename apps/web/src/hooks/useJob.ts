import { useEffect, useRef, useState } from "react";
import { getClient } from "../api/client";
import { isTerminal, type JobEvent, type JobView } from "../api/types";
import { errorMessage } from "../lib/format";

export type Transport = "idle" | "stream" | "polling";

export interface JobTracker {
  job: JobView | null;
  events: JobEvent[];
  error: string | null;
  transport: Transport;
  elapsedMs: number;
}

const sleep = (ms: number) => new Promise<void>((resolve) => window.setTimeout(resolve, ms));

/**
 * Tracks one job: streams events (fetch-based SSE), falls back to 1 s polling when the stream
 * drops, reconnects with ?after=seq, and keeps a live elapsed clock while the job runs.
 */
export function useJob(jobId: string | null): JobTracker {
  const [job, setJob] = useState<JobView | null>(null);
  const [events, setEvents] = useState<JobEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [transport, setTransport] = useState<Transport>("idle");
  const [elapsedMs, setElapsedMs] = useState(0);
  const jobRef = useRef<JobView | null>(null);

  useEffect(() => {
    jobRef.current = null;
    setJob(null);
    setEvents([]);
    setError(null);
    setTransport("idle");
    setElapsedMs(0);
    if (!jobId) return;

    let cancelled = false;
    const abort = new AbortController();
    let lastSeq = 0;

    const applyJob = (view: JobView) => {
      jobRef.current = view;
      setJob(view);
    };

    const poll = async (): Promise<JobView | null> => {
      try {
        const client = await getClient();
        const view = await client.getJob(jobId);
        if (!cancelled) {
          applyJob(view);
          setError(null);
        }
        return view;
      } catch (err) {
        if (!cancelled) setError(errorMessage(err));
        return null;
      }
    };

    const onEvent = (event: JobEvent) => {
      if (cancelled) return;
      lastSeq = Math.max(lastSeq, event.seq);
      setEvents((prev) => {
        if (prev.some((p) => p.seq === event.seq)) return prev;
        return [...prev, event].sort((a, b) => a.seq - b.seq);
      });
      void poll();
    };

    const streamLoop = async () => {
      while (!cancelled) {
        try {
          const client = await getClient();
          setTransport("stream");
          await client.streamJobEvents(jobId, lastSeq, onEvent, abort.signal);
          if (cancelled) break;
          const view = await poll();
          if (view && isTerminal(view.state)) break;
          await sleep(500);
        } catch (err) {
          if (cancelled) break;
          setTransport("polling");
          setError(`event stream unavailable, polling: ${errorMessage(err)}`);
          for (let i = 0; i < 3 && !cancelled; i += 1) {
            await sleep(1000);
            const view = await poll();
            if (view && isTerminal(view.state)) return;
          }
        }
      }
    };

    // Safety-net polling every second while the job is not terminal.
    const pollTimer = window.setInterval(() => {
      const current = jobRef.current;
      if (current && isTerminal(current.state)) {
        window.clearInterval(pollTimer);
        return;
      }
      void poll();
    }, 1000);

    void poll();
    void streamLoop();

    return () => {
      cancelled = true;
      abort.abort();
      window.clearInterval(pollTimer);
    };
  }, [jobId]);

  // Live elapsed clock.
  useEffect(() => {
    if (!job) return;
    const compute = () => {
      if (!job.started_at) return job.elapsed_ms;
      const end = job.ended_at ? Date.parse(job.ended_at) : Date.now();
      return Math.max(0, end - Date.parse(job.started_at));
    };
    setElapsedMs(compute());
    if (isTerminal(job.state)) return;
    const timer = window.setInterval(() => setElapsedMs(compute()), 250);
    return () => window.clearInterval(timer);
  }, [job]);

  return { job, events, error, transport, elapsedMs };
}
