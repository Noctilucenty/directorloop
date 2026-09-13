# Protected single-operator deployment

The backend supports a protected single-operator service. This is not a multi-user tenancy or role system, and these local changes have not deployed a public service.

Set `DL_MODE=production`, `DL_BIND_HOST=0.0.0.0`, and a randomly generated `DL_LOCAL_AUTH_TOKEN` of at least 32 characters in the server environment. A production or non-loopback app refuses to start without a sufficiently long configured token. A local demo bound to `127.0.0.1`, `::1`, or `localhost` can continue without authentication when its token is empty. Do not expose that development mode through an unauthenticated tunnel or reverse proxy.

Serve production behind HTTPS. Preserve the public Host header and configure forwarded headers only for the trusted reverse proxy, so the application sees the same HTTPS origin as the browser. The frontend and `/api` and `/media` must share one origin. Production cookies are Secure and will not authenticate plain-HTTP playback.

## Browser access

`POST /api/session` exchanges `Authorization: Bearer <existing operator token>` for a signed, 12-hour, HttpOnly, SameSite=Strict cookie. The response contains only authentication state and expiry duration. Keep the operator token out of frontend builds, local storage, query strings, media URLs, logs and traces. A transient unlock form can discard its input after this exchange.

API endpoints, API documentation and media endpoints require the bearer or session cookie whenever a token is configured. Native same-origin video requests and range requests authenticate through the cookie. Unsafe cookie-authenticated requests must carry an Origin exactly matching the public request origin. Bearer clients do not need a browser cookie. `DELETE /api/session` clears the current browser cookie; rotating the server token and restarting invalidates all previously signed cookies.

## Upload boundaries

`DL_MAX_UPLOAD_MB` controls the file-byte limit. The ASGI boundary also caps the full multipart request at that limit plus 64 KiB of overhead, including chunked requests, before excess bytes are parsed or spooled. Other API requests are capped at 1 MiB. Failed or interrupted staging cleans up its own unique temporary file. Concurrent uploads use distinct files, and the process serializes registry updates before an atomic replacement. Run one API process: the upload registry and policy locks are process-local; multi-worker deployment has not been validated.

Uploaded and downloaded media are probed with a self-contained MOV/MP4 or Matroska/WebM container allowlist and file/pipe protocol allowlist. Probe failures return generic public errors. These restrictions are defense in depth, not an operating-system sandbox. Run the process as an unprivileged service account with access only to the intended data directory and required binaries. Keep FFmpeg and application dependencies maintained. Existing code and media tests verify accepted MP4 behavior.

## Remote URL ingestion

Remote link ingestion is disabled in production, including previously queued ingest jobs. Health reports `features.url_ingest=false` and explains the upload fallback. The endpoint returns HTTP 503 with `detail.code=url_ingest_unavailable`.

The local demo downloader checks resolved hosts and redirects, but DNS validation and the later HTTP connection are separate operations; this does not close DNS rebinding. Platform downloaders can also make their own redirected requests. Do not enable production link acquisition until outbound network access is constrained outside the process, with private, loopback, link-local and metadata destinations denied for both the HTTP downloader and any media tools. This gate does not claim that network isolation has been implemented.

## Deployment checks still required

Validate the real HTTPS reverse proxy, persistent data directory, service permissions, dependency installation, media binaries, worker shutdown/recovery and backup restoration on the intended host before calling it deployed. Provider access and billing must be verified separately from local tests. The separate public review service has its own signed participant links; it is not an operator login bypass.

Local validation covers authentication across API/docs/media, cookie tampering and expiry, origin rejection, production startup, native video range responses, chunked request limits, concurrent uploads, generic errors, production ingestion gates and real FFmpeg media fixtures.
