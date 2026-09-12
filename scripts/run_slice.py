"""Run the fixture vertical slice end to end: baseline -> one improvement iteration.

Regression check for the evaluation/repair engine. Prints every stage and the final decision.
Usage: .venv/bin/python scripts/run_slice.py [pack_dir] [--fresh]
"""

from __future__ import annotations

import json
import shutil
import sys
import time

from directorloop.config import get_settings
from directorloop.domain import load_pack
from directorloop.loop import build_baseline, load_or_create_state, run_iteration, state_path
from directorloop.observability import flush, init_weave
from directorloop.planning import LatencyProfile, load_store, save_store
from directorloop.providers import build_providers


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    fresh = "--fresh" in sys.argv
    pack_dir = args[0] if args else "packs/pack_a_stand_demo"
    s = get_settings()
    weave = init_weave(s)
    print(f"weave connected={weave.connected} {weave.reason}")
    pack = load_pack(pack_dir)
    data = s.data_dir
    if fresh:
        shutil.rmtree(state_path(data, pack.project_id).parent, ignore_errors=True)
    state = load_or_create_state(data, pack.project_id, pack.truth.title, str(pack.root))
    providers = build_providers(s)
    print(f"probe={providers.probe.capability.name}:{providers.probe.capability.model} planner={providers.planner.capability.name if providers.planner else 'rules'}")

    def on_stage(stage: str, msg: str, _data: dict | None) -> None:
        print(f"  [{time.strftime('%H:%M:%S')}] [{stage}] {msg}", flush=True)

    t0 = time.monotonic()
    v0 = build_baseline(pack=pack, state=state, providers=providers, data_dir=data, trials=s.dl_probe_trials, on_stage=on_stage)
    print(f"baseline {v0.label} in {int((time.monotonic() - t0) * 1000)} ms: {v0.artifact_path}")
    store = load_store(data / "policy_store.json")
    lat = LatencyProfile.load(data / "latency_profile.json")
    t1 = time.monotonic()
    res = run_iteration(pack=pack, state=state, base_version=v0, providers=providers, store=store, latency=lat, data_dir=data,
                        trials=s.dl_probe_trials, deadline_ms=s.dl_job_deadline_seconds * 1000, on_stage=on_stage)
    save_store(store, data / "policy_store.json")
    lat.save(data / "latency_profile.json")
    flush()
    wall = int((time.monotonic() - t1) * 1000)
    summary = {
        "outcome": res.outcome,
        "iteration_wall_ms": wall,
        "timings_ms": res.timings_ms,
        "finding": res.finding.category if res.finding else None,
        "action": res.proposal.action if res.proposal else None,
        "planner": res.proposal.planner if res.proposal else None,
        "diff": res.diff_lines,
        "decision": res.decision.outcome if res.decision else None,
        "reason": res.decision.reason if res.decision else None,
        "fixed": res.comparison.fixed if res.comparison else None,
        "regressed": res.comparison.regressed if res.comparison else None,
        "candidate": res.candidate_version.artifact_path if res.candidate_version else None,
        "weave_url": res.weave_url,
    }
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
