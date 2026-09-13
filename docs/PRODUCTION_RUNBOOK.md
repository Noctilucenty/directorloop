# DirectorLoop production readiness

The engine remains a private, single-operator workspace. The public Render demo
provides uploads and bounded live screening through a narrow authenticated gateway,
alongside a recorded experiment. The gateway restricts visitors to their submitted
jobs and reports; it does not expose the private corpus or general operator API.
This presentation deployment is not a public multi-tenant production service.
Bulk paid corpus analysis remains paused. The project has not established measured
Instagram prediction accuracy, and historical quota failures are not current balance
measurements.

## Public presentation

[DirectorLoop demo](https://directorloop-demo.onrender.com) combines approved saved
Smoot artifacts with fresh upload screening. The frontend contains no provider
credentials. The private gateway holds the engine credential, while the engine
holds provider credentials and its persistent spending ledger. The operator engine,
gateway and connection must stay available for live requests; recorded evidence
remains accessible independently. See `apps/demo/README.md` for the current request
limits and browser recovery behavior.

A recorded stage is not a live model request. A public trace link does not change
the private Weave project's access policy. Deployment receipts remain operator-local.

## Deployment boundary

- Use `DL_MODE=production` and a random `DL_LOCAL_AUTH_TOKEN` of at least 32
  characters. The application refuses production or non-loopback startup without
  that token. Keep the token in the deployment secret store and out of Git.
- Terminate HTTPS at a trusted reverse proxy. Production session cookies are
  Secure, HttpOnly, and SameSite=Strict. The proxy must preserve the actual origin
  and host; trust forwarded headers only from that proxy. Test the exact deployed
  origin, including cookie exchange and native video range requests.
- Serve the built frontend and API from the same origin. The unlock form exchanges
  a transient bearer token for a 12-hour signed cookie. API and media routes share
  authentication. The token is not placed in video URLs or browser local storage.
- This is one operator's workspace. There are no per-user artifact permissions,
  organizations, roles, or tenant isolation. Do not market it as a multi-tenant
  service or host unrelated customers in one data directory.
- Use one API/worker deployment against its private persistent data directory.
  Cross-connection lease tests do not establish a supported distributed-worker
  topology. Load balancing and multi-host execution need a dedicated queue and
  artifact-store design plus integration tests.

## Media and resource controls

Uploads use unique temporary files, configured size limits, atomic publication,
and serialized source-registry updates. Request bodies are bounded before complete
multipart parsing. Video inspection accepts a constrained set of local file
containers and does not permit network protocols through FFprobe.

Remote link import is disabled in production until restricted egress is provided.
The existing local link-import path has a DNS resolution/rebinding gap and external
downloader redirects are not a complete SSRF defense. Use file uploads for a private
deployment. Re-enabling links needs an isolated downloader with explicit outbound
network controls, not a cosmetic URL validator.

Before public exposure, put media decoding/rendering in an isolated worker with
bounded CPU, memory, disk, and runtime. Update media binaries through a tested
dependency process. Upload byte limits alone do not bound decoded dimensions,
duration, transcoding cost, archive growth, or malicious decoder behavior.

## Jobs, failures, and cost

The worker now sends owner-scoped liveness heartbeats independently of stage
events. Periodic maintenance detects expired leases after a quick restart and
reconciles saved run state and running stages. An interruption receipt preserves
the earlier artifact. Recovery never automatically retries paid inference or
renders a new candidate.

Cancellation is cooperative. An already dispatched provider request or blocking
media subprocess can continue until it returns or times out. Hard cancellation
and process isolation require a separate worker boundary. Do not promise instant
abort or reimbursement of in-flight calls.

Runtime call counts and deadlines are enforced at the provider wrapper. Returned
token usage is recorded, while billing for requests without usage receipts remains
unknown. `DL_APPROVED_SPEND_USD` is a declared approval setting, not a working dollar
meter or billing ceiling. The new opt-in full-review ledger atomically reserves
shared and per-job budgets with physical attempt accounting and retained unknowns.
At the earlier hardening checkpoint, that full-review ledger was disabled and its
conservative token bounds rejected the tested Terra/Sol request under a $2 cap.
This dated result does not describe the separate current sponsor-screening lane. See `FULL_REVIEW_SPENDING.md` for activation limits and actual scope.

The optional Quick screen route now has a separate persistent dollar and physical
attempt guard. It reserves conservative published context/output cost before each
request, preserves unknown usage, and fails closed on missing or expired pricing.
See `QUICK_SCREENING.md`. This separate guard does not cover final OpenAI audits or
controlled experiments, and is not a provider billing balance or account-wide cap.

An earlier September 13 activation checkpoint passed 361 backend tests, frontend contracts and
production build; npm reported zero known frontend dependency vulnerabilities.
The idle localhost worker was restarted with the exact previous environment.
All 8 jobs, 313 events, 199 saved JSON files and 9 screening attempts were preserved.
No new video inference was submitted. These historical counts are not the current release test total and do not certify
broad public operation. Run the release checks on the exact checkout being shipped.

Quota, authentication, timeout, and ingestion failures use safe public categories.
Traces redact credential fields, authorization strings, signed URL credentials,
and unsafe deeply nested representations. Keep production logs and trace projects
private and verify their access settings separately from application authentication.

## Evaluation release gates

- A rendered candidate or higher model score is not measured audience improvement.
- Keep the same frozen evaluator for original/candidate comparisons and report
  incomplete, negative, and inconclusive outcomes.
- Incomplete comparison evidence cannot enter the conditional policy. A conservative
  decision to keep the original is separate from a reliable learning example.
- The Instagram backtest freezes actual media identities, prediction methods,
  source hashes, and partitions before unblinding metrics. No matching published
  videos currently passed admission, so historical accuracy remains unavailable.
- Compare candidate calculations against training-only account and duration
  baselines, with explicit held-out sample counts and uncertainty. Do not promote
  a model based only on its development fit or reuse an external test to tune it.

## Operator checks before a private release

1. Build the frontend and run the complete backend, frontend contract, and security
   suites on the exact release checkout. Preserve the test log and source identity.
2. Back up the jobs database, source registry, policy, uploads, and immutable run
   artifacts. Perform a restore into an isolated directory and verify hashes.
3. Test HTTPS login, expiry, logout, cross-origin rejection, unauthorized video
   requests, valid range playback, upload limits, and safe error messages at the
   real reverse proxy. Local unit tests do not validate proxy configuration.
4. Test a funded end-to-end optimization session, a quota failure, a slow provider,
   cancellation, and restart recovery. Inspect both the product and actual Weave
   hierarchy. Never use mocked model results as the release evidence.
5. Set retention and deletion policies for uploaded media and account data. Check
   trace, backup, and artifact access. Test disk-pressure behavior and queue limits.
6. Run bounded concurrency/load tests and a dependency vulnerability review, then
   record remaining risks. No load test, penetration test, multi-user assessment,
   or multi-tenant production certification was established by that local hardening pass.

## Commands

From the repository root:

```sh
.venv/bin/python -m pytest
.venv/bin/ruff check directorloop tests
npm --prefix apps/web run build
.venv/bin/python -m directorloop.cli --help
```

The runbook distinguishes implemented controls, historical validation and remaining
requirements. Operator-local receipts record the original checks; the release must
retain its own test output and source revision.
