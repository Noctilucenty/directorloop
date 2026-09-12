interface Props {
  disabled: boolean;
  running: boolean;
  onImprove: () => void;
  onCancel: () => void;
  policy: "learned" | "baseline";
  onPolicyChange: (p: "learned" | "baseline") => void;
}

export function ImproveButton({ disabled, running, onImprove, onCancel, policy, onPolicyChange }: Props) {
  return (
    <div className="improve-bar">
      <button type="button" className="btn btn-improve" onClick={onImprove} disabled={disabled || running} aria-busy={running}>
        {running ? "Improving" : "Improve"}
      </button>
      {running ? (
        <button type="button" className="btn btn-secondary" onClick={onCancel}>
          Cancel
        </button>
      ) : (
        <label className="policy-select">
          <span className="muted small">policy</span>
          <select value={policy} onChange={(e) => onPolicyChange(e.target.value as "learned" | "baseline")} disabled={disabled}>
            <option value="learned">learned (with memory)</option>
            <option value="baseline">baseline (no memory)</option>
          </select>
        </label>
      )}
    </div>
  );
}
