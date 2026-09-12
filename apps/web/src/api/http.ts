import { ApiError, type ApiClient, type ReviewAnswers } from "./client";
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

const TOKEN_KEY = "dl_local_token";

export function getLocalToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setLocalToken(token: string): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    // storage unavailable; ignore
  }
}

function authHeaders(): Record<string, string> {
  const token = getLocalToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...authHeaders(),
      ...(init?.headers ?? {}),
    },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = (await res.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
      else if (body.detail) detail = JSON.stringify(body.detail);
    } catch {
      // non-JSON error body
    }
    throw new ApiError(res.status, `${res.status} ${detail}`);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/** Parses a text/event-stream body and invokes onEvent for each `data:` payload. */
export async function readEventStream(
  path: string,
  onEvent: (event: JobEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  const res = await fetch(`/api${path}`, {
    headers: { Accept: "text/event-stream", ...authHeaders() },
    signal,
  });
  if (!res.ok || !res.body) {
    throw new ApiError(res.status, `event stream ${res.status} ${res.statusText}`);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const chunk = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      const dataLines = chunk
        .split("\n")
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trimStart());
      if (dataLines.length === 0) continue;
      const raw = dataLines.join("\n");
      try {
        onEvent(JSON.parse(raw) as JobEvent);
      } catch {
        // ignore malformed frames; the polling fallback keeps state correct
      }
    }
  }
}

export function createHttpClient(): ApiClient {
  return {
    mode: "live",
    healthReady: () => request<HealthReady>("/health/ready"),
    listProjects: () => request<ProjectSummary[]>("/projects"),
    importProject: (packDir) =>
      request<ProjectDetail>("/projects/import", { method: "POST", body: JSON.stringify({ pack_dir: packDir }) }),
    getProject: (id) => request<ProjectDetail>(`/projects/${encodeURIComponent(id)}`),
    listVersions: (projectId) => request<VersionView[]>(`/projects/${encodeURIComponent(projectId)}/versions`),
    getVersion: (id) => request<VersionDetail>(`/versions/${encodeURIComponent(id)}`),
    getComparison: (versionId, against) =>
      request<Comparison>(`/versions/${encodeURIComponent(versionId)}/comparison?against=${encodeURIComponent(against)}`),
    approveVersion: (versionId, note) =>
      request<VersionView>(`/versions/${encodeURIComponent(versionId)}/approve`, {
        method: "POST",
        body: JSON.stringify({ note }),
      }),
    rejectVersion: (versionId, note) =>
      request<VersionView>(`/versions/${encodeURIComponent(versionId)}/reject`, {
        method: "POST",
        body: JSON.stringify({ note }),
      }),
    startBaselineJob: (projectId, idempotencyKey) =>
      request<JobView>(`/projects/${encodeURIComponent(projectId)}/baseline-jobs`, {
        method: "POST",
        body: JSON.stringify({ idempotency_key: idempotencyKey }),
      }),
    startImprovementJob: (projectId, baseVersionId, idempotencyKey, policy) =>
      request<JobView>(`/projects/${encodeURIComponent(projectId)}/improvement-jobs`, {
        method: "POST",
        body: JSON.stringify({ base_version_id: baseVersionId, idempotency_key: idempotencyKey, policy }),
      }),
    getJob: (id) => request<JobView>(`/jobs/${encodeURIComponent(id)}`),
    listJobs: (projectId) => request<JobView[]>(`/projects/${encodeURIComponent(projectId)}/jobs`),
    cancelJob: (id) => request<JobView>(`/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST" }),
    streamJobEvents: (jobId, afterSeq, onEvent, signal) =>
      readEventStream(`/jobs/${encodeURIComponent(jobId)}/events?after=${afterSeq}`, onEvent, signal),
    getPolicies: () => request<PolicyStore>("/policies"),
    getBenchmarks: () => request<BenchmarkRun[]>("/benchmarks"),
    getProviders: () => request<ProviderEntry[]>("/providers"),
    createReviewSession: (projectId) =>
      request<{ token: string; url: string }>("/review/sessions", {
        method: "POST",
        body: JSON.stringify({ project_id: projectId }),
      }),
    getReview: (token) => request<ReviewAssignment>(`/review/${encodeURIComponent(token)}`),
    submitReview: (token, body: ReviewAnswers) =>
      request<{ recorded: boolean }>(`/review/${encodeURIComponent(token)}/answers`, {
        method: "POST",
        body: JSON.stringify(body),
      }),
    getReviewSummary: (projectId) =>
      request<ReviewSummary>(`/projects/${encodeURIComponent(projectId)}/review-summary`),
  };
}
