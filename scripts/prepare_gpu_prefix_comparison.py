#!/usr/bin/env python3
"""Freeze six original screening inputs for an offline-prepared GPU comparison.

No network, inference, outcome labels, or media resampling. Input and recorded
baseline are separate files; only gpu-prefix-inputs.json goes to a notebook.
"""
from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import json
from pathlib import Path

V1_PROMPT_SHA = "b37c4c1b551512fdb460f41f1549bd97cf9b34b4eaf0eab9969cac1910871b22"
V1_SCHEMA_SHA = "5f340c5b0f0085133be6bbefd586f4126b6390602fdc8f3a2cd399d1411d62c2"
V1_SYSTEM_SHA = "fd56509284facf046b6b347765eea6a31866d8dffb43a2d9dc5fb0b5159bdc3c"
NEW_LINES = ("Each observation must", "visible_fact and caption_claim use", "An attention label needs")


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def literal_constant(path: Path, name: str):
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError(f"Missing constant {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort", type=Path, required=True)
    parser.add_argument("--reports", type=Path, required=True, help="Directory with heldout-N-report.json")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    import sys
    sys.path.insert(0, str(root))
    from directorloop.screening.models import ScreenJudgment

    current = literal_constant(root / "directorloop/screening/runner.py", "SCREENING_INSTRUCTION")
    instruction = "\n".join(line for line in current.split("\n") if not line.startswith(NEW_LINES))
    system = literal_constant(root / "directorloop/providers/base.py", "VIEWER_SYSTEM_PROMPT")
    schema = ScreenJudgment.model_json_schema()
    assert digest(instruction.encode()) == V1_PROMPT_SHA, "Historical prompt does not match; stop rather than silently use v2"
    assert digest(system.encode()) == V1_SYSTEM_SHA
    assert digest(canonical(schema)) == V1_SCHEMA_SHA
    manifest = json.loads((args.cohort / "manifest.json").read_text())
    cases, baseline = [], []
    for clip_index in (1, 2, 3):
        api_path = args.reports / f"heldout-{clip_index}-report.json"
        api_raw = api_path.read_bytes()
        api = json.loads(api_raw)
        for end in (2000, 4000):
            case_id = f"heldout-{clip_index}-prefix-{end:06}"
            entry = next(e for e in manifest["cases"] if e["case_id"] == case_id)
            case_path = args.cohort / entry["path"]
            assert digest(case_path.read_bytes()) == entry["sha256"]
            case = json.loads(case_path.read_text())
            window = next(w for w in api["windows"] if w["end_ms"] == end)
            frame_times = [frame["requested_timestamp_ms"] for frame in case["frames"]]
            full_instruction = case["legacy_payload_instruction"] + instruction.format(frame_times=json.dumps(frame_times))
            assert digest(full_instruction.encode()) == window["instruction_sha256"]
            assert " ".join(word["text"] for word in case["asr_words"]) == window["prefix_asr_text"]
            assert case["source_sha256"] == api["artifact_hash"]
            frames = []
            for frame, actual in zip(case["frames"], window["evidence_frames"], strict=True):
                raw = (args.cohort / frame["path"]).read_bytes()
                assert digest(raw) == frame["sha256"] == actual["sha256"]
                assert frame["requested_timestamp_ms"] < end
                frames.append({"t_ms": frame["requested_timestamp_ms"], "sha256": frame["sha256"],
                               "jpeg_base64": base64.b64encode(raw).decode(), "width": frame["width"], "height": frame["height"]})
            cases.append({"case_id": case_id, "prefix_end_ms": end, "source_sha256": case["source_sha256"],
                          "frames": frames, "asr_words": case["asr_words"], "instruction": full_instruction,
                          "instruction_sha256": window["instruction_sha256"], "baseline_payload_sha256": window["payload_sha256"]})
            baseline.append({"case_id": case_id, "report_id": api["id"], "api_report_sha256": digest(api_raw),
                             "model": api["protocol"]["model"], "provider": api["protocol"]["provider"],
                             "protocol_fingerprint": api["protocol_fingerprint"], "instruction_sha256": window["instruction_sha256"],
                             "judgment": window["judgment"], "raw_output": window.get("raw_output"),
                             "status": window["status"], "validation_issues": window["validation_issues"],
                             "latency_ms": window["latency_ms"], "input_tokens": window["input_tokens"],
                             "output_tokens": window["output_tokens"], "weave_url": window.get("weave_url") or api.get("weave_url")})
    bundle = {"version": "directorloop-gpu-six-prefix-inputs-v1", "system_prompt": system, "system_sha256": V1_SYSTEM_SHA,
              "schema": schema, "schema_sha256": V1_SCHEMA_SHA, "original_screening_instruction_sha256": V1_PROMPT_SHA,
              "cases": cases, "data_scope": "development comparison; previously reviewed inputs; no ground-truth labels or outcomes"}
    args.out.mkdir(parents=True, exist_ok=True)
    outputs = {"gpu-prefix-inputs.json": bundle, "wandb-recorded-baseline.json": {"mode": "recorded", "cases": baseline}}
    for name, data in outputs.items():
        with (args.out / name).open("x") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        (args.out / name).chmod(0o600)
    receipt = {"status": "prepared_not_run", "cases": 6, "jpeg_frames": sum(len(c["frames"]) for c in cases),
               "inference_requests": 0, "gpu_generations": 0, "uploaded_to_molab": False,
               "source_cohort_manifest_sha256": digest((args.cohort / "manifest.json").read_bytes()),
               "files": {name: {"sha256": digest((args.out / name).read_bytes()), "bytes": (args.out / name).stat().st_size}
                         for name in outputs},
               "privacy": "Only the input bundle may enter an authorized private runtime. Recorded baseline and labels remain local. Molab notebooks/data are public-unlisted; no real-media upload authorized yet."}
    with (args.out / "preparation-receipt.json").open("x") as handle:
        json.dump(receipt, handle, indent=2)
        handle.write("\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
