import type { Job, JobEvent } from "../api/types";
import type { Transport } from "../hooks/useJob";
import { StageRail } from "./StageRail";

export function StageList({ job, events, elapsedMs, transport, error, expected }: { job: Job | null; events: JobEvent[]; elapsedMs: number; transport: Transport; error: string | null; expected: string[]; title?: string }) {
  return job ? <StageRail job={job} events={events} elapsedMs={elapsedMs} transport={transport} error={error} expected={expected} /> : null;
}
