# DirectorLoop: current state

Handoff file for whoever continues this build (Claude Code or Codex). Only verified facts go here. Newest entry wins over
any older prompt or plan.

Last updated: 2026-09-13 (bounded Quick screen integrated and validated locally)

## Latest verified update: cheaper screening, 2026-09-13

Operator constraint: Leon explicitly asked not to analyze the downloaded 100 clips
with API credits. Keep bulk paid analysis paused. The nine screening requests below
are already complete; remaining development checks use offline tests and saved data.

Main local server remains `http://127.0.0.1:8787` (PID 27719 after the final compatibility restart).
The home page now offers **Quick screen** using W&B hosted `Qwen/Qwen3.8-27B`,
with local ASR, three sampled prefixes, saved frame citations, timestamp seeking,
real Weave traces, and cooperative cancellation. Long summaries and suggestions
are collapsed. Existing saved screens can be opened without inference.

The screening provider has a persistent **$2 / 30 physical-attempt** guard,
budget id `screening-app-20260912-v1`; output cap 2,048, thinking off, no SDK retries
or compatibility fallback. Main Terra/Sol final-review settings remain unchanged
and are **not covered by this screening dollar cap**. Opening video metadata no
longer triggers paid analysis; experiment design requires a cached analysis.

Three distinct newly selected videos completed nine requests for a published-price
usage estimate of **$0.0318574**. The ledger has **$1.9681426** available and no
unsettled reservations. Remote Weave verification found three completed roots,
21 spans per root, three physical model requests per root, and no orphaned or
unfinished spans. Previous five jobs, 277 events, and 33 saved report hashes were
preserved. One screen is `needs_review` due to an ASR citation mismatch.

Quality boundary: all results still require review. Independent frame inspection
found unsupported activity counts, sponsorship, game rules, motion descriptions,
and object placement despite valid citations. These screens are not final audits,
retention/view forecasts, or policy evidence. Do not promote this model on this
three-video test. The coordinator saw an annotation summary before execution;
the test was not fully blinded, and reviewers were Codex agents rather than humans.

Verification: 330 backend tests passed; frontend tests and production build passed;
Chrome playback/seek, saved media hashes/ranges, invalid quote display, and same-key
launch deduplication were checked. No commit or push was made. The prior bulk batch
was not resumed. No GPU notebook runtime was started for this application feature.

See `docs/QUICK_SCREENING.md` for operation/configuration and
`/Users/leon/Documents/ChatGPT/HACKATON/screening-integration-2026-09-12/` for frozen
inputs, reports, quality review, remote trace proof, and screenshots. Public release
remains subject to `docs/PRODUCTION_RUNBOOK.md`; local completion is not a public
production-readiness claim. The human-evaluation module remains Leon's friend's scope.

## Earlier verified update: causal experiment loop (18:00 PDT)

Built: `directorloop/planning/causal.py` and `directorloop/runtime/causal.py`.
- Planning (deterministic): a symptom and evidence dossier; competing cause hypotheses with evidence for and against; single-variable experiments ranked with a stated formula, where one executable plan is one intervention; a do-not-edit decision; and a conditional policy with full provenance.
- Runner (`run_causal`): audit, dossier, hypotheses, policy lookup, ranking, then per arm a real render, render check, blind audit, frozen-evaluator check, two-order comparison, regression gate and verdict, then conclusions and a policy update.
- Every transition is a Weave stage. Planning inputs (base plan, candidate ops, policy snapshot and hash) are frozen in the record.

CLI:
- `directorloop causal VIDEO [--arms N] [--plan-only] [--audit ID] [--constraint TEXT]`
- `directorloop causal-replay CAUSAL_ID` re-runs a stored plan from its frozen inputs, with and without the policy.
- `directorloop show causal_...`

Records go to `data/causal/`; the policy is `data/creative/conditional_policy.json`. Verdicts win, neutral, loss and rejected are learned. Inconclusive verdicts (an order-dependent or failed comparison, or an unchecked constraint) and incomplete ones never are.

Priors admit only records that meet all three conditions:
- from a different source file (by SHA-256)
- judged by the current `COMPARISON_VERSION`
- not from scripted runs

