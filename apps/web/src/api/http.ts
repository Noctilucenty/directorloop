import type { CausalRun, CausalStart, CausalSummary } from "./causal";
import type { ScreeningReport, ScreeningStart, ScreeningSummary } from "./screening";
import { ApiError, type ApiClient } from "./client";
import { markSessionLocked } from "./session";
import type {
  ABCRun,
  ABCStart,
  ABCSummary,
  Audit,
  AuditSummary,
  Corpus,
  DesignPreview,
  ExperimentDetail,
  ExperimentSummary,
  Health,
  IngestStart,
  Job,
  JobEvent,
  PolicyStore,
  Repair,
  ReviewQr,
  ReviewSummary,
  RunDetail,
  RunStart,
  RunSummary,
  TransferReport,
  UploadResult,
  VideoDetail,
  VideoSummary,
} from "./types";

const enc = encodeURIComponent;

function detailOf(body: unknown, fallback: string): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && "message" in detail && typeof detail.message === "string") return detail.message;
  if (detail) return JSON.stringify(detail);
  return fallback;
}

export const UNREACHABLE = "Cannot reach the DirectorLoop API. Check that the server is running, then try again.";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`/api${path}`, {
      ...init,
      headers: {
        Accept: "application/json",
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
        ...(init?.headers ?? {}),
      },
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError(0, UNREACHABLE);
  }
  if (!res.ok) {
    if (res.status === 401) markSessionLocked();
    let detail = res.statusText;
    try {
      detail = detailOf(await res.json(), detail);
    } catch {
      // non-JSON error body
    }
    throw new ApiError(res.status, detail || `HTTP ${res.status}`);
  }
  if (res.status === 204) return undefined as T;
  try {
    return (await res.json()) as T;
  } catch {
    throw new ApiError(res.status, `The API answered ${path.split("?")[0]} with something that is not readable JSON.`);
  }
}

/** Reads a text/event-stream body. Only frames carrying a job event (a numeric seq) are delivered; the server's
 *  closing `event: end` frame ends the stream. */
async function readEventStream(path: string, onEvent: (event: JobEvent) => void, signal: AbortSignal): Promise<void> {
  const res = await fetch(`/api${path}`, { headers: { Accept: "text/event-stream" }, signal });
  if (res.status === 401) markSessionLocked();
  if (!res.ok || !res.body) throw new ApiError(res.status, `event stream ${res.status} ${res.statusText}`);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
    let idx: number;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const chunk = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      const lines = chunk.split("\n");
      const eventName = lines.find((l) => l.startsWith("event:"))?.slice(6).trim() ?? "message";
      const data = lines
        .filter((l) => l.startsWith("data:"))
        .map((l) => l.slice(5).trimStart())
        .join("\n");
      if (eventName === "end") {
        await reader.cancel().catch(() => undefined);
        return;
      }
      if (!data) continue;
      try {
        const parsed = JSON.parse(data) as JobEvent;
        if (typeof parsed.seq === "number") onEvent(parsed);
      } catch {
        // a malformed frame is skipped; polling keeps the state correct
      }
    }
  }
}

function upload(file: File, onProgress: (sent: number, total: number) => void, signal?: AbortSignal): Promise<UploadResult> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/uploads");
    xhr.responseType = "json";
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(e.loaded, e.total);
    };
    xhr.onload = () => {
      if (xhr.status === 401) markSessionLocked();
      if (xhr.status >= 200 && xhr.status < 300) resolve(xhr.response as UploadResult);
      else reject(new ApiError(xhr.status, detailOf(xhr.response, `upload failed (${xhr.status})`)));
    };
    xhr.onerror = () => reject(new ApiError(0, "the upload could not reach the API"));
    xhr.onabort = () => reject(new ApiError(0, "upload canceled"));
    signal?.addEventListener("abort", () => xhr.abort());
    const form = new FormData();
    form.append("file", file);
    xhr.send(form);
  });
}

export function createHttpClient(): ApiClient {
  return {
    mode: "live",
    getHealth: () => request<Health>("/health"),
    listVideos: () => request<VideoSummary[]>("/videos"),
    uploadVideo: upload,
    startCausal: (body) => request<CausalStart>("/causal", { method: "POST", body: JSON.stringify(body) }),
    listCausal: (videoId) => request<CausalSummary[]>(`/causal${videoId ? `?video_id=${enc(videoId)}` : ""}`),
    getCausal: (id) => request<CausalRun>(`/causal/${enc(id)}`),
    startRun: (body) => request<RunStart>("/runs", { method: "POST", body: JSON.stringify(body) }),
    listRuns: () => request<RunSummary[]>("/runs"),
    getRun: (id) => request<RunDetail>(`/runs/${enc(id)}`),
    getAudit: (id) => request<Audit>(`/audits/${enc(id)}`),
    listAudits: (videoId) => request<AuditSummary[]>(`/audits?video_id=${enc(videoId)}`),
    startAudit: (videoId, key) => request<{ job_id: string }>("/audits", { method: "POST", body: JSON.stringify({ video_id: videoId, idempotency_key: key }) }),
    startScreening: (videoId, key) => request<ScreeningStart>("/screenings", { method: "POST", body: JSON.stringify({ video_id: videoId, idempotency_key: key }) }),
    getScreening: (id) => request<ScreeningReport>(`/screenings/${enc(id)}`),
    listScreenings: (videoId) => request<ScreeningSummary[]>(`/screenings${videoId ? `?video_id=${enc(videoId)}` : ""}`),
    ingestUrl: (url, key) => request<IngestStart>("/ingest/url", { method: "POST", body: JSON.stringify({ url, idempotency_key: key }) }),
    startAbc: (body) => request<ABCStart>("/abc", { method: "POST", body: JSON.stringify(body) }),
    listAbc: () => request<ABCSummary[]>("/abc"),
    getAbc: (id) => request<ABCRun>(`/abc/${enc(id)}`),
    getRepair: (id) => request<Repair>(`/repairs/${enc(id)}`),
    getJob: (id) => request<Job>(`/jobs/${enc(id)}`),
    streamJobEvents: (id, after, onEvent, signal) => readEventStream(`/jobs/${enc(id)}/events?after=${after}`, onEvent, signal),
    getJobEvents: (id, after) => request<JobEvent[]>(`/jobs/${enc(id)}/events.json?after=${after}`),
    cancelJob: (id) => request<Job>(`/jobs/${enc(id)}/cancel`, { method: "POST" }),
    getVideo: (id) => request<VideoDetail>(`/videos/${enc(id)}`),
    getDesign: (id, mode) => request<DesignPreview>(`/videos/${enc(id)}/design?mode=${enc(mode)}`),
    startExperiment: (body) => request<{ job_id: string }>("/experiments", { method: "POST", body: JSON.stringify(body) }),
    listExperiments: () => request<ExperimentSummary[]>("/experiments"),
    getExperiment: (id) => request<ExperimentDetail>(`/experiments/${enc(id)}`),
    getPolicy: () => request<PolicyStore>("/policy"),
    getCorpus: () => request<Corpus>("/corpus"),
    listTransfers: () => request<TransferReport[]>("/transfer"),
    getReviewSummary: () => request<ReviewSummary>("/reviews/summary"),
    getReviewQr: () => request<ReviewQr>("/reviews/qr"),
  };
}
