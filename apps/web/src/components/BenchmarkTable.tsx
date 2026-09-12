import type { BenchmarkRun } from "../api/types";
import { fmtDate, fmtMs } from "../lib/format";
import { Badge } from "./Badge";

function gateBadge(gate: BenchmarkRun["live_gate"]) {
  if (gate === "enabled") return <Badge kind="ok" label="LIVE GATE ENABLED" />;
  if (gate === "disabled") return <Badge kind="bad" label="LIVE GATE DISABLED" />;
  return <Badge kind="unverified" label="LIVE GATE UNTESTED" />;
}

export function BenchmarkTable({ runs }: { runs: BenchmarkRun[] }) {
  if (runs.length === 0) {
    return <div className="muted">No benchmark runs recorded. Run the Molab notebook and import its JSON.</div>;
  }
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th>When</th>
            <th>Environment / GPU</th>
            <th>Model / profile</th>
            <th>Output</th>
            <th>Samples</th>
            <th>Cold load</th>
            <th>p50 end-to-end</th>
            <th>p95 end-to-end</th>
            <th>Browser first frame</th>
            <th>Peak VRAM</th>
            <th>Failures</th>
            <th>Source</th>
            <th>Live gate</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => (
            <tr key={r.id}>
              <td className="mono small">{fmtDate(r.ran_at)}</td>
              <td>
                {r.environment}
                {r.gpu ? <div className="muted small">{r.gpu}{r.vram_gb ? `, ${r.vram_gb} GB` : ""}</div> : <div className="muted small">no GPU recorded</div>}
              </td>
              <td>
                <div className="mono small">{r.model}</div>
                <div className="muted small">{r.profile}</div>
              </td>
              <td className="mono">
                {r.output_seconds}s {r.width}x{r.height} {r.frames}f
              </td>
              <td className="mono">{r.samples}</td>
              <td className="mono">{fmtMs(r.cold_load_ms)}</td>
              <td className="mono">{fmtMs(r.p50_ms)}</td>
              <td className="mono">{fmtMs(r.p95_ms)}</td>
              <td className="mono">
                {r.browser_first_frame_ms && r.browser_first_frame_ms.length
                  ? fmtMs(r.browser_first_frame_ms.reduce((a, b) => a + b, 0) / r.browser_first_frame_ms.length)
                  : "not measured"}
              </td>
              <td className="mono">{r.peak_vram_gb === null ? "n/a" : `${r.peak_vram_gb.toFixed(1)} GB`}</td>
              <td className="mono">{r.failures}</td>
              <td>{r.source === "measured" ? <Badge kind="ok" label="MEASURED" /> : <Badge kind="neutral" label="IMPORTED" />}</td>
              <td>{gateBadge(r.live_gate)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {runs.map((r) => (
        <div key={`${r.id}-note`} className="muted small">
          {r.id}: {r.quality_note}
        </div>
      ))}
    </div>
  );
}
