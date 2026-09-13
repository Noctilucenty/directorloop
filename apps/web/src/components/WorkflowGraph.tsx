import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { ABCRun, JobEvent, RunDetail } from "../api/types";
import { buildWorkflow, iterationContinuation, stagePreview, workflowTraceUrl, type WorkflowNode, type WorkflowStage } from "../lib/workflow";
import "./WorkflowGraph.css";

export interface WorkflowGraphProps {
  stages?: WorkflowStage[];
  events?: JobEvent[];
  status?: string;
  weaveUrl?: string | null;
  mode?: "single" | "abc";
  compact?: boolean;
  run?: RunDetail | ABCRun | null;
}

const labels: Record<string, string> = {
  running: "Working", completed: "Completed", failed: "Failed", skipped: "Skipped", incomplete: "Incomplete",
  cancelled: "Canceled", recorded: "Recorded",
};
const kindLabels: Record<string, string> = {
  review: "Model review", judgment: "Model judgment", planning: "Planning", mechanical: "Mechanical check",
  tool: "Tool", decision: "Decision", memory: "Memory", iteration: "Iteration", session: "Session",
  agent: "Agent", llm: "Model call", operation: "Step",
};

function TraceIcon() {
  return <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M9 3h4v4M13 3 7 9M7 3H3v10h10V9" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" /></svg>;
}

function NodeIcon({ node }: { node: WorkflowNode }) {
  const key = node.key;
  return <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
    {key === "iteration" || key === "session" ? <><path d="M18 7a7 7 0 1 0 1 9M18 3v5h-5" /><path d="m19 13 1 3 3-1" /></>
      : /audit|judge|review|diagnose|compare/.test(key) ? <><path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6S2 12 2 12Z" /><circle cx="12" cy="12" r="2.5" /></>
        : key === "render" ? <><rect x="3" y="4" width="18" height="16" rx="3" /><path d="m10 8 6 4-6 4V8Z" /></>
          : key === "verify" ? <><path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z" /><path d="m8 12 3 3 5-6" /></>
            : key === "decision" || key === "final_selection" ? <><path d="M6 4v7a5 5 0 0 0 5 5h7M6 11a5 5 0 0 1 5-5h7m-3-3 3 3-3 3m0 4 3 3-3 3" /></>
              : key === "memory" ? <><rect x="4" y="3" width="16" height="18" rx="2" /><path d="M8 3v6h8V3M8 21v-7h8v7" /></>
                : <><rect x="4" y="4" width="16" height="16" rx="4" /><path d="M8 9h8M8 13h8M8 17h4" /></>}
  </svg>;
}

