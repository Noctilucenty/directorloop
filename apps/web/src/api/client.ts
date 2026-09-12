import type {
  BenchmarkRun,
  Comparison,
  HealthReady,
  JobEvent,
  JobView,
  PolicyStore,
  ProjectDetail,
  ProjectSummary,
  ProviderEntry,
  ReviewAssignment,
  ReviewSummary,
  VersionDetail,
  VersionView,
} from "./types";

export interface ReviewAnswers {
  participant_id: string;
  answers: Record<string, string>;
  confusion_ms: number | null;
  consented: true;
}

export interface ApiClient {
  readonly mode: "mock" | "live";
  healthReady(): Promise<HealthReady>;
  listProjects(): Promise<ProjectSummary[]>;
  importProject(packDir: string): Promise<ProjectDetail>;
  getProject(id: string): Promise<ProjectDetail>;
  listVersions(projectId: string): Promise<VersionView[]>;
  getVersion(id: string): Promise<VersionDetail>;
  getComparison(versionId: string, against: string): Promise<Comparison>;
  approveVersion(versionId: string, note: string): Promise<VersionView>;
  rejectVersion(versionId: string, note: string): Promise<VersionView>;
  startBaselineJob(projectId: string, idempotencyKey: string): Promise<JobView>;
  startImprovementJob(
    projectId: string,
    baseVersionId: string | null,
    idempotencyKey: string,
    policy: "learned" | "baseline",
  ): Promise<JobView>;
  getJob(id: string): Promise<JobView>;
  listJobs(projectId: string): Promise<JobView[]>;
  cancelJob(id: string): Promise<JobView>;
  /** Streams events with seq > afterSeq. Resolves when the server closes the stream; rejects on transport error. */
  streamJobEvents(
    jobId: string,
    afterSeq: number,
    onEvent: (event: JobEvent) => void,
    signal: AbortSignal,
  ): Promise<void>;
  getPolicies(): Promise<PolicyStore>;
  getBenchmarks(): Promise<BenchmarkRun[]>;
  getProviders(): Promise<ProviderEntry[]>;
  createReviewSession(projectId: string): Promise<{ token: string; url: string }>;
  getReview(token: string): Promise<ReviewAssignment>;
  submitReview(token: string, body: ReviewAnswers): Promise<{ recorded: boolean }>;
  getReviewSummary(projectId: string): Promise<ReviewSummary>;
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

let cached: ApiClient | null = null;

export async function getClient(): Promise<ApiClient> {
  if (cached) return cached;
  const mode = (import.meta.env.VITE_API_MODE as string | undefined) === "live" ? "live" : "mock";
  if (mode === "live") {
    const mod = await import("./http");
    cached = mod.createHttpClient();
  } else {
    const mod = await import("./mock");
    cached = mod.createMockClient();
  }
  return cached;
}

export function apiMode(): "mock" | "live" {
  return (import.meta.env.VITE_API_MODE as string | undefined) === "live" ? "live" : "mock";
}
