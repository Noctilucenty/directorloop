import { useState } from "react";
import { getLocalToken, setLocalToken } from "../api/http";
import type { HealthReady, ProviderEntry } from "../api/types";
import { fmtDate } from "../lib/format";
import { Badge } from "./Badge";

function stateBadge(state: ProviderEntry["state"]) {
  if (state === "verified_supported") return <Badge kind="ok" label="VERIFIED SUPPORTED" />;
  if (state === "verified_unsupported") return <Badge kind="bad" label="VERIFIED UNSUPPORTED" />;
  return <Badge kind="unverified" />;
}

export function ProviderStatus({ providers, health }: { providers: ProviderEntry[]; health: HealthReady | null }) {
  const [token, setToken] = useState(getLocalToken());
  return (
    <div>
      {health ? (
        <div className="panel">
          <h2>Service</h2>
          <div className="kv-inline">
            <span>
              status <strong>{health.status}</strong>
            </span>
            <span>
              mode <strong>{health.mode}</strong>
            </span>
            <span>
              db <strong>{health.db ? "ok" : "down"}</strong>
            </span>
            <span>
              ffmpeg <strong className="mono">{health.ffmpeg}</strong>
            </span>
            <span>
              weave{" "}
              <strong>
                {health.weave.enabled ? (health.weave.connected ? "connected" : "not connected") : "disabled"}
              </strong>{" "}
              <span className="mono muted small">{health.weave.project}</span>
              {health.weave.reason ? <span className="muted small"> ({health.weave.reason})</span> : null}
            </span>
          </div>
        </div>
      ) : null}
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th>Provider</th>
              <th>Role</th>
              <th>Key present</th>
              <th>State</th>
              <th>Modalities</th>
              <th>Model</th>
              <th>Smoke test</th>
              <th>Checked</th>
            </tr>
          </thead>
          <tbody>
            {providers.map((p) => (
              <tr key={p.name}>
                <td className="mono">{p.name}</td>
                <td>{p.role}</td>
                <td>{p.present ? "yes" : "no"}</td>
                <td>{stateBadge(p.state)}</td>
                <td className="small">{p.modalities.join(", ") || "n/a"}</td>
                <td className="mono small">{p.model ?? "n/a"}</td>
                <td className="small">{p.smoke_result ?? "not run"}</td>
                <td className="mono small">{p.checked_at ? fmtDate(p.checked_at) : "never"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="panel">
        <h2>Local API token</h2>
        <p className="muted small">
          Only needed when the API runs with DL_LOCAL_AUTH_TOKEN set. Stored in this browser only; never sent anywhere except the same-origin API.
        </p>
        <div className="intake">
          <input className="input" type="password" value={token} onChange={(e) => setToken(e.target.value)} placeholder="bearer token" autoComplete="off" />
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => {
              setLocalToken(token);
            }}
          >
            Save
          </button>
        </div>
      </div>
    </div>
  );
}
