import { Link } from "react-router-dom";
import { getClient } from "../api/client";
import type { JobView, ProjectSummary } from "../api/types";
import { Badge } from "../components/Badge";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { useAsync } from "../hooks/useAsync";
import { fmtDate, fmtMs } from "../lib/format";

interface RunsData {
  projects: ProjectSummary[];
  jobs: { project: ProjectSummary; job: JobView }[];
}

export function RunsPage() {
  const state = useAsync<RunsData>(async () => {
    const client = await getClient();
    const projects = await client.listProjects();
    const jobs: RunsData["jobs"] = [];
    for (const project of projects) {
      const list = await client.listJobs(project.id);
      list.forEach((job) => jobs.push({ project, job }));
    }
    jobs.sort((a, b) => Date.parse(b.job.created_at) - Date.parse(a.job.created_at));
    return { projects, jobs };
  }, []);
  if (state.loading) return <Loading what="runs" />;
  if (state.error || !state.data) return <ErrorState title="Could not load runs" detail={state.error ?? undefined} onRetry={state.reload} />;
  const { jobs } = state.data;
  return (
    <div className="page">
      <div className="page-head">
        <h1>Runs</h1>
        <span className="muted">Completed runs open in the main layout with a RECORDED RUN badge.</span>
      </div>
      {jobs.length === 0 ? <EmptyState title="No runs yet" /> : null}
      <table className="table">
        <thead>
          <tr>
            <th>Started</th>
            <th>Project</th>
            <th>Kind</th>
            <th>State</th>
            <th>Outcome</th>
            <th>Elapsed</th>
            <th>Mode</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {jobs.map(({ project, job }) => (
            <tr key={job.id}>
              <td className="mono small">{fmtDate(job.started_at ?? job.created_at)}</td>
              <td>{project.name}</td>
              <td>{job.kind}</td>
              <td className="mono">{job.state}</td>
              <td>{job.result?.decision ? job.result.decision.outcome.replace(/_/g, " ") : job.error ? <span className="bad">{job.error}</span> : ""}</td>
              <td className="mono">{fmtMs(job.elapsed_ms)}</td>
              <td>
                <Badge kind={job.mode === "recorded" || job.state === "COMPLETED" ? "recorded" : "live"} />
              </td>
              <td>
                <Link className="btn btn-secondary" to={`/runs/${job.id}`}>
                  Open
                </Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
