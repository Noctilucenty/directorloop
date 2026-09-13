# Weave evaluation and post-run evidence checks

Verified September 13, 2026. No new model inference was used for this work.

## What is live

- [Native Weave evaluation: 54/54 frozen cases pass](https://wandb.ai/leondragon3798-curio/directorloop/r/call/01a09c4d-ff0b-7ebd-9ecd-b4658090c74c).
- [Dataset: directorloop-failed-trace-regression-v1](https://wandb.ai/leondragon3798-curio/directorloop/weave/objects/directorloop-failed-trace-regression-v1/versions/wEldk0qIjWXdMmjxwGEBpnTgMf3hxYum5N1xxF6ZbmE).
- [Weave Evaluations](https://wandb.ai/leondragon3798-curio/directorloop/weave/evaluations) now has a real Evaluation run, rather than only an artifact or unit-test log.
- [One production evidence-health score](https://wandb.ai/leondragon3798-curio/directorloop/r/call/01a09c4f-7f28-7530-9d2d-c1521f1cb8a6) is attached as scorer feedback to the [four-section screening trace](https://wandb.ai/leondragon3798-curio/directorloop/r/call/01a09c3f-1dae-75e0-b40d-cc51055cc77b). It has complete time coverage and no missing attention or content-score evidence under the current mechanical rules.

The dataset preserves 39 original ARIA cases, four ARIA adversarial cases, and 11 examples from actual saved screening failures and controls. Every row has an immutable input hash and label/provenance hash. Real trace IDs were checked against completed Weave calls before publication. The 11 newer cases cover inference-only references, caption/ASR confusion, premature payoff criticism, unsupported broad-audience assumptions, and valid controls.

These are development regression cases. A pass proves that the recorded mechanical admission rule behaves as specified. It does not establish semantic correctness, measured retention, predicted views, or a held-out model-quality score.

The [first native replay](https://wandb.ai/leondragon3798-curio/directorloop/r/call/01a09c4d-4a0c-7900-a369-226e262656e8) remains visible at 25/54. It found an integration bug: Weave boxed integer timestamps, while the historical validator intentionally required JSON integers. The replay boundary now restores JSON types, including the boolean/integer distinction. No fixture labels or original video evidence changed. The corrected native run passes 54/54.

## Run the regression after changes

From the repository root:

```sh
.venv/bin/pytest tests/unit/test_weave_trace_regression.py tests/unit/test_weave_online_checks.py
```

These tests also run in the normal project `pytest` suite. They currently pass 66 tests, including all 54 frozen rows, immutable-label checks, transport-type guards, time-coverage checks, and sanitization. A bounded local watcher now runs them automatically after relevant source changes; no cloud CI trigger is claimed.

### Automatic local regression watcher

`scripts/watch_trace_regressions.py` checksums all Python files under `directorloop/` plus the regression scripts, fixture, and tests. It runs once immediately, then debounces edits for three seconds before rerunning the 66 focused tests. Changes during a test run trigger another run after the sources settle. File additions and deletions also count. A separate pytest plugin blocks network connections, provider credentials are removed from the child environment, each run has a 120-second timeout, and the watcher stops after at most one hour.

A development session on September 13 verified both the initial run and an automatic rerun after a source change: 66 tests passed each time. That operator-local process and its receipts are not part of this release. Start a new bounded session with a new log directory to preserve earlier failures:

```sh
.venv/bin/python -m scripts.watch_trace_regressions --duration-seconds 3600 --log-dir ./data/regression-watch/session-01
```

`events.jsonl` records each result and `run-NNNN.log` preserves redacted pytest output. The watcher writes its PID to `watcher.pid` and stops after one hour; stop it earlier using that PID if needed.

To publish a new native evaluation after a reviewed engine change:

```sh
.venv/bin/python -m scripts.weave_trace_regression --publish --receipt ./data/weave-evaluation-regression-receipt.json
```

Publication performs deterministic replay and W&B logging only. The command returns nonzero if either local or native frozen-label matching fails. Existing failed runs remain available for comparison.

## Post-run online checks

The independent checker reads saved terminal reports, reapplies current citation/boundary admission, and emits evidence-health feedback. It flags incomplete time coverage, blocked attention sections, invalid evidence, and unavailable content scores. It never repairs videos, starts inference, changes the original report, or raises budgets.

```sh
.venv/bin/python -m scripts.weave_online_checks --publish --max-reports 3 --receipt-dir ./data/weave-online-checks
```

Each invocation examines at most three new or changed terminal reports. Receipts prevent repeat publication for an unchanged report version in ordinary operation; a crash between remote scoring and receipt persistence may require checking for duplicate feedback. Omit `--publish` for offline inspection. The command can be invoked by an operator scheduler. Scheduling state is managed outside this repository; downloading this code does not activate recurring checks.

The operator retains the original receipts privately. New commands above write under ignored `data/`:

- `weave-evaluation-regression-receipt.json`
- `weave-evaluation-regression-first-transport-failure.json`
- `weave-online-checks/`
- `weave-online-checks-remote-verification.json`

## Native Signals status

This is **Weave scorer feedback**, not native Agent Signals. Native Signals is available at [Agents → Signals](https://wandb.ai/leondragon3798-curio/directorloop/weave/agents/signals). The operator verified the page in Chrome: score volume was zero and no agent signals were configured. Native Signals is available but not configured at that checkpoint.

The current [W&B Signals documentation](https://docs.wandb.ai/weave/guides/tracking/view-agent-signals) places Signals under Agents and applies them to agent turns. To configure it, open Agents → Signals, inspect available presets, target actual DirectorLoop turns, and verify a real completed turn receives a rating/tag before claiming it is active. The creation flow describes an online LLM judge. Provider-backed preset costs need separate review; no LLM judge was enabled here.

The implementation uses the documented [native Evaluation workflow](https://docs.wandb.ai/weave/tutorial-eval) and [scorer feedback API](https://docs.wandb.ai/weave/guides/evaluation/scorers). Historical Monitor instructions describe a previous approach and should not be presented as the current Signals setup.

## Twenty-second demo explanation

“When DirectorLoop makes a bad judgment, we preserve that failure and add a regression case. Weave shows the same 54 saved cases rerun after a fix, including the failures and the passing controls. New finished reviews also receive evidence-health feedback. These checks verify our evidence rules; they do not pretend to measure human retention.”
