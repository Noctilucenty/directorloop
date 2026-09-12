export function Loading({ what }: { what: string }) {
  return (
    <div className="state state-loading" role="status" aria-live="polite">
      Loading {what}
    </div>
  );
}

export function EmptyState({ title, detail }: { title: string; detail?: string }) {
  return (
    <div className="state state-empty">
      <div className="state-title">{title}</div>
      {detail ? <div className="state-detail">{detail}</div> : null}
    </div>
  );
}

export function ErrorState({ title, detail, onRetry }: { title: string; detail?: string; onRetry?: () => void }) {
  return (
    <div className="state state-error" role="alert">
      <div className="state-title">{title}</div>
      {detail ? <div className="state-detail mono">{detail}</div> : null}
      {onRetry ? (
        <button type="button" className="btn btn-secondary" onClick={onRetry}>
          Retry
        </button>
      ) : null}
    </div>
  );
}
