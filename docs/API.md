# DirectorLoop API contract (v1)

All routes are under `/api`. JSON in, JSON out. Times are ISO-8601 UTC strings; durations are integer
milliseconds. Every resource is scoped to a project; the local single-user mode binds to loopback and
uses an optional bearer token (`Authorization: Bearer <DL_LOCAL_AUTH_TOKEN>`) when configured.

Media is served by content hash: `GET /api/media/{artifact_hash}.mp4` with HTTP Range support.

## Health

- `GET /api/health/live` -> `{ "status": "ok" }`
- `GET /api/health/ready` ->
  ```json
  { "status": "ok" | "degraded",
    "db": true,
    "ffmpeg": "8.1.2",
    "providers": { "gemini": {"present": true, "smoke": "ok" | "untested" | "failed"}, "openai": {...},
                   "wandb_inference": {...}, "typesafe": {...}, "elevenlabs": {...} },
    "weave": { "enabled": true, "project": "leondragon3798-curio/directorloop", "connected": false,
               "reason": "WANDB_API_KEY missing" },
    "mode": "demo" | "production" }
  ```

## Projects

- `GET /api/projects` -> `ProjectSummary[]`
- `POST /api/projects/import` body `{ "pack_dir": "packs/pack_a_stand_demo" }` -> `ProjectDetail`
- `GET /api/projects/{id}` -> `ProjectDetail`
- `PATCH /api/projects/{id}/brief` body: partial `CreativeBrief` -> `ProjectDetail`

```ts
type ProjectSummary = { id: string; name: string; profile: GoalProfile; pack_label: string;
                        version_count: number; best_version_id: string | null; created_at: string }
type ProjectDetail = ProjectSummary & {
  brief: CreativeBrief;               // see domain/brief.py
  truth: { title: string; is_fictional: boolean; claim_count: number; summary: string };
  assets: AssetView[];
  versions: VersionView[];
  suite: { id: string; hash: string; split: string; story_family: string; question_count: number };
  provenance_note: string;            // e.g. "Rendered fixture, not a real product"
}
type AssetView = { id: string; label: string; kind: "video"|"audio"|"image"|"graphic";
                   origin: string; duration_ms: number | null; width: number | null; height: number | null;
                   media_url: string | null; content_hash: string }
```

## Versions

- `GET /api/projects/{id}/versions` -> `VersionView[]`
- `GET /api/versions/{id}` -> `VersionDetail`
- `GET /api/versions/{id}/comparison?against={baseline_version_id}` -> `Comparison` (domain/decision.py)
- `POST /api/versions/{id}/approve` / `POST /api/versions/{id}/reject` body `{ "note": string }`

```ts
type VersionView = { id: string; project_id: string; index: number; parent_version_id: string | null;
                     role: "baseline" | "candidate"; status: "baseline" | "promoted" | "rejected" |
                     "needs_review" | "no_gain" | "unevaluated";
                     artifact_hash: string; media_url: string; duration_ms: number;
                     width: number; height: number; created_at: string;
                     evaluation: EvaluationSummary | null; job_id: string | null }
type EvaluationSummary = { run_id: string; mode: "fresh"|"cached"|"recorded";
                           probe_modality: string; provider: string; model: string; trials: number;
                           questions_passed: number; questions_total: number; score: number;
                           mechanical_passed: boolean; constraints_passed: boolean;
                           failed_question_ids: string[]; latency_ms: number; weave_url: string | null }
type VersionDetail = VersionView & { plan: EditPlan; evaluation_run: EvaluationRun | null;
                                     diff_lines: string[]; plan_diff: object | null;
                                     questions: QuestionView[] }
type QuestionView = { id: string; text: string; modality: "visual"|"audio"|"either";
                      regression_guard: boolean; leakage_status: string }   // never the answer
```

## Jobs

- `POST /api/projects/{id}/baseline-jobs` body `{ "idempotency_key": string }` -> `JobView`
- `POST /api/projects/{id}/improvement-jobs` body
  `{ "base_version_id": string | null, "idempotency_key": string, "policy": "learned" | "baseline" }` -> `JobView`
