# DirectorLoop presentation frontend

Introduction and upload page with bounded live screening and a recorded Smoot experiment. No presenter code or account prompt. A random page capability scopes access to the submitted jobs and reports.

Live screening calls a narrow HTTPS gateway connected to the private DirectorLoop engine. It reviews up to eight sections across the video, with at most one evidence-repair judgment per section: up to 16 logical model calls. Each physical provider dispatch also passes the persistent spending guard. The operator's current demo ledger has a $2 ceiling and an audited 160-attempt ceiling, preserving prior usage. Repository defaults remain 30 attempts; downloading or restarting the app does not reset a ledger or grant credits. Results require human review and do not trigger an automatic edit.

The gateway only exposes uploaded-video screening and the submitting page's own jobs/reports. File limits: 50 MB / 3 minutes. It provides no private corpus, arbitrary media, URL import, full optimization, or provider-key endpoints.

Refresh recovery keeps the page capability and submitted request/job identity in `sessionStorage`. An optional `#restore=...` URL fragment can carry an existing review capability; the page validates it, saves the recovery record, and removes the fragment with `history.replaceState`. It never contains provider credentials, but it does grant access to that review, so treat a restore link as private. Recovery reads the existing job and does not resubmit inference. No capability is stored in `localStorage`.

This is a presentation deployment: the Mac, engine and tunnel must stay available. It is not a standalone production backend. The recorded experiment remains usable when the live engine is unavailable.

Build: `npm ci && npm test && npm run build`. Serve `dist`.
Gateway source and isolated mock regression tests are included; all private config, ownership registry and spending state stay outside Git.
