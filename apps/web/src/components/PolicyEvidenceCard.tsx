import type { JobResult, PolicyRule } from "../api/types";
import { fmtPct } from "../lib/format";
import { Badge } from "./Badge";

interface Props {
  update: JobResult["policy_update"] | null;
  rule: PolicyRule | null;
}

function statusKind(status: string): "ok" | "warn" | "bad" | "neutral" {
  if (status === "validated_on_other_stories" || status === "human_supported") return "ok";
  if (status === "supported_on_dev") return "warn";
  if (status === "contradicted" || status === "rejected" || status === "retired") return "bad";
  return "neutral";
}

export function PolicyEvidenceCard({ update, rule }: Props) {
  if (!update && !rule) {
    return (
      <section className="panel" aria-label="What it learned">
        <h2>What it learned</h2>
        <div className="muted">No policy update from this run.</div>
      </section>
    );
  }
  const status = rule?.status ?? update?.status ?? "proposed";
  return (
    <section className="panel" aria-label="What it learned">
      <div className="panel-head">
        <h2>What it learned</h2>
        <Badge kind={statusKind(status)} label={status.replace(/_/g, " ").toUpperCase()} />
      </div>
      <div className="mono muted small">{rule?.id ?? update?.rule_id}</div>
      {rule ? (
        <>
          <p className="policy-text">
            When <em>{rule.trigger_condition}</em>, try <strong>{rule.recommended_action.replace(/_/g, " ")}</strong>.
            {rule.action_detail ? ` ${rule.action_detail}` : ""}
          </p>
          {rule.contraindications.length ? (
            <div className="muted small">Do not apply when: {rule.contraindications.join("; ")}</div>
          ) : null}
        </>
      ) : null}
      <div className="policy-counts">
        <span>
          <span className="mono">{rule ? rule.supporting.length : update?.support ?? 0}</span> supporting
        </span>
        <span>
          <span className="mono">{rule ? rule.counterexamples.length : update?.counter ?? 0}</span> counterexamples
        </span>
        {rule ? (
          <span>
            <span className="mono">{fmtPct(rule.confidence)}</span> confidence
          </span>
        ) : null}
        {rule ? (
          <span>
            <span className="mono">{new Set(rule.supporting.map((e) => e.story_family)).size}</span> story families
          </span>
        ) : null}
      </div>
      <div className="muted small">One successful edit is a proposed hypothesis, not a proven rule.</div>
    </section>
  );
}