- `GET /api/jobs/{id}` -> `JobView`
- `GET /api/jobs/{id}/events?after={seq}` -> `text/event-stream` of `JobEvent` (also `GET /api/jobs/{id}/events.json`)
- `POST /api/jobs/{id}/cancel` -> `JobView`
- `GET /api/projects/{id}/jobs` -> `JobView[]` (newest first; completed ones are the RECORDED RUNS)

```ts
type JobState = "QUEUED"|"PREFLIGHT"|"ANALYZING"|"EVALUATING"|"DIAGNOSING"|"PLANNING"|"GENERATING"|
                "RENDERING"|"REEVALUATING"|"REGRESSION_TESTING"|"DECIDING"|"COMPLETED"|
                "FAILED"|"CANCELED"|"TIMED_OUT"|"NEEDS_REVIEW"|"NEEDS_SOURCE_MATERIAL"|"NO_GAIN"
type JobView = { id: string; project_id: string; kind: "baseline"|"improve"; state: JobState;
                 stage: string; iteration: number; base_version_id: string | null;
                 candidate_version_id: string | null; created_at: string; started_at: string | null;
                 ended_at: string | null; elapsed_ms: number; deadline_ms: number; error: string | null;
                 mode: "live" | "recorded"; result: JobResult | null }
type JobResult = { finding: FailureFinding | null; proposal: RepairProposal | null;
                   decision: PromotionDecision | null; comparison: Comparison | null;
                   diff_lines: string[]; timings_ms: Record<string, number>;
                   weave_url: string | null; weave_call_id: string | null;
                   policy_update: { rule_id: string; status: string; support: number; counter: number } | null }
type JobEvent = { seq: number; ts: string; stage: string; message: string; data: object | null }
```

## Policies, benchmarks, providers

- `GET /api/policies` -> `PolicyStore` (domain/policy.py) plus `{ "version": number }`
- `GET /api/benchmarks` -> `BenchmarkRun[]`
  ```ts
  type BenchmarkRun = { id: string; ran_at: string; environment: string; gpu: string | null; vram_gb: number | null;
                        model: string; profile: string; output_seconds: number; width: number; height: number;
                        frames: number; cold_load_ms: number | null; warm_infer_ms: number[]; encode_ms: number[];
                        end_to_end_ms: number[]; browser_first_frame_ms: number[] | null; peak_vram_gb: number | null;
                        failures: number; samples: number; p50_ms: number | null; p95_ms: number | null;
                        quality_note: string; source: "measured" | "imported"; live_gate: "enabled"|"disabled"|"untested" }
  ```
- `GET /api/providers` -> capability registry: `{ name, role, present, state: "unknown"|"verified_supported"|"verified_unsupported", modalities, model, smoke_result, checked_at }[]`

## Human review (blinded)

- `POST /api/review/sessions` body `{ "project_id": string }` -> `{ "token": string, "url": string }`
- `GET /api/review/{token}` -> `{ "assignment": "A"|"B", "media_url": string, "questions": QuestionView[] & {options}, "consent_text": string }`
- `POST /api/review/{token}/answers` body `{ "participant_id": string, "answers": {question_id: option_id}, "confusion_ms": number | null, "consented": true }` -> `{ "recorded": true }`
- `GET /api/projects/{id}/review-summary` -> `{ "participants": number, "responses": number, "per_version": {...}, "procedure": string, "limitations": string }`

## SSE event stages (what the UI shows during Improve)

`PREFLIGHT`, `EVALUATING_BASELINE` (skipped when cached and says so), `DIAGNOSING`, `PLANNING`,
`GENERATING` (optional), `RENDERING`, `REEVALUATING`, `REGRESSION_TESTING`, `DECIDING`, `LEARNING`, `DONE`.
Each event carries a short message and, where available, `data` with structured content
(the finding, the proposal, the decision). The UI must show actual elapsed time and provider errors.
It must never animate a score counting up or imply progress that has not been reported.
