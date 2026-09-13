# DirectorLoop presentation frontend

Original introduction and upload page with anonymous bounded live screening, plus a curated recorded Smoot experiment. No presenter code or account prompt. Each browser page generates a random session capability in memory; it never appears in URLs or storage.

Live screening calls a narrow HTTPS gateway connected to the existing local DirectorLoop engine. It uses the same canonical $2 / 30-attempt budget, at most three W&B calls per video, and real Weave traces. It requires human review and never runs an automatic edit.

The gateway only exposes uploaded-video screening and the submitting page’s own jobs/reports. File limits: 50 MB / 3 minutes. No corpus, arbitrary media, URL import, full-review or provider-key endpoints.

This is a presentation deployment: the Mac, engine and tunnel must stay available. It is not a standalone production backend. The recorded experiment remains usable when the live engine is unavailable.

Build: `npm ci && npm test && npm run build`. Serve `dist`.
Gateway source and isolated mock regression tests are included; all private config, ownership registry and spending state stay outside Git.