Every contributing record and every exclusion reason is listed.

Comparator defects found on real Smoot A runs, fixed with tests (`tests/integration/test_compare_alignment.py`):
1. compare-v2: a shorter variant was sampled at different moments than its original, and judges read different caption states of identical footage as "adds on-screen text". Frames now show the same source moments in both versions.
2. compare-v2: a target moment the original wins in both orders now counts as a regression.
3. compare-v3: the catch-all issue type "other" no longer makes an unrelated fresh finding count as the target weakness persisting.

Also, every protected item and constraint now reaches the check (the 8-item truncation is gone), with a per-item receipt. `directorloop/compare/judge.py` (A/B-to-C) still samples whole videos evenly and probably has defect 1 for trims. Flagged, not changed.

Real runs on Smoot action-first-v1 (sha 6a9f3a80), two arms each: TRIM_PAUSE for dead air after the payoff, and PUNCH_IN for a static visual.

| Run | Comparison | TRIM_PAUSE | PUNCH_IN | Calls |
|---|---|---|---|---|
| `causal_1a0982fe0c9_42314891` | v1 | rejected (false text violation from misaligned frames) | neutral | 54 |
| `causal_1a09838ed04_c342b306` | v2 | neutral (false persistence via "other") | loss | 55 |
| `causal_1a0983dcee4_0238d250` | v3 | win | loss | 51 |

In the v3 run:
- TRIM_PAUSE: the variant's blind audit was low risk in all 7 windows, the whole-video tie was stable in both orders, 4 protected items were checked, and nothing got worse.
- PUNCH_IN: the original's final moment was preferred in both orders, and predicted send and save/replay dropped.
- Conclusion: the results favour dead_air_after_payoff over static_visual. These are model judgments, not audience data.
- Weave root: `01a0983d-d189-78c3-9a5c-802d4088d423`.

The v1 and v2 records stay in the policy file but are excluded from priors.

Plan-only transfer checks (no renders) ran on six other files: ap-tacoma-v4, ap-penguin-feet, ap-spider-ear, smoot-hybrid-v3, smoot-cinematic-reedit-v1 and ap-tippe-v4.
- The v3 Smoot A outcomes were admitted on five and changed priority scores (TRIM_PAUSE +1.5, PUNCH_IN -1.5).
- They were excluded on ap-tippe-v4 (conditions matched 0.44).
- No first choice or selection changed on real footage, because the learned direction agreed with each file's own evidence.
- A rank change on a different file is proven only in unit tests (scripted records, exact replay).
- `causal-replay` reproduces the stored real plans exactly.

Checks:
- 145 tests pass; ruff is clean.
- Codex's read-only checker finds 0 FAIL on the executed runs.
- It FAILs `chosen_arms_accounted` on plan-only runs, because those records keep the would-run selection. The checker needs an adapter change for that.

Ownership: the human-evaluation module belongs to Leon's friend; do not duplicate it.

## Earlier update: connected workflow and Weave (15:38 PDT)

Leon confirmed the model account was topped up. A new real A/B-to-C validation completed:
`abc_1a097b6ace8_52d2739b`, root call `01a097b6-ace3-72b0-9b67-b6b92bfd89e0`.
It used 39 model calls in 93.183 seconds, rendered and verified one C, freshly reviewed it, and retained A because
C did not demonstrate a reliable improvement. Weave displayed $0.5618; runtime cost remains unknown without a price table.
Read-only remote verification found 228 calls, one root, no orphan parents, and all 60 workflow stages with correct
semantic ancestry. Existing audit wrappers and raw provider calls remain underneath the new semantic stages.

Codex added the workflow journal, semantic runtime/audit boundaries, and the connected `WorkflowGraph` mounted in
Claude's RunPage and ComparePage. Evaluation prompts, model choices, repair rules and acceptance logic were preserved.
The local server at `http://127.0.0.1:8787` serves the built frontend. Demo: `/compare/abc_1a097b6ace8_52d2739b`.
See `docs/WORKFLOW_OBSERVABILITY.md` for the implementation, exact validation, before/after hierarchy and handoff.
These changes coexist with Claude's broader uncommitted frontend work; do not reset or blanket-stage the working tree.

