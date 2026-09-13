// In-browser API for development without the backend. Every response comes from src/api/fixtures, which
// scripts/build-fixtures.mjs converts from DirectorLoop's stored records. The mock cannot upload, judge or improve:
// those calls fail with an explanation, and health reports every feature off. Research experiments replay their
// recorded stage messages at the recorded timings.
import { ApiError, type ApiClient } from "./client";
import {
  isTerminal,
  type ABCRun,
  type ABCSummary,
  type Audit,
  type AuditSummary,
  type Corpus,
  type DesignPreview,
  type ExperimentDetail,
  type ExperimentRequest,
  type ExperimentSummary,
  type Health,
  type Job,
  type JobEvent,
  type PolicyStore,
  type Repair,
  type ReviewQr,
  type ReviewSummary,
  type RunDetail,
  type RunSummary,
  type TransferReport,
  type VideoDetail,
  type VideoSummary,
} from "./types";

const files = import.meta.glob("./fixtures/*.json", { eager: true, import: "default" }) as Record<string, unknown>;

function clone<T>(value: T): T {
  return value === undefined ? value : (JSON.parse(JSON.stringify(value)) as T);
}

function fixture<T>(name: string): T | null {
  const value = files[`./fixtures/${name}`];
  return value === undefined || value === null ? null : clone(value as T);
}

function need<T>(name: string, what: string): T {
  const value = fixture<T>(name);
  if (value === null) throw new ApiError(404, what);
  return value;
}

const NOT_IN_MOCK = "The mock API only shows stored runs. Start the API (directorloop serve) and use live mode to upload, judge or improve.";

interface ReplayScript {
  experiment_id?: string;
  events: { at_ms: number; stage: string; message: string; data: Record<string, unknown> | null }[];
}

interface MockJob {
  job: Job;
  events: JobEvent[];
  listeners: Set<(event: JobEvent) => void>;
  timers: number[];
  bodyKey: string;
}

const nowIso = () => new Date().toISOString();

