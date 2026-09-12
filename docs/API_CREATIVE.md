# DirectorLoop creative API (v2, experiment-centric)

Served by `directorloop/api/app.py` on `127.0.0.1:8787`. JSON unless noted. Times are ISO-8601 UTC; durations are
integer milliseconds. Every value that is a model judgment carries an evidence class. Evidence classes:
`real`, `historical`, `simulated`, `model_eval`, `human_test`, `mechanical`, `reference`. The UI must badge them and
must never present `model_eval`, `reference` or `simulated` values as audience retention.

Media: `GET /media/renders/{sha256}.mp4`, `GET /media/hooks/{file}.mp4`, `GET /media/source/{video_id}.mp4`
(HTTP Range supported). Media URLs in responses are relative (start with `/media/`).

## Health and status

`GET /api/health` ->
```json
{ "status": "ok", "weave": {"connected": true, "project": "leondragon3798-curio/directorloop", "traces_url": "https://..."},
  "providers": [{"name": "openai", "role": "probe", "model": "gpt-5.6-terra", "state": "verified_supported"}],
  "policy_version": 3, "corpus": {"references": 47, "with_metrics": 4, "patterns": 6},
  "review": {"public_url": "https://....trycloudflare.com" | null, "local_url": "http://127.0.0.1:8790"} }
```

## Videos

`GET /api/videos` -> `VideoSummary[]`
```ts
type VideoSummary = { video_id: string; title: string; duration_ms: number; category: string; source: string;
                      media_url: string; role: "demo_a" | "demo_b" | "reference" | "holdout" | "dev";
                      has_genome: boolean; retention_class: "real"|"historical"|"simulated"|null;
                      latest_experiment_id: string | null }
```

`GET /api/videos/{video_id}` -> `VideoDetail`
```ts
type Feature = { value: number | string | boolean | null; source: "mechanical"|"asr"|"vision_model"|"language_model"|"creator_declared"|"derived_from_plan"; confidence: number; note: string }
type Beat = { id: string; index: number; start_ms: number; end_ms: number; text: string;
              role: "hook"|"setup"|"context"|"problem"|"tension"|"proof"|"mechanism"|"payoff"|"cta"|"other";
              role_confidence: number; is_question: boolean; is_claim: boolean; redundant_with_beat_id: string | null }
type Hypothesis = { id: string; family: string; statement: string; region_start_ms: number; region_end_ms: number;
                    region_basis: string; detection_confidence: number;
                    evidence: {kind: string; text: string; source: string | null; ref: string | null}[];
                    counterevidence: {kind: string; text: string}[]; candidate_mutations: string[]; changed_variable: string }
type VideoDetail = VideoSummary & {
  genome: { artifact_hash: string; duration_ms: number; beats: Beat[]; shots: {id: string; start_ms: number; end_ms: number}[];
            hook: Record<string, Feature>; first_proof_ms: Feature; first_payoff_ms: Feature; context_before_proof_ms: Feature;
            longest_static_span_ms: Feature; longest_static_span_start_ms: Feature; shots_per_10s: Feature; speech_rate_wps: Feature;
            open_loops: {opened_ms: number; resolved_ms: number | null; description: string; confidence: number}[];
            motion_curve: [number, number][] } | null;
  investigation: { weak_region: {start_ms: number; end_ms: number; basis: string; evidence_class: string; description: string} | null;
                   hypotheses: Hypothesis[]; retention_note: string } | null;
  retention: { source_type: string; platform: string; points: {t_ms: number; remaining_fraction: number}[];
               avg_watch_time_ms: number | null; hold_3s: number | null; plays: number | null; caveats: string[] } | null }
```

`GET /api/videos/{video_id}/design?mode=learned|none` -> ranking preview without rendering
```ts
type Ranking = { mutation: string; hypothesis_id: string; family: string; score: number; detection_confidence: number;
                 reference_support: number | null; policy_mean: number | null; policy_wins: number; policy_losses: number;
                 policy_neutral: number; reason: string; description: string; changed_variable: string;
                 protected_variables: string[]; target_start_ms: number; target_end_ms: number; selected: boolean }
type DesignPreview = { video_id: string; mode: "learned"|"none"; policy_version: number; rankings: Ranking[]; skipped: string[] }
```

## Experiments (jobs)

`POST /api/experiments` body `{ "video_id": string, "policy_mode": "learned"|"none", "max_arms": 3, "record_policy": true, "idempotency_key": string }`
-> `{ "job_id": string }` (same idempotency key + same body returns the same job)

`GET /api/jobs/{job_id}` ->
```ts
type Job = { id: string; kind: "experiment"; state: "QUEUED"|"RUNNING"|"COMPLETED"|"FAILED"|"CANCELED";
             stage: string; created_at: string; started_at: string | null; ended_at: string | null; elapsed_ms: number;
             error: string | null; experiment_id: string | null; params: object }
```
`GET /api/jobs/{job_id}/events?after={seq}` -> `text/event-stream`; each event `data: {"seq": 3, "ts": "...", "stage": "RENDERING", "message": "...", "data": {...} | null}`.
Stages in order: `ANALYZING`, `DIAGNOSING`, `PLANNING`, `RENDERING`, `EVALUATING`, `DECIDING`, `LEARNING`, `DONE` (or `FAILED`).
`GET /api/jobs/{job_id}/events.json?after={seq}` -> same events as a JSON array (polling fallback).
`POST /api/jobs/{job_id}/cancel` -> Job.

