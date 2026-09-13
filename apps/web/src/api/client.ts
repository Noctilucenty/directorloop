import type { CausalRequest, CausalRun, CausalStart, CausalSummary } from "./causal";
import type { ScreeningReport, ScreeningStart, ScreeningSummary } from "./screening";
import type {
  ABCRequest,
  ABCRun,
  ABCStart,
  ABCSummary,
  Audit,
  AuditSummary,
  Corpus,
  DesignPreview,
  ExperimentDetail,
  ExperimentRequest,
  ExperimentSummary,
  Health,
  IngestStart,
  Job,
  JobEvent,
  PolicyMode,
  PolicyStore,
  Repair,
  ReviewQr,
  ReviewSummary,
  RunDetail,
  RunRequest,
  RunStart,
  RunSummary,
  TransferReport,
  UploadResult,
  VideoDetail,
  VideoSummary,
} from "./types";

export interface ApiClient {
  readonly mode: "mock" | "live";
  getHealth(): Promise<Health>;
  listVideos(): Promise<VideoSummary[]>;
  /** Uploads one video file. onProgress receives real bytes sent over the wire. */
  uploadVideo(file: File, onProgress: (sent: number, total: number) => void, signal?: AbortSignal): Promise<UploadResult>;

  startCausal(body: CausalRequest): Promise<CausalStart>;
  listCausal(videoId?: string): Promise<CausalSummary[]>;
  getCausal(causalId: string): Promise<CausalRun>;

  startRun(body: RunRequest): Promise<RunStart>;
  listRuns(): Promise<RunSummary[]>;
  getRun(runId: string): Promise<RunDetail>;
  getAudit(auditId: string): Promise<Audit>;
  listAudits(videoId: string): Promise<AuditSummary[]>;
  /** Judge only: a cold-audience audit job for one video. */
  startAudit(videoId: string, idempotencyKey: string): Promise<{ job_id: string }>;
  startScreening(videoId: string, idempotencyKey: string): Promise<ScreeningStart>;
  getScreening(screenId: string): Promise<ScreeningReport>;
  listScreenings(videoId?: string): Promise<ScreeningSummary[]>;
  ingestUrl(url: string, idempotencyKey: string): Promise<IngestStart>;
  startAbc(body: ABCRequest): Promise<ABCStart>;
  listAbc(): Promise<ABCSummary[]>;
  getAbc(abcId: string): Promise<ABCRun>;
  getRepair(repairId: string): Promise<Repair>;

  getJob(jobId: string): Promise<Job>;
  /** Streams events with seq > afterSeq. Resolves when the server closes the stream; rejects on transport error. */
  streamJobEvents(jobId: string, afterSeq: number, onEvent: (event: JobEvent) => void, signal: AbortSignal): Promise<void>;
  /** Polling fallback: the same events as a JSON array. */
  getJobEvents(jobId: string, afterSeq: number): Promise<JobEvent[]>;
  cancelJob(jobId: string): Promise<Job>;

  // Research views (experiments, policy memory, transfer, corpus, human test)
  getVideo(videoId: string): Promise<VideoDetail>;
  getDesign(videoId: string, mode: PolicyMode): Promise<DesignPreview>;
  startExperiment(body: ExperimentRequest): Promise<{ job_id: string }>;
  listExperiments(): Promise<ExperimentSummary[]>;
  getExperiment(experimentId: string): Promise<ExperimentDetail>;
  getPolicy(): Promise<PolicyStore>;
  getCorpus(): Promise<Corpus>;
  listTransfers(): Promise<TransferReport[]>;
  getReviewSummary(): Promise<ReviewSummary>;
  getReviewQr(): Promise<ReviewQr>;
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export function isNotFound(err: unknown): boolean {
  return err instanceof ApiError && err.status === 404;
}

let cached: ApiClient | null = null;

export async function getClient(): Promise<ApiClient> {
  if (cached) return cached;
  // The comparison against a define-replaced literal lets the build drop the branch it does not use,
  // so a live build never ships the mock fixtures.
  if (import.meta.env.VITE_API_MODE === "live") {
    const mod = await import("./http");
    cached = mod.createHttpClient();
  } else {
    const mod = await import("./mock");
    cached = mod.createMockClient();
  }
  return cached;
}

export function apiMode(): "mock" | "live" {
  return import.meta.env.VITE_API_MODE === "live" ? "live" : "mock";
}