export function createMockClient(): ApiClient {
  const jobs = new Map<string, MockJob>();
  const idempotency = new Map<string, string>();
  let counter = 0;

  const respond = <T,>(fn: () => T, ms = 80): Promise<T> =>
    new Promise<T>((resolve, reject) => {
      window.setTimeout(() => {
        try {
          resolve(fn());
        } catch (err) {
          reject(err);
        }
      }, ms);
    });

  const emit = (mj: MockJob, stage: string, message: string, data: Record<string, unknown> | null) => {
    const event: JobEvent = { seq: mj.events.length + 1, ts: nowIso(), stage, message, data };
    mj.events.push(event);
    mj.job.stage = stage;
    mj.listeners.forEach((listener) => listener(event));
  };

  const finish = (mj: MockJob, state: Job["state"], error: string | null) => {
    mj.timers.forEach((t) => window.clearTimeout(t));
    mj.timers = [];
    mj.job.state = state;
    mj.job.ended_at = nowIso();
    mj.job.error = error;
  };

  const elapsed = (mj: MockJob) => (mj.job.started_at ? Math.max(0, (mj.job.ended_at ? Date.parse(mj.job.ended_at) : Date.now()) - Date.parse(mj.job.started_at)) : 0);

  const client: ApiClient = {
    mode: "mock",
    getHealth: () => respond(() => need<Health>("health.json", "health unavailable")),
    listVideos: () => respond(() => need<VideoSummary[]>("videos.json", "videos not found")),
    uploadVideo: () => Promise.reject(new ApiError(503, NOT_IN_MOCK)),
    startRun: () => Promise.reject(new ApiError(503, NOT_IN_MOCK)),
    listRuns: () => respond(() => fixture<RunSummary[]>("runs.json") ?? []),
    getRun: (id) => respond(() => need<RunDetail>(`run.${id}.json`, `run ${id} not found`)),
    getAudit: (id) => respond(() => need<Audit>(`audit.${id}.json`, `audit ${id} not found`)),
    listAudits: (videoId) => respond(() => (fixture<AuditSummary[]>("audits.json") ?? []).filter((a) => a.video_id === videoId)),
    startAudit: () => Promise.reject(new ApiError(503, NOT_IN_MOCK)),
    startScreening: () => Promise.reject(new ApiError(503, NOT_IN_MOCK)),
    getScreening: () => Promise.reject(new ApiError(404, "No screening in this recorded preview.")),
    listScreenings: () => Promise.resolve([]),
    ingestUrl: () => Promise.reject(new ApiError(503, NOT_IN_MOCK)),
    startAbc: () => Promise.reject(new ApiError(503, NOT_IN_MOCK)),
    listAbc: () => respond(() => fixture<ABCSummary[]>("abc.json") ?? []),
    getAbc: (id) => respond(() => need<ABCRun>(`abc.${id}.json`, `comparison ${id} not found`)),
    startCausal: () => Promise.reject(new ApiError(503, "Causal experiments require the live API; recorded preview cannot launch a run.")),
    listCausal: () => Promise.resolve([]),
    getCausal: () => Promise.reject(new ApiError(404, "No causal record in this recorded preview.")),
    getRepair: (id) => respond(() => need<Repair>(`repair.${id}.json`, `repair ${id} not found`)),

    getJob: (id) =>
      respond(() => {
        const mj = jobs.get(id);
        if (!mj) throw new ApiError(404, "job not found");
        mj.job.elapsed_ms = elapsed(mj);
        return clone(mj.job);
      }, 30),

    streamJobEvents: (id, after, onEvent, signal) =>
      new Promise<void>((resolve, reject) => {
        const mj = jobs.get(id);
        if (!mj) {
          reject(new ApiError(404, "job not found"));
          return;
        }
        mj.events.filter((e) => e.seq > after).forEach(onEvent);
        if (isTerminal(mj.job.state)) {
          resolve();
          return;
        }
        const listener = (event: JobEvent) => {
          onEvent(event);
          if (isTerminal(mj.job.state)) {
            mj.listeners.delete(listener);
            resolve();
          }
        };
        mj.listeners.add(listener);
        signal.addEventListener("abort", () => {
          mj.listeners.delete(listener);
          resolve();
        });
      }),

    getJobEvents: (id, after) =>
      respond(() => {
        const mj = jobs.get(id);
        if (!mj) throw new ApiError(404, "job not found");
        return clone(mj.events.filter((e) => e.seq > after));
      }, 30),

    cancelJob: (id) =>
      respond(() => {
        const mj = jobs.get(id);
        if (!mj) throw new ApiError(404, "job not found");
        if (!isTerminal(mj.job.state)) {
          finish(mj, "CANCELED", null);
          emit(mj, "CANCELED", "canceled by operator", null);
        }
        return clone(mj.job);
      }),

    getVideo: (id) => respond(() => need<VideoDetail>(`video.${id}.json`, `no stored genome for ${id} in the mock`)),
    getDesign: (id, mode) => respond(() => need<DesignPreview>(`design.${id}.${mode}.json`, `no design preview for ${id}`)),

    startExperiment: (body: ExperimentRequest) =>
      respond(() => {
        const { idempotency_key, ...params } = body;
        const bodyKey = JSON.stringify(params);
        const existing = idempotency_key ? idempotency.get(idempotency_key) : undefined;
        if (existing) {
          const prior = jobs.get(existing);
          if (prior && prior.bodyKey !== bodyKey) throw new ApiError(409, "idempotency key was already used for a different request");
          if (prior) return { job_id: prior.job.id };
        }
        const script = fixture<ReplayScript>(`replay.${body.video_id}.json`);
        if (!script) throw new ApiError(404, "no recorded experiment to replay for this video");
        counter += 1;
        const job: Job = {
          id: `job_mock_${Date.now().toString(36)}_${counter}`,
          kind: "experiment",
          state: "QUEUED",
          stage: "QUEUED",
          created_at: nowIso(),
          started_at: null,
          ended_at: null,
          elapsed_ms: 0,
          error: null,
          experiment_id: null,
          params,
        };
        const mj: MockJob = { job, events: [], listeners: new Set(), timers: [], bodyKey };
        jobs.set(job.id, mj);
        if (idempotency_key) idempotency.set(idempotency_key, job.id);
        mj.timers.push(
          window.setTimeout(() => {
            mj.job.state = "RUNNING";
            mj.job.stage = "STARTING";
            mj.job.started_at = nowIso();
            for (const step of script.events) {
              mj.timers.push(
                window.setTimeout(() => {
                  if (isTerminal(mj.job.state)) return;
                  if (step.stage === "DONE") {
                    mj.job.experiment_id = script.experiment_id ?? null;
                    finish(mj, "COMPLETED", null);
                  }
                  emit(mj, step.stage, step.message, step.data);
                }, step.at_ms),
              );
            }
          }, 250),
        );
        return { job_id: job.id };
      }, 60),

    listExperiments: () => respond(() => need<ExperimentSummary[]>("experiments.json", "experiments not found")),
    getExperiment: (id) => respond(() => need<ExperimentDetail>(`experiment.${id}.json`, `experiment ${id} not found`)),
    getPolicy: () => respond(() => need<PolicyStore>("policy.json", "policy not found")),
    getCorpus: () => respond(() => need<Corpus>("corpus.json", "the reference corpus has not been built")),
    listTransfers: () => respond(() => fixture<TransferReport[]>("transfers.json") ?? []),
    getReviewSummary: () => respond(() => fixture<ReviewSummary>("reviews-summary.json") ?? { pairs: [], total_responses: 0 }),
    getReviewQr: () => respond(() => need<ReviewQr>("reviews-qr.json", "review link not available")),
  };
  return client;
}