function measuredTime(ms: number | null): string | null {
  if (ms === null) return null;
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.floor(ms / 60000)}m ${Math.round(ms % 60000 / 1000)}s`;
}

function EvidenceValue({ value, depth = 0 }: { value: unknown; depth?: number }) {
  if (value === null || value === undefined) return <span className="wf-muted">Not recorded</span>;
  if (typeof value === "boolean") return <>{value ? "Yes" : "No"}</>;
  if (typeof value !== "object") return <>{String(value)}</>;
  if (depth >= 3) return <pre className="wf-raw">{JSON.stringify(value, null, 2)}</pre>;
  if (Array.isArray(value)) {
    if (!value.length) return <span className="wf-muted">None recorded</span>;
    return <><ul className="wf-evidence-list">{value.slice(0, 6).map((item, i) => <li key={i}><EvidenceValue value={item} depth={depth + 1} /></li>)}</ul>{value.length > 6 ? <details className="wf-more"><summary>{value.length - 6} more items</summary><ul className="wf-evidence-list">{value.slice(6).map((item, i) => <li key={i}><EvidenceValue value={item} depth={depth + 1} /></li>)}</ul></details> : null}</>;
  }
  return <EvidenceFields fields={value as Record<string, unknown>} depth={depth + 1} />;
}

function EvidenceFields({ fields, depth = 0 }: { fields: Record<string, unknown>; depth?: number }) {
  const entries = Object.entries(fields).filter(([, value]) => value !== undefined && value !== "");
  return <dl className="wf-evidence-fields">{entries.map(([key, value]) => <div key={key}><dt>{key.replace(/_/g, " ")}</dt><dd><EvidenceValue value={value} depth={depth} /></dd></div>)}</dl>;
}

function nodeLabel(node: WorkflowNode): string | null {
  const label = node.inputs.label ?? node.inputs.version_label ?? node.inputs.version ?? node.summary.version;
  return label === "A" || label === "B" ? label : null;
}

type FocusRequest = { id: string; request: number } | null;
const hasNode = (node: WorkflowNode, id: string): boolean => node.id === id || node.children.some((child) => hasNode(child, id));
const hasActiveNode = (node: WorkflowNode): boolean => node.status === "running" && node.source === "stage" || node.children.some(hasActiveNode);

function StageCollection({ nodes, depth, terminal, focus }: { nodes: WorkflowNode[]; depth: number; terminal: boolean; focus: FocusRequest }) {
  const pair = nodes.length === 2 && new Set(nodes.map(nodeLabel)).has("A") && new Set(nodes.map(nodeLabel)).has("B");
  return <ol className={`wf-flow${depth === 0 ? " wf-overview" : ""}${pair ? " wf-pair" : ""}`} aria-label={pair ? "Independent input reviews" : depth === 0 ? "Session overview" : "Workflow steps"}>
    {nodes.map((node, i) => <StageCard key={node.id} node={node} number={i + 1} depth={depth} terminal={terminal} focus={focus} />)}
  </ol>;
}

function StageCard({ node, number, depth, terminal, focus }: { node: WorkflowNode; number: number; depth: number; terminal: boolean; focus: FocusRequest }) {
  const panelId = useId();
  const buttonRef = useRef<HTMLButtonElement>(null);
  const [expanded, setExpanded] = useState(false);
  const activeBranch = !terminal && hasActiveNode(node);
  const [childrenOpen, setChildrenOpen] = useState(activeBranch);
  useEffect(() => { if (activeBranch) setChildrenOpen(true); }, [activeBranch]);
  useEffect(() => {
    if (!focus || !hasNode(node, focus.id)) return;
    if (node.id === focus.id) {
      setExpanded(true);
      const frame = window.requestAnimationFrame(() => {
        buttonRef.current?.focus({ preventScroll: true });
        buttonRef.current?.scrollIntoView({ block: "center", behavior: "auto" });
      });
      return () => window.cancelAnimationFrame(frame);
    } else setChildrenOpen(true);
  }, [focus, node.id]);
  const live = node.source === "stage" && node.status === "running" && !terminal;
  const state = node.status === "running" && terminal ? "incomplete" : node.status;
  const time = measuredTime(node.duration_ms);
  const continuation = iterationContinuation(node);
  const outcomeValue = node.summary.decision ?? node.summary.outcome;
  const outcome = typeof outcomeValue === "string" ? outcomeValue : null;
  const decisionStep = node.key === "decision" || node.key === "final_selection";
  const accept = decisionStep && outcome === "accept";
  const knownDecision: Record<string, string> = {
    accept: "Candidate accepted", reject_keep_current: "Keep current version", reject_keep_inputs: "Keep the inputs",
    incomplete_keep_current: "Insufficient evidence", incomplete: "Insufficient evidence", stop: "Stopped",
  };
  return <li className={`wf-node wf-state-${state}${live ? " wf-live" : ""}${node.key === "iteration" ? " wf-iteration" : ""}${decisionStep ? " wf-decision" : ""}${accept ? " wf-accepted" : ""}${expanded || childrenOpen && node.children.length ? " wf-open" : ""}`}>
    <span className="wf-junction" aria-hidden="true" />
    <article className="wf-card">
      <div className="wf-card-top">
        <span className="wf-node-icon"><NodeIcon node={node} /></span>
        <span className="wf-node-kind">{kindLabels[node.kind.toLowerCase()] ?? node.kind.replace(/_/g, " ")}</span>
        <span className="wf-node-index" aria-hidden="true">{String(number).padStart(2, "0")}</span>
        <span className={`wf-status${live ? " wf-status-live" : ""}`}><span aria-hidden="true" />{live ? "Live" : labels[state]}</span>
      </div>
      <button ref={buttonRef} type="button" className="wf-card-action" aria-expanded={expanded} aria-controls={panelId} onClick={() => setExpanded(!expanded)}>
        <span className="wf-card-title">{node.title}</span>
        <span className="wf-card-preview">{stagePreview(node)}</span>
        <span className="wf-card-bottom">
          {decisionStep && outcome && knownDecision[outcome] ? <span className={`wf-outcome${accept ? " wf-outcome-accepted" : ""}`}>{knownDecision[outcome]}</span> : null}
          {time ? <span className="wf-timing" title="Measured duration recorded by the backend">{time} measured</span> : null}
          <span className="wf-expand">{expanded ? "Close evidence" : "Inspect evidence"}<span aria-hidden="true">{expanded ? "−" : "+"}</span></span>
        </span>
      </button>
      <div id={panelId} className="wf-evidence" hidden={!expanded}>
        {Object.keys(node.summary).length ? <EvidenceFields fields={node.summary} /> : <p className="wf-muted">{live ? "The result will appear when this step finishes." : "No result summary was recorded for this step."}</p>}
        {Object.keys(node.inputs).length ? <details className="wf-inputs"><summary>Step inputs</summary><EvidenceFields fields={node.inputs} /></details> : null}
        <div className="wf-evidence-footer">
          {node.weave_url ? <a href={node.weave_url} target="_blank" rel="noreferrer">Open this step in Weave <TraceIcon /></a> : <span className="wf-muted">No individual trace link recorded</span>}
          {node.source === "record" ? <span className="wf-source-note">Saved run record</span> : <span className="wf-source-note">Recorded stage</span>}
        </div>
      </div>
    </article>
    {node.children.length ? <details className="wf-children" open={childrenOpen} onToggle={(event) => setChildrenOpen(event.currentTarget.open)}><summary><span>{node.children.length} {node.children.every((child) => child.key === "review_window") ? "review windows · video-time order" : "connected steps"}</span><span className="wf-branch-label">{node.key === "iteration" ? "Follow the attempt" : "Follow the evidence"}</span></summary><StageCollection nodes={node.children} depth={depth + 1} terminal={terminal} focus={focus} /></details> : null}
    {continuation && (childrenOpen || expanded) ? <div className={`wf-return${continuation.accepted ? " wf-return-accepted" : ""}`}><svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.4" aria-hidden="true"><path d="M15 3v7a4 4 0 0 1-4 4H4m4-4-4 4 4 4" /></svg><div><strong>{continuation.title}</strong>{continuation.detail ? <p>{continuation.detail}</p> : null}</div></div> : null}
  </li>;
}

export function WorkflowGraph({ stages, events, status, weaveUrl, mode, run, compact = false }: WorkflowGraphProps) {
  const titleId = useId();
  const [focus, setFocus] = useState<FocusRequest>(null);
  const workflow = useMemo(() => buildWorkflow(stages, events, run), [stages, events, run]);
  const runStatus = (status ?? run?.status ?? "").toLowerCase();
  const terminal = ["completed", "failed", "canceled", "cancelled", "incomplete"].includes(runStatus);
  const active = terminal ? 0 : workflow.stages.filter((s) => s.source === "stage" && s.status === "running" && !s.children.some((c) => c.status === "running")).length;
  const url = workflowTraceUrl(weaveUrl ?? run?.weave_url);
  const abc = mode === "abc" || run && "attempts" in run;
  const overview = workflow.nodes.length === 1 && workflow.nodes[0].key === "session" ? workflow.nodes[0].children : workflow.nodes;
  const attempts = run ? "attempts" in run ? run.attempts : run.iterations : null;
  const rendered = attempts?.filter((attempt) => "render_hash" in attempt ? attempt.render_hash : attempt.candidate_artifact_hash).length;
  const accepted = attempts?.filter((attempt) => attempt.decision === "accept").length;
  const retained = run ? "final_version" in run ? run.final_version : run.final_version_id : null;
  const latestRender = [...workflow.stages].reverse().find((stage) => stage.key === "render" && (stage.status === "completed" || stage.source === "record"));
  const latestIteration = [...workflow.stages].reverse().find((stage) => stage.key === "iteration");
  const reveal = (node: WorkflowNode) => setFocus((previous) => ({ id: node.id, request: (previous?.request ?? 0) + 1 }));
  return <section className="workflow-graph" aria-labelledby={titleId}>
    <div className="wf-heading"><div><p className="wf-eyebrow">The agent at work</p><h2 id={titleId}>Inside the loop</h2>{!compact ? <p className="wf-description">{abc ? "Independent reviews, supported edits, and an explicit decision about what to keep." : "Follow the review, the repair, and the decision to keep or try again."}</p> : null}</div>{url ? <a className="wf-trace-link" href={url} target="_blank" rel="noreferrer">Whole session in Weave <TraceIcon /></a> : null}</div>
    {run ? <div className="wf-result-strip"><div className="wf-result-heading"><span className="wf-result-label">{terminal ? "Session outcome" : "Recorded so far"}</span>{retained && run.final_decision ? <strong>{retained === "v0" ? "Original" : retained} {terminal ? "retained" : "selected"}</strong> : null}</div><p>{run.final_decision || (terminal ? run.stop_reason : "The agent has not made its final decision.")}</p><div className="wf-result-stats"><span><strong>{attempts?.length ?? 0}</strong> attempts recorded</span><span><strong>{rendered ?? 0}</strong> {abc ? "C candidates" : "candidates"} rendered</span><span><strong>{accepted ?? 0}</strong> accepted</span>{latestRender ? <button type="button" onClick={() => reveal(latestRender)}>Inspect rendered {abc ? "C" : "candidate"} <span aria-hidden="true">→</span></button> : latestIteration ? <button type="button" onClick={() => reveal(latestIteration)}>Inspect the attempt <span aria-hidden="true">→</span></button> : null}</div></div> : null}
    <div className="wf-toolbar"><span className={`wf-session-state${active ? " wf-session-live" : ""}`} aria-live="polite"><span aria-hidden="true" />{active ? `${active} ${active === 1 ? "step" : "steps"} active` : runStatus === "failed" ? "Session failed" : runStatus === "canceled" || runStatus === "cancelled" ? "Session canceled" : workflow.source === "record" ? "Recorded workflow" : terminal ? "Session finished" : workflow.source === "stage" ? "Recorded stages" : "Waiting for the first step"}</span><span className="wf-evidence-label">{compact ? "Model evaluation" : "Model judgments · mechanical checks · explicit decisions"}</span></div>
    {workflow.source === "record" ? <p className="wf-recorded-note">Reconstructed from this run’s saved records. Stage links and timings appear only where recorded.</p> : null}
    {workflow.source === "empty" ? <div className="wf-empty"><span className="wf-empty-node" aria-hidden="true" /><h3>The workflow starts here</h3><p>As the run publishes steps, its reviews, edits, and decisions connect here. Open a step to inspect its evidence.</p></div> : <StageCollection nodes={overview} depth={0} terminal={terminal} focus={focus} />}
    {run?.annotations.length ? <details className="wf-annotations"><summary>{run.annotations.length} notes added to this run</summary><ul>{run.annotations.map((note, index) => <li key={index}>{note}</li>)}</ul></details> : null}
    {!compact ? <p className="wf-footnote">A completed step means the operation finished. Acceptance is a separate decision. Model reviews are not measured audience retention.</p> : null}
  </section>;
}
