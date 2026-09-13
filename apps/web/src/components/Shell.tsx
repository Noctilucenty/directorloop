import type { ReactNode } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";
import { apiMode } from "../api/client";
import type { VideoSummary } from "../api/types";
import { useAppState } from "../hooks/useAppState";
import { fmtS } from "../lib/format";
import { ChevronIcon } from "./Icons";

/** Small bordered label used for section names and step numbers. */
export function Frame({ children, tone, title }: { children: ReactNode; tone?: "accent" | "good" | "bad" | "warn" | "model"; title?: string }) {
  return (
    <span className={`frame-label${tone ? ` frame-${tone}` : ""}`} title={title}>
      {children}
    </span>
  );
}

export function TopBar() {
  const location = useLocation();
  const memory = /\/research\/(transfer|policy|corpus|review)/.test(location.pathname);
  return (
    <header className="topbar">
      <Link to="/" className="wordmark" aria-label="DirectorLoop, new video">
        <span className="brand-mark" aria-hidden="true"><i /><i /></span> DIRECTORLOOP
      </Link>
      <nav className="nav" aria-label="Primary">
        <NavLink to="/" end className={({ isActive }) => `nav-link${isActive || location.pathname.startsWith("/judge/") ? " is-active" : ""}`}>
          Analyze
        </NavLink>
        <NavLink to="/runs" className={() => `nav-link${location.pathname.startsWith("/research/experiments") || location.pathname.startsWith("/runs") || location.pathname.startsWith("/compare/") || location.pathname.startsWith("/causal/") ? " is-active" : ""}`}>
          Experiments
        </NavLink>
        <NavLink to="/research/transfer" className={() => `nav-link${memory ? " is-active" : ""}`}>
          Memory
        </NavLink>
      </nav>
      {apiMode() === "mock" ? (
        <span className="mode-flag" title="Stored records only; nothing can run">
          RECORDED PREVIEW
        </span>
      ) : null}
    </header>
  );
}

/** Shown when the API cannot be reached at all, so every empty or failed panel below has an obvious cause. */
export function ApiUnavailable() {
  const { healthError } = useAppState();
  if (!healthError || apiMode() === "mock") return null;
  return (
    <div className="api-down" role="alert">
      <span>{healthError}</span>
      <button type="button" className="btn btn-small" onClick={() => window.location.reload()}>
        Retry
      </button>
    </div>
  );
}

const RESEARCH = [
  { to: "/research/transfer", label: "Next-video transfer" },
  { to: "/runs", label: "Agent runs" },
  { to: "/research/policy", label: "Policy memory" },
  { to: "/research/corpus", label: "Reference corpus" },
  { to: "/research/review", label: "Human test" },
];

export function ResearchNav() {
  return (
    <nav className="subnav" aria-label="Research">
      <Frame>Evidence library</Frame>
      {RESEARCH.map((r) => (
        <NavLink key={r.to} to={r.to} className={({ isActive }) => `subnav-link${isActive ? " is-active" : ""}`}>
          {r.label}
        </NavLink>
      ))}
    </nav>
  );
}

/** Title-sized video picker for research views. */
export function VideoSelect({ options, value, onChange, label }: { options: VideoSummary[]; value: string | null; onChange: (id: string) => void; label: string }) {
  const current = options.find((v) => v.video_id === value) ?? null;
  return (
    <div className="video-select">
      <label className="sr-only" htmlFor="video-select">
        {label}
      </label>
      <span className="select-wrap">
        <select id="video-select" value={value ?? ""} onChange={(e) => onChange(e.target.value)}>
          {options.map((v) => (
            <option key={v.video_id} value={v.video_id}>
              {v.title}
            </option>
          ))}
        </select>
        <span className="select-sizer" aria-hidden="true">
          {current?.title ?? ""}
        </span>
        <ChevronIcon className="select-chevron" size={20} />
      </span>
      {current?.duration_ms ? <span className="mono muted">{fmtS(current.duration_ms)}</span> : null}
    </div>
  );
}

export function useVideoTitle(videoId: string | null | undefined): string {
  const { videos } = useAppState();
  if (!videoId) return "";
  return videos.find((v) => v.video_id === videoId)?.title ?? videoId;
}
