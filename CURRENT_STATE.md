# DirectorLoop: current state

Handoff file for whoever continues this build (Claude Code or Codex). Only verified facts go here. Newest entry wins over
any older prompt or plan.

Last updated: 2026-09-12 14:50 PDT (main at 519f365)

## Direction (authoritative, in order)

1. `/Users/leon/Documents/ChatGPT/HACKATON/DIRECTORLOOP_AB_TO_C_COMPLETE_CLAUDE_HANDOFF.txt`: the flagship is A/B-to-C.
   Two edits of the same idea, independent cold audits, beat-aligned comparison, a directed C, a real render, a fresh
   review, a consistent decision and a lesson.
2. `DIRECTORLOOP_FINAL_GOAL.md` (in this repo): the cold-audience audit contract, repair routes A/B/C, honest outcomes.
3. The runtime acceptance test: from one launch (app, API or CLI), audit, diagnose, choose a repair, render, re-evaluate,
   compare and decide, including justified alternatives after failures and explicit stops.

## Who owns what right now

- Codex: W&B Weave trace readability (semantic span names and hierarchy). Nobody else edits tracing structure.
  Note: tests/unit/test_director.py and test_api_runs.py monkeypatch `director.run_audit`; a refactor that stops
  director.py calling `run_audit` must update those hooks, or the cancel test silently stops cancelling.
- Frontend agent (Claude subagent): `apps/web` redesign (upload or URL, judge, timeline findings, improve, A vs C,
  A/B-to-C mode) against the real API. It does not edit `directorloop/`.
- Claude main session: backend runtime and A/B-to-C (`directorloop/runtime`, `directorloop/compare`, API, CLI).

## Commands

```
.venv/bin/python -m pytest -p no:warnings        # 80 tests pass at 519f365
.venv/bin/ruff check directorloop tests scripts   # clean
.venv/bin/directorloop run VIDEO --objective TEXT [--constraint TEXT] [--budget N] [--focus X] [--allow-edit TYPE]
.venv/bin/directorloop abc A.mp4 B.mp4 --objective TEXT [--constraint TEXT] [--budget N] [--max-calls N] [--deadline S]
.venv/bin/directorloop show RUN_ID_OR_ABC_ID
.venv/bin/directorloop serve                      # API + web app on 127.0.0.1:8787
```

## API (loopback; optional bearer token DL_LOCAL_AUTH_TOKEN)

