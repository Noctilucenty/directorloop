# DirectorLoop

**An AI video editor that has to justify its changes.**

DirectorLoop reviews a short video in chronological sections, identifies where attention may weaken, explains the evidence, and tests a small edit against the original. Unsupported findings stay uncertain; an edit can lose, and the original can remain the winner.

[Live demo](https://directorloop-demo.onrender.com) · [Download source ZIP](https://github.com/Noctilucenty/directorloop/archive/refs/heads/main.zip) · [Source](https://github.com/Noctilucenty/directorloop)

## Demo in 60 seconds

1. Open the live demo and choose a short video. Analysis shows progress and restores its saved result after refresh.
2. Read the three score explanations, what works, and the weaker moments on the attention chart.
3. Open **Experiments** to play the saved original and edited variants. The recorded decision keeps the original when improvement is inconclusive.
4. Follow the Weave links below to inspect actual execution and the failed-to-fixed regression example.

The public upload flow performs a review. **Before/after preview** plays two supplied videos; it does not automatically edit them. The operator application contains the full edit-and-rejudge workflow.

## See the evidence

- [Recorded edit/rejudge session](https://wandb.ai/leondragon3798-curio/directorloop/r/call/01a097b6-ace3-72b0-9b67-b6b92bfd89e0).
- [Weave regression: 54/54 evidence-rule cases pass](https://wandb.ai/leondragon3798-curio/directorloop/r/call/01a09c4d-ff0b-7ebd-9ecd-b4658090c74c).
- [Preserved earlier transport failure: 25/54](https://wandb.ai/leondragon3798-curio/directorloop/r/call/01a09c4d-4a0c-7900-a369-226e262656e8).
- [Production evidence-health check](https://wandb.ai/leondragon3798-curio/directorloop/r/call/01a09c4f-7f28-7530-9d2d-c1521f1cb8a6).

Weave links may require project access. The public demo also provides a recorded experiment. Live analysis depends on the operator engine being available.

## How we used the event tools

| Tool | Role and verified scope |
| --- | --- |
| W&B Inference | The screening lane uses Qwen vision judgments over sampled frames and transcript prefixes, with bounded request accounting. |
| W&B Weave | Connected traces expose actual review, diagnosis, editing, comparison, errors, timing and decisions. A native Evaluation replays 54 frozen cases, including real failures and controls. |
| W&B ARIA | The recorded guardrail experiment preserved 39 original cases and improved four adversarial mechanical cases from 1/4 to 4/4. Those cases are retained in the regression dataset. |
| CoreWeave GPU + marimo/Molab | An interactive synthetic Qwen2.5-VL GPU smoke test verified the interface on an RTX PRO 6000. It is separate from the production screening service and does not establish real-video accuracy. |
| TypeSafe | Optional structured judgment integration for narrow evidence questions. It is not a prerequisite for the default screening path. |

Finished reports can receive deterministic evidence-health scorer feedback in Weave. Native Agent Signals is available in the account but was not configured at the last verified check. We distinguish that from our own scorer feedback.

## Fast local preview

Requires Node.js 22.12+ (validated with Node.js 24). From the downloaded repository:

```sh
cd apps/demo
npm ci
npm run build
npm run preview
```

Open `http://127.0.0.1:5188`. Recorded evidence is available without provider keys. Fresh analysis needs a configured backend; the ZIP does not contain production secrets or the private video corpus.

## Run the full local application

Requires Python 3.12+, Node.js, and FFmpeg/FFprobe on your PATH.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cp .env.example .env
npm --prefix apps/web ci
npm --prefix apps/web run build
.venv/bin/directorloop serve
```

Open `http://127.0.0.1:8787`. Configure your own provider credentials privately in `.env` before starting fresh model work. Screening is opt-in through `DL_SCREENING_ENABLED`; it has a separate finite dollar/attempt guard. Keys, production state and uploaded videos are intentionally excluded from Git.

## Repeat the regression

```sh
.venv/bin/pytest tests/unit/test_weave_trace_regression.py tests/unit/test_weave_online_checks.py
```

This suite has 66 checks, including all 54 frozen evaluation cases. During development, a bounded watcher runs it initially and after relevant source changes:

```sh
.venv/bin/python -m scripts.watch_trace_regressions --duration-seconds 3600 --log-dir ./regression-watch-session
```

The watcher blocks network access and retains failed results. It runs locally for at most one hour. GitHub Actions also runs the frozen evidence regression and both frontend test/build suites on pushes and pull requests; see `.github/workflows/regression.yml`. See `docs/WEAVE_REGRESSION_AND_ONLINE_CHECKS.md` for native evaluation publication and post-run feedback commands.

## What the scores mean

Attention strength and creative/sharing scores are evidence-linked model ratings, not measured audience retention, predicted views, or probabilities of going viral. Sampled frames and ASR do not constitute continuous native video/audio understanding. Mechanical validation can catch unsupported references without proving the model's interpretation is true.

Built for CoreWeave Hacks, September 12–13, 2026.
