import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { getClient } from "../api/client";
import {
  isTerminal,
  type FailureFinding,
  type JobView,
  type PolicyRule,
  type ProjectDetail,
  type ProjectSummary,
  type PromotionDecision,
  type RepairProposal,
  type VersionView,
} from "../api/types";
import { DecisionPanel } from "../components/DecisionPanel";
import { EditDiff } from "../components/EditDiff";
import { EvidencePanel } from "../components/EvidencePanel";
import { ImproveButton } from "../components/ImproveButton";
import { MetricVector } from "../components/MetricVector";
import { PolicyEvidenceCard } from "../components/PolicyEvidenceCard";
import { ProjectIntake } from "../components/ProjectIntake";
import { RunProgress } from "../components/RunProgress";
import { ErrorState, Loading } from "../components/States";
import { TopBar } from "../components/TopBar";
import { VersionPlayer } from "../components/VersionPlayer";
import { VersionTimeline } from "../components/VersionTimeline";
import { useJob } from "../hooks/useJob";
import { errorMessage, newIdempotencyKey } from "../lib/format";

interface EventDerived {
  finding: FailureFinding | null;
  proposal: RepairProposal | null;
  decision: PromotionDecision | null;
}

export function MainPage() {
  const { jobId: routeJobId } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();

  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [projectId, setProjectId] = useState<string | null>(searchParams.get("project"));
  const [project, setProject] = useState<ProjectDetail | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [activeJobId, setActiveJobId] = useState<string | null>(routeJobId ?? null);
  const [policy, setPolicy] = useState<"learned" | "baseline">("learned");
  const [actionError, setActionError] = useState<string | null>(null);
  const [rules, setRules] = useState<PolicyRule[]>([]);
  const [fallbackJobs, setFallbackJobs] = useState<JobView[] | null>(null);

  const tracker = useJob(activeJobId);
  const { job, events, elapsedMs } = tracker;
  const recorded = routeJobId !== undefined || job?.mode === "recorded";

  const loadProjects = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const client = await getClient();
      const list = await client.listProjects();
      setProjects(list);
      const chosen = projectId && list.some((p) => p.id === projectId) ? projectId : list[0]?.id ?? null;
      setProjectId(chosen);
      if (chosen) {
        setProject(await client.getProject(chosen));
      } else {
        setProject(null);
      }
    } catch (err) {
      setLoadError(errorMessage(err));
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    void loadProjects();
  }, [loadProjects]);

  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    getClient()
      .then((client) => client.getProject(projectId))
      .then((p) => {
        if (!cancelled) setProject(p);
      })
      .catch((err: unknown) => {
        if (!cancelled) setLoadError(errorMessage(err));
      });
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  useEffect(() => {
    if (routeJobId) setActiveJobId(routeJobId);
  }, [routeJobId]);

  // When a job reaches a terminal state, refresh versions and policies.
  useEffect(() => {
    if (!job || !isTerminal(job.state)) return;
    let cancelled = false;
    getClient().then(async (client) => {
      const [p, store] = await Promise.all([client.getProject(job.project_id), client.getPolicies()]);
      if (cancelled) return;
      setProject(p);
      setRules(store.rules);
    }).catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [job?.state, job?.id]);

  useEffect(() => {
    getClient()
      .then((client) => client.getPolicies())
      .then((store) => setRules(store.rules))
      .catch(() => undefined);
  }, [projectId]);

  const derived = useMemo<EventDerived>(() => {
    const out: EventDerived = { finding: null, proposal: null, decision: null };
    for (const e of events) {
      if (!e.data) continue;
      if (e.data.finding) out.finding = e.data.finding as FailureFinding;
      if (e.data.proposal) out.proposal = e.data.proposal as RepairProposal;
      if (e.data.decision) out.decision = e.data.decision as PromotionDecision;
    }
    return out;
  }, [events]);

  const finding = job?.result?.finding ?? derived.finding;
  const proposal = job?.result?.proposal ?? derived.proposal;
  const decision = job?.result?.decision ?? derived.decision;
  const comparison = job?.result?.comparison ?? null;
  const diffLines = job?.result?.diff_lines ?? [];

  const versions: VersionView[] = project?.versions ?? [];
  const baseline = useMemo(() => {
    const byId = job?.base_version_id ? versions.find((v) => v.id === job.base_version_id) : undefined;
    return byId ?? versions.find((v) => v.role === "baseline") ?? null;
  }, [versions, job?.base_version_id]);
  const candidate = useMemo(() => {
    if (job?.candidate_version_id) return versions.find((v) => v.id === job.candidate_version_id) ?? null;
    if (job) return null;
    const promoted = versions.filter((v) => v.status === "promoted").sort((a, b) => b.index - a.index);
    return promoted[0] ?? null;
  }, [versions, job]);

  const interval = finding && finding.start_ms !== null && finding.end_ms !== null ? { start_ms: finding.start_ms, end_ms: finding.end_ms } : null;
  const running = job !== null && !isTerminal(job.state);
  const failed = job !== null && (job.state === "FAILED" || job.state === "TIMED_OUT");

  const improve = async () => {
    if (!project || !baseline) return;
    setActionError(null);
    try {
      const client = await getClient();
      const key = newIdempotencyKey();
      const created = await client.startImprovementJob(project.id, baseline.id, key, policy);
      if (routeJobId) navigate("/");
      setActiveJobId(created.id);
      setFallbackJobs(null);
    } catch (err) {
      setActionError(errorMessage(err));
    }
  };

  const cancel = async () => {
    if (!job) return;
    try {
      const client = await getClient();
      await client.cancelJob(job.id);
    } catch (err) {
      setActionError(errorMessage(err));
    }
  };

  const openRecorded = async () => {
    if (!project) return;
    try {
      const client = await getClient();
      const jobs = await client.listJobs(project.id);
      const good = jobs.find((j) => j.state === "COMPLETED" && j.result?.decision);
      if (good) navigate(`/runs/${good.id}`);
      else setFallbackJobs(jobs);
    } catch (err) {
      setActionError(errorMessage(err));
    }
  };

  const ruleForCard = useMemo(() => {
    const id = job?.result?.policy_update?.rule_id;
    return (id ? rules.find((r) => r.id === id) : null) ?? null;
  }, [rules, job?.result?.policy_update?.rule_id]);

  if (loading && !project) return <Loading what="projects" />;
  if (loadError && !project) return <ErrorState title="Could not load projects" detail={loadError} onRetry={loadProjects} />;

  return (
    <div className="main">
      <TopBar project={project} job={job} elapsedMs={elapsedMs} recorded={recorded} />
      <div className="main-controls">
        <ProjectIntake
          projects={projects}
          selectedId={projectId}
          onSelect={(id) => {
            setProjectId(id);
            setSearchParams({ project: id });
            if (routeJobId) navigate(`/?project=${id}`);
            setActiveJobId(null);
          }}
          onImported={(p) => {
            setProjects((prev) => (prev.some((x) => x.id === p.id) ? prev : [...prev, p]));
            setProjectId(p.id);
            setProject(p);
          }}
        />
        <ImproveButton
          disabled={!project || !baseline}
          running={running}
          onImprove={improve}
          onCancel={cancel}
          policy={policy}
          onPolicyChange={setPolicy}
        />
      </div>
      {actionError ? (
        <div className="callout callout-bad" role="alert">
          {actionError}
        </div>
      ) : null}
      {failed ? (
        <div className="callout callout-bad live-failed" role="alert">
          <strong>{job.state === "TIMED_OUT" ? "LIVE RUN TIMED OUT" : "LIVE RUN FAILED"}</strong>
          {job.error ? <span className="mono"> {job.error}</span> : null}
          <button type="button" className="btn btn-secondary" onClick={openRecorded}>
            Open recorded successful run
          </button>
          {fallbackJobs && !fallbackJobs.some((j) => j.state === "COMPLETED") ? (
            <span className="muted small">No completed run is recorded for this project.</span>
          ) : null}
        </div>
      ) : null}
      {recorded && job ? (
        <div className="callout callout-warn">
          This is a recorded run from {job.ended_at ?? job.created_at}. It is not live.
        </div>
      ) : null}
      {project?.provenance_note ? <div className="muted small provenance">{project.provenance_note}</div> : null}
      <div className="grid-main">
        <div className="col">
          <VersionPlayer baseline={baseline} candidate={candidate} interval={interval} />
        </div>
        <div className="col">
          <RunProgress job={job} events={events} transport={tracker.transport} error={tracker.error} />
          <EvidencePanel finding={finding} />
          <MetricVector baseline={baseline?.evaluation ?? null} candidate={candidate?.evaluation ?? null} comparison={comparison} />
        </div>
        <div className="col">
          <EditDiff proposal={proposal} diffLines={diffLines} />
          <DecisionPanel decision={decision} />
          <PolicyEvidenceCard update={job?.result?.policy_update ?? null} rule={ruleForCard} />
        </div>
      </div>
      <VersionTimeline versions={versions} selectedId={candidate?.id ?? baseline?.id ?? null} />
    </div>
  );
}
