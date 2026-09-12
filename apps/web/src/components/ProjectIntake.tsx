import { useState } from "react";
import { getClient } from "../api/client";
import type { ProjectDetail, ProjectSummary } from "../api/types";
import { errorMessage } from "../lib/format";

interface Props {
  projects: ProjectSummary[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onImported: (project: ProjectDetail) => void;
}

const KNOWN_PACKS = ["packs/pack_a_stand_demo", "packs/pack_b_factual_short", "packs/pack_c_narrative"];

export function ProjectIntake({ projects, selectedId, onSelect, onImported }: Props) {
  const [packDir, setPackDir] = useState(KNOWN_PACKS[0]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const importPack = async () => {
    setBusy(true);
    setError(null);
    try {
      const client = await getClient();
      const project = await client.importProject(packDir);
      onImported(project);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="intake">
      <label className="intake-field">
        <span className="muted small">project</span>
        <select value={selectedId ?? ""} onChange={(e) => onSelect(e.target.value)} disabled={projects.length === 0}>
          {projects.length === 0 ? <option value="">none imported</option> : null}
          {projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name} ({p.pack_label})
            </option>
          ))}
        </select>
      </label>
      <label className="intake-field">
        <span className="muted small">import pack</span>
        <input list="known-packs" value={packDir} onChange={(e) => setPackDir(e.target.value)} className="input" />
        <datalist id="known-packs">
          {KNOWN_PACKS.map((p) => (
            <option key={p} value={p} />
          ))}
        </datalist>
      </label>
      <button type="button" className="btn btn-secondary" onClick={importPack} disabled={busy || !packDir}>
        {busy ? "Importing" : "Import"}
      </button>
      {error ? (
        <span className="bad small mono" role="alert">
          {error}
        </span>
      ) : null}
    </div>
  );
}
