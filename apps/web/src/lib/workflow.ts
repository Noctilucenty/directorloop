import type { ABCRun, JobEvent, RunDetail } from "../api/types";

export type WorkflowStatus = "running" | "completed" | "failed" | "skipped" | "incomplete" | "cancelled";

/** Published by the backend and streamed in WORKFLOW_STAGE events. */
export interface WorkflowStage {
  id: string;
  parent_id: string | null;
  key: string;
  title: string;
  kind: string;
  iteration: number | null;
  status: WorkflowStatus;
  started_at: string | null;
  ended_at: string | null;
  duration_ms: number | null;
  weave_call_id: string | null;
  weave_url: string | null;
  inputs: Record<string, unknown>;
  summary: Record<string, unknown>;
}

export interface WorkflowNode extends Omit<WorkflowStage, "status"> {
  status: WorkflowStatus | "recorded";
  source: "stage" | "record";
  children: WorkflowNode[];
}

export interface WorkflowData {
  nodes: WorkflowNode[];
  stages: WorkflowNode[];
  source: "stage" | "record" | "empty";
}

const STATUSES = new Set(["running", "completed", "failed", "skipped", "incomplete", "cancelled"]);
const object = (v: unknown): Record<string, unknown> => v !== null && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : {};
const str = (v: unknown): string | null => typeof v === "string" && v.length > 0 ? v : null;
const duration = (v: unknown): number | null => typeof v === "number" && Number.isFinite(v) && v >= 0 ? Math.round(v) : null;

