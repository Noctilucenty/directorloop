import { useEffect, useRef, useState } from "react";
import type { VersionView } from "../api/types";
import { fmtSeconds, shortHash } from "../lib/format";
import { Badge } from "./Badge";

interface Props {
  baseline: VersionView | null;
  candidate: VersionView | null;
  interval: { start_ms: number; end_ms: number } | null;
}

type Mode = "baseline" | "candidate" | "side_by_side";

function SinglePlayer({
  version,
  muted,
  label,
  registerRef,
}: {
  version: VersionView | null;
  muted: boolean;
  label: string;
  registerRef: (el: HTMLVideoElement | null) => void;
}) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [version?.media_url]);
  if (!version) {
    return (
      <div className="player-slot">
        <div className="player-label">{label}</div>
        <div className="player-empty">No {label.toLowerCase()} yet</div>
      </div>
    );
  }
  return (
    <div className="player-slot">
      <div className="player-label">
        {label} <span className="mono muted">V{version.index}</span> <span className="mono muted">{shortHash(version.artifact_hash)}</span>
      </div>
      {failed ? (
        <div className="player-empty" role="alert">
          Media could not be loaded from {version.media_url}
        </div>
      ) : (
        <video
          ref={registerRef}
          className="player-video"
          src={version.media_url}
          controls
          muted={muted}
          playsInline
          preload="metadata"
          onError={() => setFailed(true)}
        />
      )}
      <div className="player-meta mono muted">
        {fmtSeconds(version.duration_ms)} at {version.width}x{version.height}
      </div>
    </div>
  );
}

export function VersionPlayer({ baseline, candidate, interval }: Props) {
  const [mode, setMode] = useState<Mode>("baseline");
  const baseRef = useRef<HTMLVideoElement | null>(null);
  const candRef = useRef<HTMLVideoElement | null>(null);

  useEffect(() => {
    if (candidate && mode === "baseline" && candidate.status === "promoted") setMode("candidate");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [candidate?.id]);

  const active = mode === "candidate" ? candRef : baseRef;

  const playInterval = () => {
    const targets = mode === "side_by_side" ? [baseRef.current, candRef.current] : [active.current];
    if (!interval) {
      targets.forEach((v) => v?.play().catch(() => undefined));
      return;
    }
    targets.forEach((v) => {
      if (!v) return;
      v.currentTime = interval.start_ms / 1000;
      const stopAt = interval.end_ms / 1000;
      const onTime = () => {
        if (v.currentTime >= stopAt) {
          v.pause();
          v.removeEventListener("timeupdate", onTime);
        }
      };
      v.addEventListener("timeupdate", onTime);
      v.play().catch(() => undefined);
    });
  };

  return (
    <section className="panel player" aria-label="Version player">
      <div className="panel-head">
        <h2>Video</h2>
        <div className="segmented" role="tablist" aria-label="Which version to show">
          {(["baseline", "candidate", "side_by_side"] as Mode[]).map((m) => (
            <button
              key={m}
              type="button"
              role="tab"
              aria-selected={mode === m}
              className={`seg-btn ${mode === m ? "seg-active" : ""}`}
              onClick={() => setMode(m)}
              disabled={m !== "baseline" && !candidate}
            >
              {m === "side_by_side" ? "Side by side" : m === "baseline" ? "Baseline" : "Candidate"}
            </button>
          ))}
        </div>
      </div>
      <div className={mode === "side_by_side" ? "player-grid two" : "player-grid"}>
        {(mode === "baseline" || mode === "side_by_side") && (
          <SinglePlayer version={baseline} muted={false} label="Baseline" registerRef={(el) => (baseRef.current = el)} />
        )}
        {(mode === "candidate" || mode === "side_by_side") && (
          <SinglePlayer
            version={candidate}
            muted={mode === "side_by_side"}
            label="Candidate"
            registerRef={(el) => (candRef.current = el)}
          />
        )}
      </div>
      <div className="player-actions">
        <button type="button" className="btn btn-secondary" onClick={playInterval}>
          {interval ? `Play the important interval (${fmtSeconds(interval.start_ms)} to ${fmtSeconds(interval.end_ms)})` : "Play"}
        </button>
        {candidate ? (
          <Badge
            kind={candidate.status === "promoted" ? "ok" : candidate.status === "rejected" ? "bad" : "warn"}
            label={candidate.status.replace(/_/g, " ").toUpperCase()}
          />
        ) : null}
        {mode === "side_by_side" ? <span className="muted small">Only the baseline audio track is active.</span> : null}
      </div>
    </section>
  );
}
