import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState, type ReactNode } from "react";
import { mediaFailure } from "../lib/causal";
import { markSessionLocked } from "../api/session";

export interface PlayerHandle {
  seek(ms: number): void;
  playInterval(startMs: number, endMs: number): void;
  play(): void;
  pause(): void;
  element(): HTMLVideoElement | null;
}

interface Props {
  src: string | null;
  label?: ReactNode;
  onTime?: (ms: number) => void;
  muted?: boolean;
  className?: string;
}

/** A 9:16 player. When the file cannot load, a quiet placeholder stands in and seeking still reports the time. */
export const VideoPlayer = forwardRef<PlayerHandle, Props>(function VideoPlayer({ src, label, onTime, muted, className }, ref) {
  const video = useRef<HTMLVideoElement | null>(null);
  const [failed, setFailed] = useState(false);
  const [failureStatus, setFailureStatus] = useState(0);
  const activeSource = useRef(src);
  activeSource.current = src;
  const stopAt = useRef<number | null>(null);
  const raf = useRef<number | null>(null);
  const onTimeRef = useRef(onTime);
  onTimeRef.current = onTime;

  useEffect(() => {
    setFailed(false);
    setFailureStatus(0);
    stopAt.current = null;
  }, [src]);

  const inspectFailure = async () => {
    setFailed(true);
    if (!src) return;
    // Fetch only headers/a byte on failure. Native media errors do not expose HTTP status.
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch(src, { headers: { Range: "bytes=0-0" }, signal: controller.signal });
      if (response.status === 401) markSessionLocked();
      if (activeSource.current === src) setFailureStatus(response.status);
      await response.body?.cancel();
    } catch { /* The generic transport failure remains visible. */ }
    finally { window.clearTimeout(timeout); }
  };

  const tick = useCallback(() => {
    const v = video.current;
    if (!v) return;
    const ms = v.currentTime * 1000;
    onTimeRef.current?.(ms);
    if (stopAt.current !== null && ms >= stopAt.current) {
      v.pause();
      stopAt.current = null;
    }
    if (!v.paused) raf.current = window.requestAnimationFrame(tick);
  }, []);

  useEffect(
    () => () => {
      if (raf.current !== null) window.cancelAnimationFrame(raf.current);
    },
    [],
  );

  useImperativeHandle(
    ref,
    () => ({
      seek(ms: number) {
        stopAt.current = null;
        onTimeRef.current?.(ms);
        const v = video.current;
        if (v && !failed) v.currentTime = ms / 1000;
      },
      playInterval(startMs: number, endMs: number) {
        onTimeRef.current?.(startMs);
        const v = video.current;
        if (!v || failed) return;
        v.currentTime = startMs / 1000;
        stopAt.current = endMs;
        void v.play().catch(() => undefined);
      },
      play() {
        if (video.current && !failed) void video.current.play().catch(() => undefined);
      },
      pause() {
        video.current?.pause();
      },
      element() {
        return failed ? null : video.current;
      },
    }),
    [failed],
  );

  return (
    <figure className={`player ${className ?? ""}`}>
      {label ? <figcaption className="player-label">{label}</figcaption> : null}
      <div className="player-frame">
        {src && !failed ? (
          <video
            ref={video}
            src={src}
            controls
            playsInline
            preload="metadata"
            muted={muted}
            onError={() => void inspectFailure()}
            onPlay={() => {
              if (raf.current !== null) window.cancelAnimationFrame(raf.current);
              raf.current = window.requestAnimationFrame(tick);
            }}
            onSeeked={() => onTimeRef.current?.((video.current?.currentTime ?? 0) * 1000)}
          />
        ) : (
          <div className="player-empty" role="img" aria-label="Video unavailable">
            <div className="player-empty-title">{src ? mediaFailure(failureStatus).title : "No video"}</div>
            <div className="player-empty-detail">{src ? mediaFailure(failureStatus).detail : "Nothing to play."}</div>
          </div>
        )}
      </div>
    </figure>
  );
});