/** Trace URLs are navigational evidence, never executable or arbitrary third-party links. */
export function workflowTraceUrl(value: unknown): string | null {
  if (typeof value !== "string") return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && (url.hostname === "wandb.ai" || url.hostname.endsWith(".wandb.ai")) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

function normalize(value: unknown): WorkflowStage | null {
  const v = object(value);
  if (!str(v.id) || !str(v.title) || !STATUSES.has(String(v.status))) return null;
  return {
    id: String(v.id), parent_id: str(v.parent_id), key: str(v.key) ?? "stage", title: String(v.title),
    kind: str(v.kind) ?? "operation", iteration: typeof v.iteration === "number" ? v.iteration : null,
    status: v.status as WorkflowStatus, started_at: str(v.started_at), ended_at: str(v.ended_at),
    duration_ms: duration(v.duration_ms), weave_call_id: str(v.weave_call_id), weave_url: workflowTraceUrl(v.weave_url),
    inputs: object(v.inputs), summary: object(v.summary),
  };
}

function makeTree(stages: WorkflowNode[]): WorkflowNode[] {
  const byId = new Map(stages.map((s) => [s.id, s]));
  const roots: WorkflowNode[] = [];
  for (const stage of stages) {
    const parent = stage.parent_id ? byId.get(stage.parent_id) : undefined;
    // Missing parents and malformed cycles remain visible, without recursive rendering loops.
    const seen = new Set([stage.id]);
    let cursor = parent;
    while (cursor && !seen.has(cursor.id)) {
      seen.add(cursor.id);
      cursor = cursor.parent_id ? byId.get(cursor.parent_id) : undefined;
    }
    if (!parent || cursor) roots.push(stage);
    else parent.children.push(stage);
  }
  // Parallel review calls can finish in any order. The video windows are easier to
  // inspect in video-time order; this does not change span timing or execution order.
  for (const node of stages) {
    if (node.children.length && node.children.every((child) => child.key === "review_window")) {
      node.children.sort((a, b) => {
        const aStart = a.inputs.start_ms;
        const bStart = b.inputs.start_ms;
        return typeof aStart === "number" && typeof bStart === "number" ? aStart - bStart : 0;
      });
    }
  }
  return roots;
}

function recordedStages(run: RunDetail | ABCRun): WorkflowNode[] {
  const nodes: WorkflowNode[] = [];
  const abc = "attempts" in run;
  const root = `record:${run.id}`;
  const add = (suffix: string, title: string, key: string, summary: Record<string, unknown>, options: Partial<WorkflowNode> = {}) => {
    const node: WorkflowNode = {
      id: `${root}:${suffix}`, parent_id: root, key, title, kind: "operation", iteration: null,
      status: "recorded", started_at: null, ended_at: null, duration_ms: null, weave_call_id: null,
      weave_url: null, inputs: {}, summary, source: "record", children: [], ...options,
    };
    nodes.push(node);
    return node.id;
  };
  add("session", abc ? "A/B to C session" : "Video repair session", "session", {
    objective: abc ? run.context.objective : run.config.objective,
    annotations: run.annotations,
    error: run.error,
  }, {
    id: root, parent_id: null, kind: "session", status: run.status === "failed" ? "failed" : "recorded",
    started_at: run.created_at, ended_at: run.ended_at, duration_ms: duration(run.timings_ms.total_ms),
    weave_url: workflowTraceUrl(run.weave_url), weave_call_id: run.weave_call_id,
  });

  if (abc) {
    const reviewed = Object.entries(run.versions).filter(([label, v]) => (label === "A" || label === "B") && v.audit_id);
    if (reviewed.length) {
      const reviewId = add("originals", "Review the two inputs independently", "original_audit", {}, { kind: "review" });
      for (const [label, version] of reviewed) {
        add(`audit-${label}`, `Cold review · ${label}`, "original_audit", {
          version: label, video: version.title, audit_id: version.audit_id, audit_status: version.audit_status, notes: version.notes,
        }, { parent_id: reviewId, kind: "review", inputs: { label } });
      }
    }
    if (run.comparison) {
      add("compare-ab", "Compare A and B", "compare", {
        preference: run.comparison.best_supported || "No reliable preference", reason: run.comparison.best_supported_reason,
        comparable: run.comparison.comparable, confounds: run.comparison.confounds,
        dimensions: run.comparison.whole?.dimensions,
      }, { kind: "judgment" });
    }
    for (const attempt of run.attempts) {
      const group = add(`iteration-${attempt.index}`, `Attempt ${attempt.index} · Direct C`, "iteration", {
        decision: attempt.decision, reason: attempt.reason, next_action: attempt.next_action, next_action_reason: attempt.next_action_reason,
      }, { iteration: attempt.index, kind: "iteration", duration_ms: duration(attempt.timings_ms.attempt_ms) });
      const child = (suffix: string, title: string, key: string, summary: Record<string, unknown>, options: Partial<WorkflowNode> = {}) => add(`iteration-${attempt.index}-${suffix}`, title, key, summary, { parent_id: group, iteration: attempt.index, ...options });
      if (attempt.proposal) child("plan", "Choose a supported edit", "plan_repair", { ...attempt.proposal }, { kind: "planning" });
      if (attempt.render_hash) child("render", "Render candidate C", "render", { artifact_hash: attempt.render_hash, version: attempt.version_id }, { kind: "tool" });
      if (attempt.evaluation?.change_verification) child("verify", "Verify the edit happened", "verify", { ...attempt.evaluation.change_verification }, { kind: "mechanical" });
      if (attempt.evaluation?.candidate_audit_id) child("review", "Fresh review of C", "rejudge", { audit_id: attempt.evaluation.candidate_audit_id, audit_status: attempt.evaluation.candidate_audit_status }, { kind: "review" });
      const evaluation = attempt.evaluation as (typeof attempt.evaluation & { base_whole?: unknown; other_whole?: unknown; region?: unknown });
      const hasComparison = evaluation && [evaluation.vs_base, evaluation.vs_other, evaluation.target_region, evaluation.base_whole, evaluation.other_whole, evaluation.region].some((pair) => Object.keys(object(pair)).length > 0);
      if (evaluation && hasComparison) child("compare", "Compare C with the inputs", "compare", {
        outcome: evaluation.outcome, reason: evaluation.outcome_reason, improved: evaluation.improved,
        regressed: evaluation.regressed, uncertain: evaluation.uncertain, new_weaknesses: evaluation.new_weaknesses,
      }, { kind: "judgment" });
      child("decision", "Decide what to retain", "decision", { decision: attempt.decision, reason: attempt.reason, next_action: attempt.next_action, next_action_reason: attempt.next_action_reason }, { kind: "decision" });
      if (attempt.lesson_id) child("memory", "Store the lesson", "memory", { lesson_id: attempt.lesson_id }, { kind: "memory" });
    }
  } else {
    const originalAudit = run.audit_ids[0] ?? run.iterations[0]?.audit_id;
    if (originalAudit) add("original", "Cold review of the original", "original_audit", { audit_id: originalAudit }, { kind: "review" });
    for (const attempt of run.iterations) {
      const group = add(`iteration-${attempt.index}`, `Attempt ${attempt.index} · Repair`, "iteration", {
        decision: attempt.decision, reason: attempt.reason, next_action: attempt.next_action, next_action_reason: attempt.next_action_reason,
      }, { iteration: attempt.index, kind: "iteration", duration_ms: duration(attempt.timings_ms.iteration_ms) });
      const child = (suffix: string, title: string, key: string, summary: Record<string, unknown>, options: Partial<WorkflowNode> = {}) => add(`iteration-${attempt.index}-${suffix}`, title, key, summary, { parent_id: group, iteration: attempt.index, ...options });
      if (attempt.considered.length || attempt.finding_id) child("plan", "Choose a supported repair", "plan_repair", {
        finding: attempt.finding, edit: attempt.edit, reason: attempt.selection_reason || attempt.ranking_reason, considered: attempt.considered,
      }, { kind: "planning" });
      if (attempt.candidate_artifact_hash) child("render", "Render the candidate", "render", { version: attempt.candidate_version_id, artifact_hash: attempt.candidate_artifact_hash }, { kind: "tool", duration_ms: duration(attempt.timings_ms.render_ms) });
      if (attempt.change_verified !== null) child("verify", "Verify the edit happened", "verify", { verified: attempt.change_verified, checks: attempt.verification_checks }, { kind: "mechanical" });
      if (attempt.candidate_audit_id) child("review", "Fresh review of the candidate", "rejudge", { audit_id: attempt.candidate_audit_id }, { kind: "review", duration_ms: duration(attempt.timings_ms.fresh_review_ms) });
      if (attempt.outcome) child("compare", "Compare with the retained version", "compare", { outcome: attempt.outcome, target_resolved: attempt.target_resolved, improved: attempt.improved, regressed: attempt.regressed }, { kind: "judgment", duration_ms: duration(attempt.timings_ms.compare_ms) });
      child("decision", "Decide what to retain", "decision", { decision: attempt.decision, reason: attempt.reason, next_action: attempt.next_action, next_action_reason: attempt.next_action_reason }, { kind: "decision" });
      if (attempt.lesson_id) {
        const lesson = run.lessons.find((l) => l.lesson_id === attempt.lesson_id);
        child("memory", "Store the lesson", "memory", lesson ? { ...lesson } : { lesson_id: attempt.lesson_id }, { kind: "memory" });
      }
    }
  }
  if (run.final_decision || run.stop_reason) add("final", "Final selection", "final_selection", {
    decision: run.final_decision, retained_version: abc ? run.final_version : run.final_version_id, stop_reason: run.stop_reason,
  }, { kind: "decision" });
  return nodes;
}

/** Never manufacture live stages from a job's broad phase or from historical run records. */
export function buildWorkflow(stages: WorkflowStage[] = [], events: JobEvent[] = [], run?: RunDetail | ABCRun | null): WorkflowData {
  const merged = new Map<string, WorkflowStage>();
  const ingest = (value: unknown) => {
    const stage = normalize(value);
    if (!stage) return;
    const prior = merged.get(stage.id);
    // A replayed start event must not resurrect a finished persisted stage.
    if (prior && prior.status !== "running" && stage.status === "running") return;
    if (prior?.ended_at && stage.ended_at && Date.parse(prior.ended_at) > Date.parse(stage.ended_at)) return;
    merged.set(stage.id, stage);
  };
  const embedded = (run as (RunDetail | ABCRun) & { workflow_stages?: unknown[] } | null)?.workflow_stages;
  if (Array.isArray(embedded)) embedded.forEach(ingest);
  stages.forEach(ingest);
  [...events].sort((a, b) => a.seq - b.seq).forEach((e) => { if (e.stage === "WORKFLOW_STAGE") ingest(e.data?.stage); });
  const nodes: WorkflowNode[] = merged.size ? [...merged.values()].map((s) => ({ ...s, source: "stage", children: [] })) : run ? recordedStages(run) : [];
  return { nodes: makeTree(nodes), stages: nodes, source: merged.size ? "stage" : run ? "record" : "empty" };
}

export function stagePreview(node: WorkflowNode): string {
  for (const key of ["reason", "final_decision", "summary", "description", "reaction", "understanding", "cause", "stop_reason", "outcome", "decision", "selected_edit", "edit", "finding", "objective"]) {
    const value = node.summary[key];
    if (typeof value === "string" && value.trim()) return value.replace(/_/g, " ");
  }
  if (node.key === "review_window") {
    const start = node.inputs.start_ms ?? node.inputs.prefix_start_ms;
    const end = node.inputs.end_ms ?? node.inputs.prefix_end_ms;
    if (typeof start === "number" && typeof end === "number") return `${start}–${end} ms of the video`;
  }
  if (node.status === "running") return "Working on this step";
  if (node.source === "record") return "Evidence from the saved run";
  return node.children.length ? `${node.children.length} connected steps` : "Open to inspect the recorded evidence";
}

export function iterationContinuation(node: WorkflowNode): { title: string; detail: string; accepted: boolean } | null {
  if (node.key !== "iteration") return null;
  const own = node.summary;
  const decision = node.children.find((child) => child.key === "decision")?.summary ?? {};
  const action = str(own.next_action ?? decision.next_action);
  const result = str(own.decision ?? decision.decision);
  const accepted = result === "accept";
  if (!action && !result) return null;
  const title = action === "continue_from_candidate" ? "Continue from the accepted candidate"
    : action === "try_alternative" || action === "try_another_c" ? "Keep the baseline · try another edit"
    : accepted ? "Candidate retained" : result === "stop" ? "Stop · retain the current selection"
    : result === "incomplete" || result === "incomplete_keep_current" ? "Incomplete evidence · retain the baseline"
    : "Candidate not retained";
  return { title, detail: str(own.next_action_reason ?? decision.next_action_reason) ?? "", accepted };
}
