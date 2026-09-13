# Quick screening

Quick screen is an optional, separately budgeted first look at a video. It uses
W&B hosted Qwen inference and local Whisper transcription. The final OpenAI
evaluator and controlled experiments keep their existing provider configuration.
No GPU notebook needs to stay running for this route.

Current operator constraint (2026-09-13): do not run the downloaded 100-video
collection through paid inference. The integration test used three videos only.
Do not treat the remaining budget as authorization to resume bulk analysis.

## Use

With `DL_SCREENING_ENABLED=true`, select or upload a video on the home page and
choose **Quick screen**. The result shows up to three moments: the first two
seconds, seconds two to four, and the final two seconds. Each request receives
only frames and transcript words available through its prefix boundary.

Select a moment, an observation, then its timestamp to inspect the saved frame
and seek the video. On-screen claims, transcript claims, interpretations, and
unknowns remain distinct. Citation membership is checked mechanically; semantic
truth is not established by a valid timestamp. Suggestions require review.

Protocol `grounded-screening-v3` incorporates the ARIA citation experiment:
quotes must match complete Unicode words in the supplied prefix, with NFC and
whitespace normalization. Case and punctuation remain significant. Empty quotes,
punctuation-only quotes, word/contraction fragments, and numeric fragments such
as `5` inside `-5` or `1.5` cannot anchor a claim or attention label. Scripts
without word separators are checked conservatively; this is not a language
segmentation or semantic validator. Timestamps retain strict integer typing and
must be nonnegative members of the actual supplied frame list.

Offline regression of the actual engine improved 37/39 to 39/39 on the adapted
ARIA development fixtures. All nine saved review windows retained their exact
previous issue lists. Four additional adversarial cases passed before and after
because the engine already had stricter safeguards than ARIA's prototype.
The ARIA prototype's separate 1/4-to-4/4 result is not an engine measurement.
Existing reports are preserved, and new reports record the validator protocol
alongside their code fingerprint. A passing citation still cannot authorize an
edit or establish semantic truth.

The model does not measure retention, predict views, or authorize an edit. A
natural endcard or signoff is not automatically a repair target. **Full review ·
OpenAI** is a separate paid action. Opening saved metadata or an existing screen
does not trigger new model calls.

## Budget and failure behavior

The route pins `Qwen/Qwen3.8-27B` on W&B Inference, disables thinking and SDK
retries, caps each output at 2,048 tokens, and disables compatibility fallbacks.
The persistent SQLite ledger reserves the published full-context worst case
before each physical request and settles returned usage. Unreturned usage remains
reserved. Unsupported or expired pricing, exhausted headroom, and disconnected
Weave tracing stop admission.

Default screening limits are **$2 and 30 physical attempts**. Admission needs
headroom for up to three requests; concurrent requests also reserve individually.
The budget id's limits are immutable. Do not remove the ledger or generate new
budget ids to reset spending automatically. This guard covers screening only;
`DL_APPROVED_SPEND_USD` does not cap final OpenAI reviews or experiments.

Cancellation is cooperative. A dispatched request can finish and incur usage.
Every completed prefix is saved atomically. A durable start marker prevents
automatic paid replay after process loss; recovery preserves partial evidence.
Starting another screen is an explicit new action.

## API and evidence

- `POST /api/screenings` accepts `video_id` and a stable `idempotency_key`;
  returns `job_id`, `screen_id`, and whether it created a job.
- `GET /api/screenings?video_id=...` returns summaries.
- `GET /api/screenings/{id}` returns the saved report and same-origin media URLs.
- Existing job status, event stream, cancellation, and session authentication
  apply. Original media and frame routes verify the recorded SHA-256.
- `/api/health` exposes screening availability and ledger headroom. It does not
  poll the provider's billing balance.

Reports live under `data/screenings`; local ASR caches use a separate protocol
directory. Keep these files and the spending ledger in backups. Each result
records source and prompt hashes, schema and sampling identity, actual request
policy, ASR provenance, returned usage, and a real Weave root with prefix/model
children. A trace link is not proof of complete remote delivery; inspect the
completed root and child spans for release evidence.

This is a screening feature in the existing private local app. Public deployment,
multi-user isolation, media-worker sandboxing, and final-provider spending limits
remain governed by `PRODUCTION_RUNBOOK.md`.
