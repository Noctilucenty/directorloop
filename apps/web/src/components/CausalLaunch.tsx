import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError, getClient } from "../api/client";
import type { CausalRequest } from "../api/causal";
import { useAppState } from "../hooks/useAppState";
import { errorMessage, newIdempotencyKey } from "../lib/format";
import { DEFAULT_OBJECTIVE } from "../lib/labels";
import { Frame } from "./Shell";

export function CausalLaunch({ videoId, auditId, needsRights, disabled = false, initialObjective }: { videoId: string; auditId?: string; needsRights: boolean; disabled?: boolean; initialObjective?: string }) {
  const navigate = useNavigate();
  const app = useAppState();
  const [objective, setObjective] = useState(initialObjective || DEFAULT_OBJECTIVE);
  const [constraints, setConstraints] = useState("");
  const [arms, setArms] = useState(2);
  const [planOnly, setPlanOnly] = useState(false);
  const [reuse, setReuse] = useState(false);
  const [rights, setRights] = useState(false);
  const [rightsRequired, setRightsRequired] = useState(needsRights);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inFlight = useRef(false);
  // Reuse after a lost response, but use a new key when the deliberate request changes.
  const launch = useRef<{ fingerprint: string; key: string } | null>(null);
  const start = async () => {
    if (inFlight.current) return;
    setError(null);
    if (!planOnly && (needsRights || rightsRequired) && !rights) { setOpen(true); return; }
    const lines = constraints.split("\n").map((line) => line.trim()).filter(Boolean);
    if (objective.trim().length < 3 || lines.length > 12 || lines.some((line) => line.length > 300)) { setOpen(true); setError("Use a goal of at least 3 characters and at most 12 constraints of 300 characters each."); return; }
    const body: Omit<CausalRequest, "idempotency_key"> = {
      video_id: videoId, objective: objective.trim(), constraints: lines,
      arms: planOnly ? 0 : arms, plan_only: planOnly, max_model_calls: 120, deadline_s: 1800,
      ...(rights ? { owner_confirms_rights: true } : {}),
      ...(reuse && auditId ? { audit_id: auditId } : {}),
    };
    const fingerprint = JSON.stringify(body);
    if (launch.current?.fingerprint !== fingerprint) launch.current = { fingerprint, key: newIdempotencyKey() };
    inFlight.current = true; setBusy(true);
    try {
      const started = await (await getClient()).startCausal({ ...body, idempotency_key: launch.current.key });
      navigate(`/causal/${encodeURIComponent(started.causal_id)}?job=${encodeURIComponent(started.job_id)}`);
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) { setRightsRequired(true); setOpen(true); }
      setError(errorMessage(err));
    } finally { inFlight.current = false; setBusy(false); }
  };
  return <div className="next-cta">
    <div className="next-copy"><Frame>02 · Controlled experiments</Frame><p className="next-line">Find competing explanations, test isolated edits, then compare each revision with the original. Weak evidence can end with no edit.</p>
      <p className="muted small">Starts a fresh original review by default. The run can use up to 120 model calls and 30 minutes.</p>
      {open ? <div className="improve-options">
        <label className="setting setting-wide"><span className="field-label">Goal</span><input value={objective} maxLength={500} disabled={busy} onChange={(e) => setObjective(e.target.value)} /></label>
        <label className="setting setting-wide"><span className="field-label">Constraints · one per line, up to 12</span><textarea rows={3} value={constraints} disabled={busy} onChange={(e) => setConstraints(e.target.value)} placeholder="Keep the original voice and factual claims" /></label>
        <label className="setting"><span className="field-label">Maximum experiments</span><select value={arms} disabled={busy || planOnly} onChange={(e) => setArms(Number(e.target.value))}><option value={1}>1</option><option value={2}>2</option><option value={3}>3</option></select></label>
        <label className="check"><input type="checkbox" checked={planOnly} disabled={busy} onChange={(e) => setPlanOnly(e.target.checked)} /><span>Plan only · review and reason, without rendering</span></label>
        {auditId ? <label className="check"><input type="checkbox" checked={reuse} disabled={busy} onChange={(e) => setReuse(e.target.checked)} /><span>Reuse this saved original review. The server checks its file hash and evaluator compatibility.</span></label> : null}
        {(needsRights || rightsRequired) && !planOnly ? <label className="check rights"><input type="checkbox" checked={rights} disabled={busy} onChange={(e) => setRights(e.target.checked)} /><span>I own this linked video or have permission to make an edited version.</span></label> : null}
      </div> : null}
    </div>
    <div className="next-actions"><button className="btn btn-quiet" onClick={() => setOpen(!open)} disabled={busy}>{open ? "Hide options" : "Experiment options"}</button><button className="btn btn-primary btn-hero" onClick={start} disabled={busy || disabled || !app.feature("causal")}>{busy ? "Starting session" : planOnly ? "Build experiment plan" : "Test explanations"}</button></div>
    {!app.feature("causal") ? <p className="muted small full-row">Controlled experiments need a live API with causal runs enabled.</p> : null}
    {error ? <p className="callout callout-bad full-row" role="alert">{error}</p> : null}
  </div>;
}