`GET /api/experiments` -> `ExperimentSummary[]` (newest first)
```ts
type ExperimentSummary = { id: string; video_id: string; created_at: string; policy_mode: string; generation: number;
                           outcome: string | null; winner_label: string | null; arms: number; policy_version_before: number;
                           policy_version_after: number | null; total_ms: number | null; weave_url: string | null; recorded: boolean }
```

`GET /api/experiments/{id}` -> `ExperimentDetail`
```ts
type FitnessComponent = { name: string; value: number | null; unit: string; better: "higher"|"lower"; evidence: string; detail: string; trials: number | null; valid: number | null }
type Arm = { id: string; label: "control"|"A"|"B"|"C"|string; status: string; media_url: string | null; hook_media_url: string | null;
             duration_ms: number | null; render_ms: number | null; outcome: "win"|"loss"|"neutral"|"rejected"|null; outcome_reason: string | null;
             mutation: { type: string; description: string; changed_variable: string; protected_variables: string[]; hypothesis_id: string;
                         target_start_ms: number; target_end_ms: number; expected_benefit: string; risk: string } | null;
             fitness: { hard_gates_passed: boolean; gate_notes: string[]; components: FitnessComponent[] } | null;
             preference_reasons: { full: string[]; hook: string[] } }
type ExperimentDetail = ExperimentSummary & {
  question: string; hypotheses: Hypothesis[]; ranking: Ranking[]; arms: Arm[];
  decision: { outcome: string; winner_arm_id: string | null; reason: string; primary_metric: string; per_arm: Record<string, string> } | null;
  policy_updates: { strategy: string; mutation: string; outcome: string; before: object; after: object; policy_version: number }[];
  reference_patterns: { mutation: string; pattern: string; description: string; count: number; total: number; top: string | null; bottom: string | null; label: string }[];
  suite: { id: string; hash: string; questions: { id: string; text: string }[] };  // never answers
  timings_ms: Record<string, number>; model_calls: number; notes: string[] }
```

## Policy, corpus, transfer, human review

`GET /api/policy` ->
```ts
{ version: number;
  strategies: { id: string; mutation: string; scope: string; status: "REFERENCE_PRIOR"|"PROPOSED"|"SUPPORTED_OFFLINE"|"HUMAN_SUPPORTED"|"REAL_WORLD_SUPPORTED"|"CONTRADICTED"|"REJECTED";
                wins: number; losses: number; neutral: number; rejected: number; human: string | null; confidence: number;
                evidence: { experiment_id: string; video_id: string; outcome: string; note: string; weave_url: string | null }[] }[];
  changes: { version: number; experiment_id: string; strategy_id: string; before: object; after: object; created_at: string }[] }
```

`GET /api/corpus` -> `{ id, version, references: number, with_metrics: number, patterns: { pattern_id, description, count, total_comparable, support_ratio, top_group_count, top_group_total, bottom_group_count, bottom_group_total, performance_metric, note }[], label: "REFERENCE CREATIVE (owned Curio shorts; descriptive counts)" }`

`GET /api/transfer` -> newest first
```ts
type TransferReport = { video_id: string; policy_version: number; oracle_experiment: string;
  outcomes_on_b: Record<string, "win"|"loss"|"neutral"|"rejected">;
  modes: { none: TransferMode; learned: TransferMode }; first_choice_changed: boolean; weave_url: string | null; oracle_weave_url: string | null; created_at: string }
type TransferMode = { order: string[]; first_mutation: string | null; first_outcome_on_b: string | null; attempts_until_first_win: number | null;
                      model_calls_until_then: number; eval_and_render_ms_until_then: number; note: string | null }
```

`GET /api/reviews/summary` -> `{ pairs: { experiment_id: string; test_type: "hook"|"full"; arms: string[]; arm_labels: Record<string,string>; n: number; preferred: Record<string, number>; no_preference: number; label: string }[]; total_responses: number }`
`GET /api/reviews/qr` -> `{ url: string | null; qr_png: string | null /* data URI */; note: string }`

## UI rules

- Main screen answers: what is weak and why (hypotheses with evidence and counterevidence), what the agent will try first and
  why (ranking with reasons, learned vs no-memory), what happened (arm cards with components and outcome), what changed in the
  policy (before -> after, wins and losses), and what it does on the next video (transfer panel).
- Model preference values are labeled "MODEL EVAL: model continue-watching preference" and shown as shares with n calls.
  Human results are labeled "HUMAN TEST: blinded continue-watching preference, n = X". Never the word "retention" for either.
- Losses and rejected arms are shown with the same prominence as wins.
- No fake progress: show actual stages and elapsed time from job events.
