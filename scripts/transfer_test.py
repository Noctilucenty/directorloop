"""Transfer test: does prior experiment evidence change the FIRST experiment on an unseen video, and is it better?

Protocol (docs/POLICY_EVIDENCE.md):
  1. rank candidates on video B with no memory and with the learned policy; save both BEFORE evaluating B
  2. evaluate every valid candidate on B once (policy recording off)
  3. for each mode: first mutation, its outcome on B, attempts until the first win in that order, calls and time spent

Usage: .venv/bin/python scripts/transfer_test.py VIDEO_B_PATH VIDEO_B_ID [--policy data/creative/policy.json]
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from directorloop.config import get_settings
from directorloop.creative.design import design_experiment
from directorloop.creative.experiment import run_creative_experiment
from directorloop.creative.genome import extract_genome
from directorloop.creative.investigate import investigate
from directorloop.creative.mutate import experiment_brief, identity_plan, source_manifest
from directorloop.creative.policy import CreativePolicyStore, load_policy
from directorloop.domain.creative import ReferenceCorpus
from directorloop.domain.ids import utc_now_iso
from directorloop.observability import flush, init_weave
from directorloop.observability.weave_ops import current_call_ref, traced
from directorloop.providers import build_providers


def rank(video_path: Path, video_id: str, providers: Any, data_dir: Path, policy: CreativePolicyStore, corpus: ReferenceCorpus | None, mode: str) -> list[dict[str, Any]]:
    g = extract_genome(video_path, providers.probe, data_dir / "creative" / "genomes")
    manifest = source_manifest(f"proj_{video_id}", video_path, g.artifact_hash)
    brief = experiment_brief(f"proj_{video_id}", g, "")
    design = design_experiment(hypotheses=investigate(g, corpus=corpus).hypotheses, genome=g, plan=identity_plan(g), manifest=manifest, brief=brief,
                               video_path=video_path, parent_version_id="v0", policy=policy, corpus=corpus, policy_mode=mode, max_arms=10)
    return [{"mutation": c.mutation.type.value, "score": c.ranking.score, "reason": c.ranking.reason, "family": c.hypothesis.family.value} for c in design.candidates]


def attempts_until_win(order: list[str], outcomes: dict[str, str]) -> int | None:
    for i, m in enumerate(order, 1):
        if outcomes.get(m) == "win":
            return i
    return None


@traced("next_video_policy_effect", kind="agent")
def next_video_policy_effect(video_path: Path, video_id: str, policy_path: Path, corpus_path: Path, trials: int) -> dict[str, Any]:
    s = get_settings()
    providers = build_providers(s)
    learned = load_policy(policy_path)
    corpus = ReferenceCorpus.model_validate_json(corpus_path.read_text(encoding="utf-8")) if corpus_path.exists() else None
    empty = CreativePolicyStore()
    rankings = {
        "none": rank(video_path, video_id, providers, s.data_dir, empty, corpus, "none"),
        "learned": rank(video_path, video_id, providers, s.data_dir, learned, corpus, "learned"),
    }
    out_dir = s.data_dir / "creative" / "transfer"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    pre = out_dir / f"{stamp}_{video_id}_rankings_before_evaluation.json"
    pre.write_text(json.dumps({"saved_at": utc_now_iso(), "policy_version": learned.version, "rankings": rankings}, indent=2), encoding="utf-8")
    print(f"rankings saved before evaluation: {pre}", flush=True)
    for mode, rows in rankings.items():
        print(f"  {mode:8s}: " + ", ".join(f"{r['mutation']} {r['score']:.3f}" for r in rows), flush=True)

    all_types = sorted({r["mutation"] for r in rankings["none"]})

    def on_stage(stage: str, msg: str, _d: dict | None) -> None:
        print(f"  [{time.strftime('%H:%M:%S')}] [{stage}] {msg[:400]}", flush=True)

    oracle = run_creative_experiment(video_id=video_id, video_path=video_path, providers=providers, data_dir=s.data_dir, policy=load_policy(policy_path),
                                     corpus=corpus, policy_mode="learned", max_arms=10, trials=trials, record_policy=False, arm_filter=all_types, on_stage=on_stage)
    outcomes: dict[str, str] = {}
    cost: dict[str, dict[str, int]] = {}
    detail = json.loads((s.data_dir / "creative" / "experiments" / f"{oracle.id}.detail.json").read_text(encoding="utf-8"))
    for arm in oracle.arms:
        if arm.mutation is None or oracle.decision is None:
            continue
        m = arm.mutation.type.value
        outcomes[m] = oracle.decision.per_arm.get(arm.id, "neutral")
        d = detail["per_arm_detail"].get(arm.label, {})
        cost[m] = {"model_calls": int(d.get("model_calls", 0)), "eval_ms": int(d.get("eval_ms", 0)), "render_ms": int(arm.render_ms or 0)}

    report: dict[str, Any] = {"video_id": video_id, "policy_version": learned.version, "oracle_experiment": oracle.id, "outcomes_on_b": outcomes, "modes": {}}
    for mode, rows in rankings.items():
        order = [r["mutation"] for r in rows]
        k = attempts_until_win(order, outcomes)
        spent = order[: (k or len(order))]
        report["modes"][mode] = {
            "order": order,
            "first_mutation": order[0] if order else None,
            "first_outcome_on_b": outcomes.get(order[0]) if order else None,
            "attempts_until_first_win": k,
            "model_calls_until_then": sum(cost[m]["model_calls"] for m in spent if m in cost),
            "eval_and_render_ms_until_then": sum(cost[m]["eval_ms"] + cost[m]["render_ms"] for m in spent if m in cost),
            "note": None if k else f"no win among {len(order)} candidates",
        }
    report["first_choice_changed"] = report["modes"]["none"]["first_mutation"] != report["modes"]["learned"]["first_mutation"]
    _, url = current_call_ref()
    report["weave_url"] = url
    report["oracle_weave_url"] = oracle.weave_url
    (out_dir / f"{stamp}_{video_id}_transfer_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("video_id")
    ap.add_argument("--policy", default="data/creative/policy.json")
    ap.add_argument("--corpus", default="data/corpus/curio_corpus_with_patterns.json")
    ap.add_argument("--trials", type=int, default=3)
    args = ap.parse_args()
    w = init_weave(get_settings())
    print(f"weave connected={w.connected}", flush=True)
    report = next_video_policy_effect(Path(args.video), args.video_id, Path(args.policy), Path(args.corpus), args.trials)
    flush()
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
