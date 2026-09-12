# DirectorLoop: working agreements

This file is for every agent or engineer working in this repository. Read it before writing code.

## What this is

DirectorLoop is a hackathon build (CoreWeave Hacks, Agent Loops, September 12-13 2026). It takes a
short video plus a creative brief, evaluates whether the rendered video communicates what the brief
requires, diagnoses a supported failure, chooses the smallest justified typed edit, renders a new
version, re-evaluates under the same frozen suite, decides promote / reject / no-gain / needs-review,
and stores what the experiment taught it. The specification is `docs/SPEC.md` (a copy of
`DirectorLoop_Full_Prompt.txt`). The loop itself is the product.

## Layout

- `directorloop/domain/`   typed contracts (pydantic v2). Change these first, then everything else.
- `directorloop/media/`    ffprobe inspection, frame sampling, whisper.cpp transcription, caption
                           rendering (Pillow PNG overlays; this ffmpeg has no drawtext), FFmpeg
                           rendering from a validated EditPlan, plan validation.
- `directorloop/evals/`    mechanical checks, comprehension probes, scoring, leakage control, comparison.
- `directorloop/planning/` finding aggregation, routing table, planner, acceptance, policy memory.
- `directorloop/providers/` adapters: Gemini (native video), OpenAI-compatible (OpenAI, W&B Inference,
                           TypeSafe once verified), ElevenLabs / macOS `say` speech, capability registry.
- `directorloop/observability/` Weave ops and attributes, outbox, W&B experiment runs.
- `directorloop/loop/`     `run_iteration` orchestrator (spec section 20).
- `directorloop/jobs/`     SQLAlchemy models, durable queue with leases, worker.
- `directorloop/api/`      FastAPI app. Serves `apps/web/dist` when built.
- `apps/web/`              React + TypeScript + Vite client. Contract: `docs/API.md`.
- `packs/`                 demo packs. Media files are named by content hash and gitignored;
                           `scripts/make_pack_a.py` regenerates pack A deterministically.
- `notebooks/`             marimo notebooks (GPU benchmark for Molab, experiment lab).
- `tests/`                 unit, integration (real ffmpeg on tiny fixtures), security, evals.

## Rules that are not negotiable

1. Model output is typed edit operations (`domain/edit_plan.py`). Never shell strings, filter strings, URLs.
2. Probe inputs never contain: source truth, correct answers, claim ids, planner notes, filenames,
   generation prompts, version labels, or whether the candidate is expected to be better.
3. Three evidence levels stay separate: mechanical, model probe, human. Never one blended score.
   Model samples are "model comprehension probes", never "viewers" or "people".
4. Versions are immutable. Write renders to a temp path, verify, then atomically publish by hash.
5. Secrets never reach logs, traces, notebooks, the frontend or Git. Report presence and length only.
6. Do not display a target as a measurement. Every latency number in docs says measured or target,
   with sample size and configuration.
7. Cached preprocessing of immutable assets is fine. A cached evaluation presented as fresh is not.
   Every run carries `mode: fresh | cached | recorded`.
8. No emojis anywhere in deliverables. No decorative sponsor overlays on videos.
9. Integer milliseconds for all timing. Content hashes are sha256 hex.
10. Commit messages describe the change; no attribution trailers.

## Commands

```
.venv/bin/python -m pytest                 # tests
.venv/bin/ruff check directorloop tests    # lint
.venv/bin/python -m directorloop.cli --help
```