- GET /api/health (features: runs, abc true; url_ingest, classify false)
- POST /api/uploads (multipart "file"), GET /api/videos, /media/source/{video_id}.mp4
- POST /api/runs, GET /api/runs, GET /api/runs/{id}; GET /api/audits/{id}; GET /api/repairs/{id}; /media/audit/{id}/{t}.jpg
- POST /api/abc, GET /api/abc, GET /api/abc/{id}; /media/renders/{sha}.mp4
- GET /api/jobs/{id}, /events (SSE), /events.json, POST /api/jobs/{id}/cancel
- Older experiment endpoints remain: /api/experiments, /api/policy, /api/corpus, /api/transfer, /api/reviews/*

## Built and verified

| Area | State | Evidence |
|---|---|---|
| Cold-audience audit (unprimed 2 s prefix windows, whole-story diagnosis, close-up checks, coverage record) | working on real footage | audits in data/audit; runs below |
| Single-video runtime loop (`directorloop/runtime/director.py`) | working through CLI and API | 9 real runs; scripted decision tests |
| Render verification (duration, loudness envelope vs plan, sentence heard at planned position, pause shorter, planned reframe) | working, with negative controls | tests/integration/test_verify_change.py |
| Comparison (two-order pairwise, rules vs content two-version check, words mapped through the plan) | working | fixed defects listed below |
| A/B-to-C runner (`directorloop/runtime/abc.py`, `directorloop/compare/*`) | working through CLI and API | 2 completed real runs; tests/unit/test_abc.py, test_api_abc.py |
| Run limits (model calls, deadline) | enforced at the provider boundary | test_call_budget_limits; token usage recorded, cost left unknown |
| Weave tracing | every run nests under one root; 91-call run verified with zero orphans | call 01a09771-9df3-72d6-ac6f-16a29b47c514 |

## Real runs (all kept; model judgments, not audience data)

Single-video runs (`data/runs`):
- aptip, beavers-v7 (x3), aplaze, ocean-v7-before-quiet-repair: all stopped without a rendered repair. The needed repairs
  were captions, animation timing or new narration/shots (routes B/C), or the selector judged every executable edit
  mis-targeted. aplaze's audit found no weaknesses.
- run_1a097719df3_7d704e04 (aptip): one real punch-in rendered and verified, then rejected. The rejection came from two
  comparison defects that were fixed afterwards (annotated on the record); the fresh audit still flagged the weakness.
- run_1a0977320ab_3ff4d85e (aplaze): killed by the development session; recorded as a manual intervention.

A/B-to-C runs (`data/abc`):
- abc_1a09788a1d4_303d76b2, Smoot action-first-v1 (A) vs cinematic-reedit-v1 (B), rubric v1: A preferred overall in both
  orders (comprehension, information progression, visual-narration alignment). C1 = trim A's 1.1 s final hold: verified,
  tie, rejected. Attempt 2: the selector declined every option with reasons. Final: keep A. 43 calls, 117 s.
- abc_1a0978f0a08_002eb452, Smoot cinematic-reedit-v1 vs earlier-action-v3 (identical narration), rubric v2: A and B
  judged the same on every dimension. C1 = trim A's 0.5 s final hold: verified, tie, rejected. Attempt 2 declined.
  Final: keep both inputs. 35 calls, 122 s. Its "new weakness" was reviewer variance on unchanged footage; that case is
  now classified as uncertainty (04ecda5).
- In progress at this update: Smoot police-payoff-v5 vs relaxed-read-v6 (same words, 7.5 s vs 10.4 s), then
  progress-proof-v2 vs hybrid-v3.

Earlier experiment work (policy, Video A experiment, the losing Video B transfer result): `docs/POLICY_EVIDENCE.md`.

## Defects found on real footage today (fixed, with tests)

- A rule ("do not add new claims or on-screen text") was checked as on-screen content and judged lost.
- Target-moment clips used separate transcriptions whose boundary word differed; the judge preferred the original for it.
- Comparison crashed when an edit removed a finding's interval.
- Punch-in dropped up to 200 ms of sentence audio at the zoom boundary.
- Alignment folded an unmatched sentence into a match because longer text scored higher.
- A selector choice that named an unavailable option ended the run with a vague reason.

## Blunt limitations

- The polished Curio shorts rarely offer an executable single-video repair; most honest runs end in a justified stop.
- Whole-video pairwise verdicts are unstable for small edits; ties dominate when C differs by a short trim.
- Two audits of identical content disagree on findings (reviewer variance). Treat per-finding diffs with caution.
- The reviewer sees sampled frames plus the whisper transcript and loudness, not the audio or continuous motion.
- Weave's automatic OpenAI integration stores the sampled frames sent to the model inside the private Weave project.
- No Claude model runs inside the product runtime (OpenAI gpt-5.6-terra reviews, gpt-5.6-sol selects).
- TypeSafe: TYPESAFE_API_KEY is present; the native evidence helper at
  `/Users/leon/Documents/ChatGPT/HACKATON/integrations/typesafe_evidence.py` is not wired into this repo yet.
- The Weave project is private; judge access is not arranged.

## Next

1. Finish the A/B-to-C runs above, pick the demo pair from real results without tuning the evaluator, and document it.
2. URL ingestion (direct media first) and content classification endpoints for the new frontend flow.
3. Wire the frontend to /api/abc and /api/runs once the redesign lands; end-to-end UI test.
