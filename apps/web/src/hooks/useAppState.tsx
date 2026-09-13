import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { getClient } from "../api/client";
import type { FeatureName, Health, VideoSummary } from "../api/types";
import { errorMessage } from "../lib/format";

const VIDEO_KEY = "dl_selected_video";
const DEFAULT_VIDEO = "aptip";

interface AppState {
  health: Health | null;
  healthError: string | null;
  /** A feature is on only when the API says so; a missing field counts as off. */
  feature: (name: FeatureName) => boolean;
  videos: VideoSummary[];
  videosLoading: boolean;
  videosError: string | null;
  reloadVideos: () => void;
  addVideo: (video: VideoSummary) => void;
  videoId: string | null;
  video: VideoSummary | null;
  setVideoId: (id: string) => void;
  /** Experiment ids produced by runs started in this browser session (research view badges). */
  sessionExperiments: Set<string>;
  markSessionExperiment: (id: string) => void;
}

const Ctx = createContext<AppState | null>(null);

function readStoredVideo(): string | null {
  try {
    return localStorage.getItem(VIDEO_KEY);
  } catch {
    return null;
  }
}

export function AppStateProvider({ children }: { children: ReactNode }) {
  const [health, setHealth] = useState<Health | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [videos, setVideos] = useState<VideoSummary[]>([]);
  const [videosLoading, setVideosLoading] = useState(true);
  const [videosError, setVideosError] = useState<string | null>(null);
  const [videoTick, setVideoTick] = useState(0);
  const [videoId, setVideoIdState] = useState<string | null>(() => readStoredVideo());
  const [sessionExperiments, setSessionExperiments] = useState<Set<string>>(() => new Set());

  useEffect(() => {
    let cancelled = false;
    getClient()
      .then((c) => c.getHealth())
      .then((h) => !cancelled && setHealth(h))
      .catch((err: unknown) => !cancelled && setHealthError(errorMessage(err)));
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    setVideosLoading(true);
    setVideosError(null);
    getClient()
      .then((c) => c.listVideos())
      .then((list) => {
        if (cancelled) return;
        setVideos(list);
        setVideoIdState((current) => {
          if (current && list.some((v) => v.video_id === current)) return current;
          return list.find((v) => v.video_id === DEFAULT_VIDEO)?.video_id ?? list[0]?.video_id ?? null;
        });
      })
      .catch((err: unknown) => !cancelled && setVideosError(errorMessage(err)))
      .finally(() => !cancelled && setVideosLoading(false));
    return () => {
      cancelled = true;
    };
  }, [videoTick]);

  const setVideoId = useCallback((id: string) => {
    setVideoIdState(id);
    try {
      localStorage.setItem(VIDEO_KEY, id);
    } catch {
      // storage unavailable
    }
  }, []);

  const value = useMemo<AppState>(
    () => ({
      health,
      healthError,
      feature: (name) => health?.features?.[name] === true,
      videos,
      videosLoading,
      videosError,
      reloadVideos: () => setVideoTick((t) => t + 1),
      addVideo: (v) => setVideos((prev) => [v, ...prev.filter((p) => p.video_id !== v.video_id)]),
      videoId,
      video: videos.find((v) => v.video_id === videoId) ?? null,
      setVideoId,
      sessionExperiments,
      markSessionExperiment: (id) => setSessionExperiments((prev) => new Set(prev).add(id)),
    }),
    [health, healthError, videos, videosLoading, videosError, videoId, setVideoId, sessionExperiments],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAppState(): AppState {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useAppState outside AppStateProvider");
  return ctx;
}
