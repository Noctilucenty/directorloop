import { useEffect, useRef, useState } from "react";
import { getClient, isNotFound } from "../api/client";
import { isTerminal, type Job, type JobEvent } from "../api/types";
import { errorMessage } from "../lib/format";

export type Transport = "idle" | "stream" | "polling";

export interface JobTracker {
  job: Job | null;
  events: JobEvent[];
  error: string | null;
  transport: Transport;
  elapsedMs: number;
  /** The API has no job with this id (for example a run launched from the command line). */
  missing: boolean;
}

const sleep = (ms: number) => new Promise<void>((resolve) => window.setTimeout(resolve, ms));

/**
 * Follows one job. Events arrive over the SSE stream and reconnect with ?after=<last seq>. If the stream fails,
 * events and job state are polled every second from /events.json and /jobs/{id}, and the stream is retried.
 * The elapsed clock is the server's elapsed_ms plus local time since that reading, so clock skew does not leak in.
 */
export function useJob(jobId: string | null): JobTracker {
  const [job, setJob] = useState<Job | null>(null);
  const [events, setEvents] = useState<JobEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [transport, setTransport] = useState<Transport>("idle");
  const [elapsedMs, setElapsedMs] = useState(0);
  const [missing, setMissing] = useState(false);
  const received = useRef<{ job: Job; at: number } | null>(null);

  useEffect(() => {
    received.current = null;
    setJob(null);
    setEvents([]);
    setError(null);
    setTransport("idle");
    setElapsedMs(0);
    setMissing(false);
    if (!jobId) return;

    let cancelled = false;
    const abort = new AbortController();
    let lastSeq = 0;
    let safetyNet = 0;

    const stop = () => {
      cancelled = true;
      abort.abort();
      window.clearInterval(safetyNet);
    };

    const addEvents = (incoming: JobEvent[]) => {
      if (cancelled || incoming.length === 0) return;
      for (const e of incoming) lastSeq = Math.max(lastSeq, e.seq);
      setEvents((prev) => {
        const seen = new Set(prev.map((p) => p.seq));
        const fresh = incoming.filter((e) => !seen.has(e.seq));
        return fresh.length ? [...prev, ...fresh].sort((a, b) => a.seq - b.seq) : prev;
      });
    };

    const pollJob = async (): Promise<Job | null> => {
      try {
        const client = await getClient();
        const view = await client.getJob(jobId);
        if (!cancelled) {
          received.current = { job: view, at: Date.now() };
          setJob(view);
          setError(null);
        }
        return view;
      } catch (err) {
        if (cancelled) return null;
        if (isNotFound(err)) {
          setMissing(true);
          setTransport("idle");
          stop();
          return null;
        }
        setError(errorMessage(err));
        return null;
      }
    };

    const pollEvents = async () => {
      try {
        const client = await getClient();
        addEvents(await client.getJobEvents(jobId, lastSeq));
      } catch (err) {
        if (!cancelled && !isNotFound(err)) setError(errorMessage(err));
      }
    };

    const streamLoop = async () => {
      let first = await pollJob();
      while (!cancelled && !first) {
        await sleep(1000);
        first = await pollJob();
      }
      if (cancelled || !first) return;
      if (isTerminal(first.state)) {
        await pollEvents();
        return;
      }
      while (!cancelled) {
        try {
          const client = await getClient();
          setTransport("stream");
          await client.streamJobEvents(
            jobId,
            lastSeq,
            (e) => {
              addEvents([e]);
              void pollJob();
            },
            abort.signal,
          );
          if (cancelled) return;
          const view = await pollJob();
          if (view && isTerminal(view.state)) {
            await pollEvents();
            return;
          }
          await sleep(500);
        } catch (err) {
          if (cancelled) return;
          setTransport("polling");
          setError(`event stream unavailable, polling every second (${errorMessage(err)})`);
          for (let i = 0; i < 5 && !cancelled; i += 1) {
            await sleep(1000);
            await pollEvents();
            const view = await pollJob();
            if (view && isTerminal(view.state)) {
              await pollEvents();
              return;
            }
          }
        }
      }
    };

    safetyNet = window.setInterval(() => {
      const current = received.current?.job;
      if (current && isTerminal(current.state)) {
        window.clearInterval(safetyNet);
        void pollEvents();
        return;
      }
      void pollJob();
    }, 1000);

    void streamLoop();

    return () => stop();
  }, [jobId]);

  useEffect(() => {
    if (!job) return;
    const compute = () => {
      const r = received.current;
      if (!r) return job.elapsed_ms;
      if (r.job.state !== "RUNNING") return r.job.elapsed_ms;
      return r.job.elapsed_ms + (Date.now() - r.at);
    };
    setElapsedMs(compute());
    if (isTerminal(job.state)) return;
    const timer = window.setInterval(() => setElapsedMs(compute()), 100);
    return () => window.clearInterval(timer);
  }, [job]);

  return { job, events, error, transport, elapsedMs, missing };
}
