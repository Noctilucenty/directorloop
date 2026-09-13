# DirectorLoop web studio

React 18, TypeScript, and Vite. The frontend uses the current `docs/API_CREATIVE.md` contract and the shared API abstraction in `src/api/client.ts`.

```sh
npm install
npm run dev          # live local API; http://127.0.0.1:5173
npm run dev:recorded # explicit recorded fixture preview
npm test             # workflow, attention, historical transfer, causal API, and session checks
npm run build        # type-check and production build to dist/
```

The development server proxies `/api` and `/media` to the existing CoreWeave server at `http://127.0.0.1:8787`. Production uses same-origin endpoints; the backend serves `apps/web/dist` directly. An explicit `VITE_API_MODE=live|mock` environment variable overrides the mode. The frontend does not start the Python backend.

## Experience

- `/`: video upload, supported URL intake, stored videos, and an optional A/B workflow. Advanced controls are collapsed.
- `/screen/:screenId?video=:videoId&job=:jobId`: optional W&B Quick screen. Three sampled moments, model observations beside exact saved frames, and timestamp seeking. Suggestions and technical details are collapsed. Every suggestion requires review; it cannot supply an original audit to an edit experiment. See `docs/QUICK_SCREENING.md`.
- `/judge/:videoId`: chronological review. Selecting an attention window synchronizes playback, observations, transcript when source hashes match, and overlapping findings. Structural hypotheses remain distinct from cold-review findings.
- `/causal/:causalId?job=:jobId`: controlled experiment session. One real causal run connects hypotheses, evidence, isolated edits, render verification, blind comparisons, and conditional memory. Job IDs stay in the URL so refresh reconnects to SSE/polling.
- `/research/experiments`: earlier research experiment library.
- `/research/experiments/:id`: Observe, Hypothesize, Test & compare, and Learn chapters. Original and rendered variants are playable together. Recorded model results and mechanical checks retain their provenance.
- `/research/experiments/new/:videoId`: current design preview and experiment launch through the existing job API.
- `/research/transfer`: Video A → creative memory → Video B, with animated before/after rankings. Historical explanations only use evidence from the report's saved policy version.
- `/runs`: controlled-session history, earlier improvement runs, and A/B comparisons.
- `/runs/:runId`, `/compare/:abcId`: preserved earlier improvement and A/B infrastructure.
- `/research/policy`, `/research/corpus`, `/research/review`: detailed policy evidence, reference corpus, and human-test records.

## Motion and evidence

The studio uses a CSS 3D film-loop sculpture, scroll-entry reveals, pointer depth, native scrolling, and animated rank movement. The fixed Pause motion control stops decorative movement; system reduced-motion preferences are respected. Motion never animates invented result numbers or implies a running model job.

The recorded transfer demonstrates a changed ranking and a negative evaluation outcome. A changed plan is not labeled a proven improvement. Similarity scores, individual policy contributions, presentation-order votes, and human-test results appear only when supported by the API. No platform retention curve is inferred from model attention-risk windows.

Recorded preview runs use saved fixtures, not fresh model evaluations. Media in that mode still needs the local backend; unavailable files receive an explicit placeholder. Creating a live audit or experiment can invoke the configured model providers.

## Controlled sessions and private access

On a registered video's review page, **Test explanations** starts `POST /api/causal`. The default is a fresh original review, at most two experiment arms, 120 model calls, and 30 minutes. Options expose the objective, constraints, one to three arms, and plan-only mode. Reusing the currently displayed complete audit is explicit; the server checks its source hash and evaluator compatibility. Link-derived edits require the owner's rights confirmation. A lost launch response reuses the same idempotency key when the request is unchanged.

Only persisted workflow stages and actual job events appear in the graph. A completed job can be a no-edit decision, a plan only, a loss, or inconclusive evidence. The page preserves historical verdicts but highlights an `insufficient_evidence` comparison rather than presenting it as complete. Missing cost is **Unknown**. Source playback distinguishes 404 missing media and 409 changed bytes. The page never supplies local filesystem paths to the start API.

When the protected API returns 401, the studio asks for its access token. `POST /api/session` exchanges a transient input for the server's HttpOnly session cookie. The token is not persisted in localStorage, sessionStorage, logs, or URLs. Same-origin fetch, uploads, SSE, and native video requests use the cookie. A session unlocked in this page can be locked with `DELETE /api/session`. Local demo mode without a configured token stays open.

The intake uses the server's upload limit and disables link import when the capability is unavailable, showing the published reason. Production link import stays unavailable until restricted network egress is configured.

For a separate, explicitly isolated API during development, set `DL_WEB_API_PROXY` before starting Vite; the default remains port 8787. For example, `DL_WEB_API_PROXY=http://127.0.0.1:8795 npm run dev -- --host 127.0.0.1 --port 5174`. This changes the local proxy only, not saved evidence or production endpoint configuration.
