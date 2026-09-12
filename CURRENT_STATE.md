# DirectorLoop: current state

Handoff file for whoever continues this build (Claude Code or Codex). Only verified facts go here.
Update it at every milestone. Newest entry wins over any older prompt or plan.

Last updated: 2026-09-12 13:00 PDT

## Where things are

- Repo: `/Users/leon/Desktop/dev/CoreWeave` (git, branch `main`, remote `https://github.com/Noctilucenty/directorloop`, PUBLIC)
- Python: `.venv` (uv, Python 3.12). Web client: `apps/web` (Vite + React + TypeScript).
- Secrets: repo `.env` (gitignored, mode 600). Keys present: OPENAI_API_KEY, GEMINI_API_KEY (out of credit),
  ELEVENLABS_API_KEY, WANDB_API_KEY. Never print values.
- Local data (gitignored): `data/` (renders by content hash, caches, genomes, project state, policy store).
- Specs: `docs/SPEC.md` (original build spec). The product pivot (UGC creative research agent) is the
  locked direction; see `/Users/leon/Downloads/DirectorLoop_Claude_Midproject_Pivot_Prompt.txt`.

## Product direction (locked)

Self-improving UGC creative research agent: video -> CreativeGenome -> weak region -> reference patterns
and prior policy evidence -> competing hypotheses -> controlled typed mutations -> real rendered variants
-> same frozen evaluation -> honest decision -> policy update -> the next video's first experiment changes.
The earlier comprehension-repair loop is the evaluation/mutation engine underneath.

## Services and start commands

No long-running services exist yet. There is no API server and no jobs worker yet.

```
.venv/bin/python -m pytest                       # all tests (29 passing at last run)
.venv/bin/ruff check directorloop tests scripts  # lint
.venv/bin/python scripts/run_slice.py packs/pack_a_stand_demo --fresh   # fixture repair loop end to end
.venv/bin/python scripts/make_pack_a.py          # regenerate fixture pack A (reuses narration)
cd apps/web && npm run dev                       # React client against the MOCK API only
```

## Implemented and verified

| Area | Status | Evidence |
|---|---|---|
| Typed domain contracts (brief, truth, assets, edit plan ops, evaluation, findings, repair, policy, decision) | working | unit tests |
| Creative domain models (genome, retention signal, reference corpus, hypotheses, mutations, experiments, fitness, pairwise review) | defined | `directorloop/domain/creative.py`; used by genome and mutation code |
| FFmpeg renderer (argument arrays, atomic publish by sha256, captions as Pillow overlays, join-aware audio fades) | working | integration tests; real renders in `data/renders/` |
| Plan validator (asset ownership, bounds, duration, crop upscale limit, captions, protected constraints) | working | tests |
| Model comprehension probes (OpenAI gpt-5.6-terra, 8 frames at 512 px + whisper transcript of the rendered audio, one question per call) | working | fixture slice run |
| No-media leakage control | working | fixture slice run |
| Mechanical + constraint checks | working | tests |
| Acceptance gates incl. text-substitution guard (a caption stating a visual answer does not count) | working | `tests/unit/test_engine_integrity.py` |
| Fixture repair loop (`run_iteration`) with Weave ops | working | run at 12:41: finding -> repair -> render -> re-test -> decision in 17.6 s |
| Weave tracing to `leondragon3798-curio/directorloop` | working | example call: https://wandb.ai/leondragon3798-curio/directorloop/r/call/01a09723-5dfc-737a-b0dd-4a451fb9185d |
| W&B Inference smoke tests (Kimi-K2.6, DeepSeek-V4-Pro planners; Qwen3.8-27B, Gemma-4-31B vision probes) | verified supported | 2026-09-12 smoke tests; not yet used in the loop |
| CreativeGenome extraction (scene cuts, motion curve, audio energy, token-timed sentence beats snapped to pauses, one vision-model labeling call for roles/hook/open loops) | working on real Curio shorts | AP-TIPPE-V4 (5 beats, 8.5 s), AP-LAZE-NARRATED-01 (4 beats, 6.9 s); tests |
| Mutation engine (identity plan per beat; PROOF_EARLIER, PAYOFF_EARLIER, RESULT_FIRST, CONTEXT_COMPRESSION, REMOVE_REDUNDANT_BEAT, PATTERN_INTERRUPT punch-in, SHORTEN_SHOT dead-air trim) | working on real Curio shorts | 5 and 6 arms rendered in parallel in 4.0 s and 6.8 s; durations match plans within one frame; join discontinuity under 0.4 of normal; burned-in captions move with their beat (frames checked); tests |
| React judge client | UI only, MOCK API | `apps/web`, builds and type-checks; not wired to a backend |
| Fixture pack A v3 (pencil cup hides the mechanism in wide and result shots) | regression fixture | sentinel-colour visibility test |

## Not built yet (in priority order)

1. RetentionInvestigator (genome + optional retention signal -> competing hypotheses with evidence and counterevidence)
2. Reference corpus: builder in progress (`scripts/build_curio_corpus.py`, output `data/corpus/curio_references.json`), then genome per reference + pattern extraction
3. ExperimentDesigner + scoped CreativePolicy store (ranking: detection evidence + reference prior + experiment evidence)
4. CreativeFitness (frozen per-video suite, hook clip probes, model pairwise continue-watching preference with order swap, structural timings)
5. Experiment runner with full Weave lineage, persistence, policy update
6. Video A experiment, then unseen Video B transfer (no memory vs learned policy)
7. Blinded human review route + QR (separate minimal server; only review routes exposed)
8. API server + jobs + wiring the React client to it; experiment/evolution UI
9. Optional: Molab GPU benchmark notebook, ARIA analysis, marimo lab

## Known limitations found by testing

- Pack A v3 slice run: the model viewer passed "which part moves" on a baseline where no tab is visible anywhere,
  because one question's wording revealed another's answer when all questions shared a call. Fixed: one question per call.
- The same run promoted a candidate whose only fix was a caption stating the visual answer. Fixed: text-substitution gate.
- Beat role labels from the vision model vary between labeling runs (one of five roles changed between two runs on the
  same video). Genomes are cached per artifact hash, so a single experiment is internally consistent.
- Gemini key: prepaid credits depleted (HTTP 429); OpenAI is the working probe provider.

## Reference data (read-only sources, owned by Leon)

- Finished Curio shorts: `/Users/leon/Desktop/dev/Curio-Automation/data/productions/<TOPIC>/<slug>-custom-captioned.mp4`
- Instagram per-reel metrics: `/Users/leon/Desktop/dev/Curio-Automation/data/viral-intelligence/ig-insights-2026-0{7-31,8-01,8-02}.json`
- Facebook per-second retention curves (8 posts): `.../viral-intelligence/fb-retention-curves-2026-07-30.json`
- Never open `.../graph-pulls/2026-07-30/page-direct.json` or `reels.json` (contain access tokens).
- Metrics stay in gitignored `data/`; the public repo gets code and formats only.

## Next concrete task

Implement the RetentionInvestigator and ExperimentDesigner with the scoped CreativePolicy, test them, then
CreativeFitness and the experiment runner, then run the Video A experiment.
