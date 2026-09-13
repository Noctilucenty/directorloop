# DirectorLoop GPU screening

The new [marimo notebook](../notebooks/directorloop_gpu_screen.py) is an **interactive screening smoke test**. It does not replace the production evaluator, start on opening, use paid inference, or expose a server.

## Route

| Work | Route |
| --- | --- |
| Interactive open-model experiments | Molab notebook |
| Background screening service | W&B hosted inference, or separately provisioned serving compute |
| Final original-versus-edit decision | Same frozen evaluator on both versions |
| Evidence | Weave hierarchy plus downloaded receipt |

Molab advertises free RTX Pro 6000 Blackwell GPUs, 96 GB VRAM, and sessions up to 12 hours. This is an offering, not a measurement of the device assigned to this account. [Official offering](https://marimo.io/features/vs-colab-alternative)

Molab's current restrictions exclude exposing compute to outside traffic, remote SSH control, and noninteractive jobs. Therefore this notebook is an interactive demonstration, not an unattended batch worker or the app's remote GPU API. Notebooks are public but unlisted by default; this file contains only deterministic synthetic data and no credentials. [Restrictions](https://marimo.io/pages/molab/restrictions)

## Fixed experiment

| Setting | Value |
| --- | --- |
| Model | `Qwen/Qwen2.5-VL-3B-Instruct` |
| Pinned revision | `66285546d2b821cf421d4f5eb2576359d3770cd3` |
| Dtype / attention | BF16 / SDPA; no third-party attention compilation |
| Input | Two synthetic JPEGs at 0 and 1000 ms; ASR ends at 1800 ms |
| Prefix | 0–2000 ms only |
| Output | Strict enum JSON; 256 tokens maximum |
| Repetitions | Two, cold then warm; no automatic repair/retry |
| Input cap | 2048 text/visual tokens; 256 visual tokens per image maximum |
| Time bound | 120 seconds generation bound; 900 seconds child-process watchdog |
| Preflight | Actual GPU name, VRAM, CUDA build, kernel execution; minimum 12 GiB free VRAM and disk |

The model repository contains approximately 3.75 billion BF16 parameters (roughly 7.5 GB of weights, excluding runtime memory). Choosing this modest checkpoint leaves room for runtime overhead; fit is still checked before download. Its official interface supports image input and structured responses. Exact output is validated locally; malformed JSON stops the smoke instead of creating another model call. [Model card](https://huggingface.co/Qwen/Qwen2.5-VL-3B-Instruct)

The notebook requests Torch 2.7.1 and torchvision 0.22.1 from the official CUDA 12.8 wheel index. Blackwell support began in Torch 2.7 with CUDA 12.8. **Molab's managed environment retained newer Torch 2.11.0 / torchvision 0.26.0 / CUDA 13.0 in the measured run**, rather than honoring those two requested pins. This difference is preserved in the receipt; reproduce the measured configuration explicitly before comparing benchmarks. The first real CUDA matrix multiply succeeded before checkpoint loading. [PyTorch Blackwell release](https://pytorch.org/blog/pytorch-2-7/)

## Run

1. In Molab's dashboard, choose **More notebook creation options → Import** and upload `notebooks/directorloop_gpu_screen.py` under a new DirectorLoop name. Preserve the existing Curio benchmark and running CPU notebook.
2. Select the included GPU on this new notebook only. If a new charge, billing form, or unavailable allocation appears, stop and record that state. The offering does not establish remaining account allowance or active hardware.
3. Let the notebook's declared dependencies resolve. The source pins the official CUDA wheel index. If the importer does not honor that configuration, install the same pinned requirements in the notebook's package interface; do not work around the notebook UI with SSH or tunnels.
4. Confirm included allowance, then press **Run smoke**. Keep the notebook interactive while it runs. It emits no OpenAI API calls and makes at most two local model generations.
5. Download the receipt. A pass means the multimodal request returned the expected schema and control values. The supplied ASR names the object, color, and direction, so this case does not isolate visual understanding. It does not establish video-audit quality, retention accuracy, or monetary savings.
6. Shut down this new notebook's runtime after downloading evidence. The child releases CUDA allocations on exit, but process cleanup does **not** shut down the Molab runtime. Do not stop other notebooks.

The notebook can be edited locally using `uvx marimo edit notebooks/directorloop_gpu_screen.py --sandbox`. Opening it resolves dependencies but does not load a model. Keep its GPU inference interactive on Molab.

## Weave and persistence

Weave is optional in the public notebook. It uses `WANDB_API_KEY` only from the runtime's existing private environment; never paste a key into a cell or notebook source. If no private secret mechanism is already configured, leave the checkbox off and import the downloaded receipt into the local authenticated project's tracing workflow later.

When enabled, the hierarchy is `directorloop.gpu_screen.session → hardware → load_model → cold_inference → warm_inference`. Only hardware metadata, hashes, bounded token/latency measurements, and enum-validated synthetic judgments are returned. Code capture is disabled. Local flush is labeled separately from remote trace verification. Failed stages preserve a fixed stage and exception class, not raw stack traces or credentials.

Receipts are written to a unique `directorloop_gpu_receipts/gpu_smoke_<id>/receipt.json` and exposed by **Save receipt**. Molab warns that ordinary runtime writes are not durable unless uploaded through its file browser or placed in persistent cache. Download the receipt before shutdown. The model's Hugging Face cache may not survive runtime recycling; record model-load time separately from warm inference.

## Measured Molab result — September 13, 2026 UTC

New isolated notebook: [DirectorLoop GPU screening smoke](https://molab.marimo.io/notebooks/nb_cPf3k8sgetGjnpqJ3nrM8o).

| Measurement | Actual result |
| --- | --- |
| Run | `gpu_smoke_7787b6f36c7a4d6ba7c1dff51b9b129e` |
| Device | NVIDIA RTX PRO 6000 Blackwell Server Edition; compute capability 12.0 |
| Available device memory | 101,975,851,008 bytes total; 101,390,548,992 bytes free at preflight |
| Kernel preflight | Passed |
| Checkpoint load | 15,788 ms; includes cache/network work, not a download-only measurement |
| First inference | 1,508 ms; 626 input and 48 output tokens |
| Warm inference | 833 ms; 626 input and 48 output tokens |
| Peak Torch allocation | 7,748,452,352 bytes / 7.216 GiB |
| Total measured worker time | 30,572 ms |
| Both outputs | `circle`, `red`, `right`, `asr_agrees=true`, evidence `[0,1000]`; strict JSON valid |
| Paid inference calls | 0 |
| Remote Weave | Disabled; no secrets entered in Molab |
| Completion | Child exit code 0; CUDA model references released |

Actual versions: Torch 2.11.0, torchvision 0.26.0, CUDA 13.0, transformers 4.51.3, accelerate 1.6.0, Pillow 12.3.0, Weave 0.53.9. Model and processor used the pinned model revision above.

The exact rendered JSON receipt was saved as the operator-local `gpu-smoke-receipt.json`; it is not included in this source release. Its native download event did not arrive through the browser extension; the plain JSON Evidence block was exported instead, without reconstructing or changing fields. `gpu-smoke-provenance.json` records the receipt hash and acquisition method. Screenshots are `gpu-smoke-complete.png` and `gpu-shutdown-confirmed.png` beside it.

**Shutdown confirmed:** after this notebook's Confirm Shutdown action, the dashboard's Running filter contained only the original `untitled-coral-pinniped` notebook (`nb_fxE8Ap2aaeAxcVKCohHVyx`). The new GPU notebook was absent. The old Curio benchmark and CPU notebook were not edited or stopped. Opening the new notebook again may start a new allocation.

The two samples are repeats of one synthetic case, not an independent quality evaluation. ASR exposes the control answer. Latency and hardware are measured; savings, real-video performance, and visual-only comprehension remain unmeasured. A locally imported Weave receipt must use `mode=recorded` and identify Molab as the source, rather than representing the GPU operations as fresh local execution.

## Local validation

- Local Python import, marimo structural check, and scoped Ruff check passed.
- Local JPEG hashes, prefix bounds, JSON-schema rejection cases, and default no-run gate passed.
- Missing GPU dependencies fail without any model generation or paid API call.
- Mocked watchdog termination and private receipt persistence checks passed; no process/model launched by that test.

Before adopting the model for screening, freeze a small real-video set and compare supported findings, abstentions, chronology, regression sensitivity, and latency against the final evaluator. A successful geometric smoke is only the first integration gate.
