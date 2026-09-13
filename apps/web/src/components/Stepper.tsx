export type StepKey = "judge" | "improve" | "result";
export type StepState = "waiting" | "working" | "done" | "failed";

export interface StepView {
  key: StepKey;
  number: string;
  title: string;
  state: StepState;
  status: string;
}

export function Stepper({ steps, active, onPick }: { steps: StepView[]; active: StepKey; onPick: (key: StepKey) => void }) {
  return (
    <nav className="stepper" aria-label="Run steps">
      {steps.map((s) => (
        <button key={s.key} type="button" className={`step state-${s.state}${active === s.key ? " is-active" : ""}`} aria-current={active === s.key ? "step" : undefined} onClick={() => onPick(s.key)}>
          <span className="step-number mono">{s.number}</span>
          <span className="step-text">
            <span className="step-title">{s.title}</span>
            <span className="step-status">{s.status}</span>
          </span>
          <span className="step-dot" aria-hidden="true" />
        </button>
      ))}
    </nav>
  );
}
