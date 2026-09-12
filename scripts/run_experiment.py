"""Run one creative experiment on a real video and print every stage.

Usage:
  .venv/bin/python scripts/run_experiment.py VIDEO_PATH VIDEO_ID [--mode learned|none] [--no-record] [--arms 3]
      [--policy data/creative/policy.json] [--all-candidates]
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from directorloop.config import get_settings
from directorloop.creative.experiment import run_creative_experiment
from directorloop.creative.fitness import fitness_table
from directorloop.creative.policy import load_policy, save_policy
from directorloop.domain.creative import ReferenceCorpus
from directorloop.observability import flush, init_weave
from directorloop.providers import build_providers


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("video_id")
    ap.add_argument("--mode", default="learned", choices=["learned", "none"])
    ap.add_argument("--no-record", action="store_true")
    ap.add_argument("--arms", type=int, default=3)
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--policy", default="data/creative/policy.json")
    ap.add_argument("--corpus", default="data/corpus/curio_corpus_with_patterns.json")
    ap.add_argument("--all-candidates", action="store_true", help="evaluate every valid candidate, not only the selected arms")
    args = ap.parse_args()
    s = get_settings()
    w = init_weave(s)
    print(f"weave connected={w.connected} {w.reason}", flush=True)
    providers = build_providers(s)
    policy_path = Path(args.policy)
    policy = load_policy(policy_path)
    corpus = None
    if Path(args.corpus).exists():
        corpus = ReferenceCorpus.model_validate_json(Path(args.corpus).read_text(encoding="utf-8"))
    print(f"policy version {policy.version} ({len(policy.strategies)} strategies); corpus {'loaded v' + str(corpus.version) if corpus else 'not loaded'}", flush=True)

    def on_stage(stage: str, msg: str, _d: dict | None) -> None:
        print(f"  [{time.strftime('%H:%M:%S')}] [{stage}] {msg[:600]}", flush=True)

    arm_filter = None
    if args.all_candidates:
        arm_filter = ["PROOF_EARLIER", "PAYOFF_EARLIER", "RESULT_FIRST", "CONTEXT_COMPRESSION", "REMOVE_REDUNDANT_BEAT", "PATTERN_INTERRUPT", "SHORTEN_SHOT"]
    exp = run_creative_experiment(
        video_id=args.video_id, video_path=Path(args.video), providers=providers, data_dir=s.data_dir, policy=policy, corpus=corpus,
        policy_mode=args.mode, max_arms=args.arms, trials=args.trials, record_policy=not args.no_record, arm_filter=arm_filter, on_stage=on_stage,
    )
    if not args.no_record:
        save_policy(policy, policy_path)
    flush()
    fits = {f"{a.label}{' ' + a.mutation.type.value if a.mutation else ''}": a.fitness for a in exp.arms if a.fitness}
    print(json.dumps(fitness_table(fits), indent=1))
    print(json.dumps({
        "experiment_id": exp.id, "decision": exp.decision.model_dump() if exp.decision else None, "timings_ms": exp.timings_ms,
        "model_calls": exp.model_calls, "policy_version": [exp.policy_version_before, exp.policy_version_after], "policy_updates": exp.policy_updates,
        "arms": [{"label": a.label, "mutation": a.mutation.description if a.mutation else "original", "path": a.artifact_path} for a in exp.arms],
        "weave_url": exp.weave_url,
    }, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
