# DirectorLoop web client

React 18 + TypeScript + Vite, no UI framework. Built against `docs/API.md`.

```
npm install
npm run dev        # mock API in the browser (VITE_API_MODE=mock from .env.development), http://localhost:5173
npm run build      # type-check, then build to dist/ with VITE_API_MODE=live (same-origin /api)
```

In dev, `/api` is proxied to `http://127.0.0.1:8787`; set `VITE_API_MODE=live` in `.env.development.local`
to talk to the real API from the dev server. In production FastAPI serves `dist/` and `/api` together.

Routes: `/` (the loop), `/runs`, `/runs/:jobId` (recorded run in the main layout), `/versions/:id`,
`/policies`, `/benchmarks`, `/providers`, `/review/:token` (blinded human review, no navigation).

The mock layer (`src/api/mock.ts`) simulates one Improve job over about 8 seconds with stage events, a
promoted candidate, and one recorded rejected run. Everything it shows is invented and labeled mock.
The two files in `public/mock/` are tiny synthetic test clips so the player has something to play.
