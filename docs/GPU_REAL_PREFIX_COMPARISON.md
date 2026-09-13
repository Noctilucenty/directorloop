# Real-prefix GPU comparison checkpoint

Status: **inputs prepared and verified; no real-media GPU run performed.** Work paused at the coordinator's request to free an agent for the authorized Render deployment. The dedicated real-media notebook runner remains to be adapted from `notebooks/directorloop_gpu_screen.py`; that existing notebook still performs only its synthetic test.

## Prepared kit

The prepared real-media kit is an operator-local artifact and is not included in this source release. Rebuild it from your own inputs with the script below.

- `gpu-prefix-inputs.json`: six 2s/4s prefixes, 42 exact JPEGs, prefix-bounded ASR, the original v1 prompt and unchanged schema. It contains no source filenames, platform outcomes, independent labels or W&B predictions.
- `wandb-recorded-baseline.json`: corresponding six previously recorded W&B judgments, validation issues, token/latency measurements and trace links. Keep this file local and outside model inputs.
- `preparation-receipt.json`: hashes, counts and explicit unrun/unuploaded state.

Rebuilder: `scripts/prepare_gpu_prefix_comparison.py`. Run with the project's Python environment and `--cohort`, `--reports`, `--out`; it makes no network or inference requests and uses exclusive writes. It verifies the original instruction hash, schema/system hashes, all six full instruction hashes against recorded W&B, every JPEG hash and ASR text. All checks passed for this kit.

The existing three-video cohort is now a **development comparison**, not untouched held-out evaluation: it was reviewed and informed later screening work. The GPU must receive the frozen v1 prompt, not the newer v2 prompt, for the closest feasible comparison. Raw results must remain unchanged. A valid JSON result or cited timestamp is not semantic truth.

## Execution requirements for the remaining runner

Use `Qwen/Qwen2.5-VL-3B-Instruct` at revision `66285546d2b821cf421d4f5eb2576359d3770cd3`, `trust_remote_code=False`, safetensors, BF16 and SDPA. Its supported multi-image interface is documented in the [official model card](https://huggingface.co/Qwen/Qwen2.5-VL-3B-Instruct). Preserve the exact input JPEG bytes and record processor resizing/grid dimensions separately.

Run through an interactive notebook only. The notebook must remain idle on opening, require an explicit run action and verified included GPU allowance, check actual device/VRAM and a CUDA kernel before model download, cap at six total generations without retry, save partial receipts before each dispatch and after each result, and stop on infrastructure or malformed-output failure. Use a bounded watchdog and distinguish loading, preprocessing, prefill/generation and total wall time. No W&B/OpenAI inference requests, tunnels, remote serving or API keys.

For schema comparison, Transformers generation needs an explicit schema prompt or constrained decoder; W&B previously used a response-format schema. Record this difference and differing decoding settings. Do not present measured latency as an isolated hardware comparison when request format or decoding differs. Save all invalid outputs and abstentions. Compare actual semantics against the independent observations, not W&B answers as ground truth.

## Why no real-media upload occurred

Chrome connected successfully in the background on September 13. The account dashboard showed zero running notebooks; the dedicated synthetic notebook retained GPU configuration. No allocation was started or changed.

Molab's notebooks are public but unlisted. Its current storage documentation says file-browser uploads persist and notebook forks carry attached data. The notebook settings page showed no private visibility control. Therefore the real-media bundle was kept local. Ephemeral widget upload privacy has not been established, and no public upload permission was requested during this checkpoint. [Sharing](https://marimo.io/features/vs-colab-alternative), [storage](https://marimo.io/blog/seamless-storage-in-molab).

Molab permits interactive notebook computation and restricts noninteractive jobs, remote SSH control and exposing compute to outside traffic. The included GPU is an experimental notebook resource, not the production app's hosted model server. [Restrictions](https://marimo.io/pages/molab/restrictions).

## Verified sponsor evidence already available

The earlier isolated Molab **synthetic** notebook ran Qwen2.5-VL-3B on an actual RTX PRO 6000 Blackwell Server Edition. Measured checkpoint load was 15788ms, first generation 1508ms and warm generation 833ms; both used 626 input/48 output tokens. Peak allocation was 7.216 GiB. Two outputs passed the synthetic interface schema. No paid inference calls or API secrets were used; runtime shutdown was confirmed. The control ASR contained the answers, so this is not independent visual accuracy or real-video quality evidence.

Operator-local receipt: `gpu-smoke-receipt.json` (not included in the source release).
Provenance: `gpu-smoke-provenance.json` beside it. Shutdown image: `gpu-shutdown-confirmed.png`.
Notebook: [DirectorLoop GPU screening smoke](https://molab.marimo.io/notebooks/nb_cPf3k8sgetGjnpqJ3nrM8o).

For real footage, W&B completed three screening sessions with nine model calls. The independent review found semantic errors despite eight structurally clean prefixes. The operator retains `predictions-frozen-all-three.json` and `QUALITY_REVIEW.json`; these private development records are not included in the source release. That evidence supports real sponsor-tool use and traceability, not proven retention gains or automatic edit safety.
