import type { ReactNode } from "react";

export function Loading({ what }: { what: string }) {
  return (
    <div className="state state-loading" role="status" aria-live="polite">
      <span className="loading-bar" aria-hidden="true" />
      Loading {what}
    </div>
  );
}

export function EmptyState({ title, detail, action }: { title: string; detail?: ReactNode; action?: ReactNode }) {
  return (
    <div className="state state-empty">
      <div className="state-title">{title}</div>
      {detail ? <div className="state-detail">{detail}</div> : null}
      {action ? <div className="state-action">{action}</div> : null}
    </div>
  );
}

export function ErrorState({ title, detail, onRetry }: { title: string; detail?: string; onRetry?: () => void }) {
  return (
    <div className="state state-error" role="alert">
      <div className="state-title">{title}</div>
      {detail ? <div className="state-detail">{detail}</div> : null}
      {onRetry ? (
        <button type="button" className="btn" onClick={onRetry}>
          Retry
        </button>
      ) : null}
    </div>
  );
}
