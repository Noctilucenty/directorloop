import { getClient } from "../api/client";
import { isTerminal, type Job, type JobEvent } from "../api/types";

/** Waits for a job to finish by polling its state and events once a second; reports each new event. */
export async function waitForJob(jobId: string, onEvent: (e: JobEvent) => void, signal?: AbortSignal): Promise<{ job: Job; events: JobEvent[] }> {
  const client = await getClient();
  const events: JobEvent[] = [];
  let after = 0;
  for (;;) {
    if (signal?.aborted) throw new Error("canceled");
    const fresh = await client.getJobEvents(jobId, after);
    for (const e of fresh) {
      after = Math.max(after, e.seq);
      events.push(e);
      onEvent(e);
    }
    const job = await client.getJob(jobId);
    if (isTerminal(job.state)) {
      const tail = await client.getJobEvents(jobId, after);
      tail.forEach((e) => {
        events.push(e);
        onEvent(e);
      });
      return { job, events };
    }
    await new Promise((r) => window.setTimeout(r, 1000));
  }
}

export function eventValue(events: JobEvent[], key: string): string | null {
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const v = events[i].data?.[key];
    if (typeof v === "string") return v;
  }
  return null;
}
