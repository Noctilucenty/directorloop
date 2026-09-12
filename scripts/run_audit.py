"""Run a cold-audience audit on a real video and print the report.

Usage: .venv/bin/python scripts/run_audit.py VIDEO_PATH VIDEO_ID [--no-closeups]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from directorloop.audit.review import run_audit
from directorloop.config import get_settings
from directorloop.observability import flush, init_weave
from directorloop.providers import build_providers


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    video, video_id = Path(args[0]), args[1]
    s = get_settings()
    init_weave(s)
    provider = build_providers(s).probe
    rep = run_audit(video_id=video_id, video_path=video, version_id="original", provider=provider, data_dir=s.data_dir,
                    on_stage=lambda st, m, d: print(f"  [{time.strftime('%H:%M:%S')}] [{st}] {m[:300]}", flush=True), closeups="--no-closeups" not in sys.argv)
    flush()
    print(f"\nAUDIT {rep.id} status={rep.status} duration={rep.duration_ms} ms timings={rep.timings_ms} calls={rep.model_calls}")
    print(f"coverage: {rep.coverage.mode}, {rep.coverage.total_frames_sent} frames, prefix coverage {rep.coverage.covered_ms('prefix')}/{rep.duration_ms} ms")
    print("summary:", rep.overall_summary)
    for r in rep.window_reactions:
        print(f"  window {r.start_ms / 1000:4.1f}-{r.end_ms / 1000:4.1f}s {r.reaction:15s} risk {r.attention_risk:7s} | cause: {r.cause[:140]}")
    for f in rep.findings:
        print(f"\nFINDING {f.id} {f.start_ms / 1000:.2f}-{f.end_ms / 1000:.2f}s [{f.issue_type}] objective={f.objective} severity={f.severity} uncertainty={f.uncertainty} closeup_verified={f.verified_closeup}")
        for o in f.observed:
            print(f"   OBSERVED [{o.kind}] {o.text}" + (f" (t={o.t_ms / 1000:.1f}s)" if o.t_ms is not None else ""))
        print(f"   UNDERSTANDING: {f.viewer_understanding}")
        print(f"   PREDICTED REACTION (model judgment): {f.predicted_reaction}")
        print(f"   WEAKNESS: {f.weakness}")
        print(f"   ALTERNATIVES: {f.alternatives}")
        print(f"   REPAIR ({f.repair_kind}): {f.proposed_repair}")
        print(f"   KEEP: {f.keep_unchanged}")
        print(f"   CLOSEUP: {f.closeup_note}")
        print(f"   EVIDENCE FRAMES: {[e.t_ms for e in f.evidence_frames]}")
    for st in rep.strengths:
        print(f"\nSTRENGTH {st.start_ms / 1000:.1f}-{st.end_ms / 1000:.1f}s: {st.what} | why: {st.why}")
    print("\nAUDIENCE (model judgment):")
    for k, v in rep.audience.model_dump().items():
        if isinstance(v, dict):
            print(f"   {k:18s} {v['verdict']:22s} {v['reason'][:150]}" + (f" (weakens at {v['at_ms'] / 1000:.1f}s)" if v.get("at_ms") else "") + (f" (to: {v['to_whom']})" if v.get("to_whom") else ""))
    print("\nweave:", rep.weave_url)
    print("limitations:", rep.coverage.limitations)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