## Direction (authoritative, in order)

1. `/Users/leon/Documents/ChatGPT/HACKATON/DIRECTORLOOP_AB_TO_C_COMPLETE_CLAUDE_HANDOFF.txt`: the flagship is A/B-to-C.
   Two edits of the same idea, independent cold audits, beat-aligned comparison, a directed C, a real render, a fresh
   review, a consistent decision and a lesson.
2. `DIRECTORLOOP_FINAL_GOAL.md` (in this repo): the cold-audience audit contract, repair routes A/B/C, honest outcomes.
3. The runtime acceptance test: from one launch (app, API or CLI), audit, diagnose, choose a repair, render, re-evaluate,
   compare and decide, including justified alternatives after failures and explicit stops.

## Historical credit blocker (15:00 PDT; resolved for the 15:22 validation)

At 15:00 the model provider returned HTTP 429 insufficient_quota / credit_balance_exhausted. Leon subsequently
confirmed a top-up, and the new validation above succeeded with the same gpt-5.6-terra reviewer and gpt-5.6-sol selector.
The earlier failed progress-proof-v2 versus hybrid-v3 record remains historical evidence of that failure; it was not
rewritten or rerun by the observability validation. Account balance was not independently measured.

## Who owns what right now

- Codex: W&B Weave trace readability (semantic span names and hierarchy). Nobody else edits tracing structure.
  Note: tests/unit/test_director.py and test_api_runs.py monkeypatch `director.run_audit`; a refactor that stops
  director.py calling `run_audit` must update those hooks, or the cancel test silently stops cancelling.
- Frontend agent (Claude subagent): `apps/web` redesign (upload or URL, judge, timeline findings, improve, A vs C,
  A/B-to-C mode) against the real API. It does not edit `directorloop/`.
- Claude main session: backend runtime and A/B-to-C (`directorloop/runtime`, `directorloop/compare`, API, CLI).

## Commands

```
.venv/bin/python -m pytest -p no:warnings        # 86 tests pass at 18dab05
.venv/bin/ruff check directorloop tests scripts   # clean
.venv/bin/directorloop run VIDEO --objective TEXT [--constraint TEXT] [--budget N] [--focus X] [--allow-edit TYPE]
.venv/bin/directorloop abc A.mp4 B.mp4 --objective TEXT [--constraint TEXT] [--budget N] [--max-calls N] [--deadline S]
.venv/bin/directorloop show RUN_ID_OR_ABC_ID
.venv/bin/directorloop serve                      # API + web app on 127.0.0.1:8787
```

## API (loopback; optional bearer token DL_LOCAL_AUTH_TOKEN)

- GET /api/health (features: runs, abc, url_ingest true; classify false)
- POST /api/ingest/url (direct media links; Instagram/TikTok/YouTube/X via yt-dlp; SSRF, port, size and time limits); linked videos need owner_confirms_rights to be edited
- POST /api/audits (Judge Video, audit only), GET /api/audits?video_id=
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
- abc_1a0979874ab_05c246c4, Smoot police-payoff-v5 (A, 7.5 s) vs relaxed-read-v6 (B, 10.4 s), identical words, rubric v2:
  A preferred overall in both orders (opening interest, information progression, pacing). The selector declined every C
  option (swaps would weaken A's opening or lengthen its ending; needed: phrase-level retiming, which no executable edit
  offers). Final: keep A. 21 calls, 87 s.
- abc_1a09799dc01_f3bd31aa, Smoot progress-proof-v2 vs hybrid-v3: failed during the audits when OpenAI credits ran out.
  The crash left the record running; it was closed by hand (annotated) and the runtime now turns provider failures into
  explicit failed records (613918c).

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

1. Resolve the model-credit blocker (Leon), then re-run progress-proof-v2 vs hybrid-v3 and pick the demo pair from real results
   without tuning the evaluator. Across 3 completed A/B-to-C runs no C has been accepted yet; that is the honest state.
2. Content classification endpoint (creative type, platform guess, speech and captions present) for the new frontend flow.
3. Wire the frontend to /api/abc and /api/runs once the redesign lands; end-to-end UI test.
