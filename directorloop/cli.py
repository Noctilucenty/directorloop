"""DirectorLoop command line.

  directorloop run VIDEO --objective TEXT [--constraint TEXT ...] [--budget N] [--focus attention|comprehension|...]
                   [--allow-edit REMOVE_BEAT ...] [--video-id ID] [--category NAME]
  directorloop show RUN_ID            print a stored run: every iteration, decision and reason
  directorloop audit VIDEO [--video-id ID]
  directorloop serve                  start the API and web app (loopback by default)

A run is one launch: cold-audience audit, diagnosis, repair selection, render, change verification, fresh audit,
comparison and next-action decision, repeated within the budget. Progress prints as it happens; the full record is
written to data/runs/<run_id>.json and traced in Weave when WANDB_API_KEY is configured.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

from .config import get_settings


def _video_id(path: Path) -> str:
    from .domain.ids import sha256_file

    slug = re.sub(r"[^a-z0-9]+", "-", path.stem.lower()).strip("-")[:40] or "video"
    return f"{slug}-{sha256_file(path)[:8]}"


def _progress(stage: str, message: str, data: dict | None) -> None:
    print(f"  [{time.strftime('%H:%M:%S')}] {stage:13s} {message[:400]}", flush=True)


def print_run(run) -> None:  # noqa: ANN001
    c = run.config
    print(f"\nRUN {run.id}  status={run.status}  launched via {run.launched_via}")
    print(f"video: {c.video_id} ({Path(run.original_path).name})  objective: {c.objective}")
    print(f"constraints: {c.constraints or 'none'}  focus: {c.focus or 'none'}  allowed edits: {c.allowed_edits or 'all'}  budget: {c.iteration_budget}")
    print(f"reviewer: {run.runtime.get('reviewer')}  selector: {run.runtime.get('selector')}  commit: {run.runtime.get('git_commit')}")
    print(f"audits: {', '.join(run.audit_ids)}")
    for it in run.iterations:
        print(f"\n  ITERATION {it.index} on {it.current_version_id} (audit {it.audit_id})")
        for cf in it.considered:
            state = cf.skipped or (f"route {cf.route}, runnable" if cf.runnable else f"route {cf.route}, not runnable: {cf.why_not_runnable}")
            print(f"    considered {cf.interval_ms[0] / 1000:.1f}-{cf.interval_ms[1] / 1000:.1f}s [{cf.issue_type}/{cf.objective}, {cf.severity}] {state}")
            if cf.selection_reason:
                print(f"       selector: {cf.selection_reason[:260]}")
        if it.finding_id:
            print(f"    chosen finding: {it.finding[:200]}")
            print(f"    ranking: {it.ranking_reason}")
            print(f"    edit: {it.edit} ({it.mutation_type})")
            print(f"    change verified: {it.change_verified}  {'; '.join(it.verification_checks)[:400]}")
            if it.candidate_audit_id:
                print(f"    candidate {it.candidate_version_id}: fresh audit {it.candidate_audit_id}")
            if it.outcome:
                print(f"    outcome: {it.outcome}  target resolved: {it.target_resolved}")
                for x in it.improved[:5]:
                    print(f"      + {x[:200]}")
                for x in it.regressed[:5]:
                    print(f"      - {x[:200]}")
        print(f"    DECISION: {it.decision}: {it.reason}")
        print(f"    NEXT: {it.next_action}: {it.next_action_reason}")
    print(f"\nFINAL: {run.final_decision}")
    print(f"stop reason: {run.stop_reason}")
    print(f"final file: {run.final_path}")
    print(f"mocked stages: {run.mocked_stages or 'none'}  manual interventions: {run.manual_interventions or 'none'}")
    print(f"weave: {run.weave_url}")
    print(f"total: {run.timings_ms.get('total_ms')} ms")


def cmd_run(args: argparse.Namespace) -> int:
    from .observability import flush, init_weave
    from .providers import build_providers
    from .runtime.director import RunConfig, run_director

    s = get_settings()
    video = Path(args.video).expanduser().resolve()
    if not video.is_file():
        print(f"video not found: {video}", file=sys.stderr)
        return 2
    config = RunConfig(video_id=args.video_id or _video_id(video), video_path=str(video), objective=args.objective, focus=args.focus,
                       constraints=args.constraint or [], allowed_edits=args.allow_edit, iteration_budget=args.budget, category=args.category)
    w = init_weave(s)
    print(f"weave: {'connected to ' + w.project if w.connected else 'not connected (' + w.reason + ')'}")
    providers = build_providers(s)
    run = run_director(config, providers, s.data_dir, on_stage=_progress, launched_via="cli")
    flush()
    print_run(run)
    return 0 if run.status == "completed" else 1


def print_abc(run) -> None:  # noqa: ANN001
    ctx = run.context
    print(f"\nA/B-TO-C RUN {run.id}  status={run.status}  launched via {run.launched_via}")
    print(f"context: {ctx.creative_type}; viewer: {ctx.audience}; objective: {ctx.objective}; encounter: {ctx.encounter}")
    print(f"rubric {run.rubric.get('version')} sha256 {str(run.rubric.get('sha256'))[:16]} (frozen before any C was chosen)")
    for key, v in run.versions.items():
        print(f"  {key}: {v.video_id} evaluated {v.evaluated_hash[:12]} ({v.duration_ms} ms) audit {v.audit_id} [{v.audit_status}]")
    comp = run.comparison
    if comp is not None:
        print(f"comparable: {comp.comparable}  wording similarity: {comp.transcript_similarity}  confounds: {comp.confounds or 'none'}")
        for u in comp.alignment:
            a = "-" if u.a_start_ms is None else f"{u.a_start_ms / 1000:.1f}-{u.a_end_ms / 1000:.1f}s"
            b = "-" if u.b_start_ms is None else f"{u.b_start_ms / 1000:.1f}-{u.b_end_ms / 1000:.1f}s"
            print(f"  part {u.index + 1} [{u.kind}] A {a} | B {b} | {u.text_a[:60]!r} | {u.text_b[:60]!r}")
        if comp.whole:
            print("A vs B (whole, both orders): " + "; ".join(f"{d.dimension}={d.verdict}" for d in comp.whole.dimensions) + f"; overall={comp.whole.overall.verdict}")
        for r in comp.regions:
            print(f"  part {int((r.region or {}).get('unit_index', -1)) + 1}: " + "; ".join(f"{d.dimension}={d.verdict}" for d in r.dimensions) + f"; overall={r.overall.verdict}")
        print(f"best supported input: {comp.best_supported or 'none'} ({comp.best_supported_reason})")
    for att in run.attempts:
        print(f"\n  C ATTEMPT {att.index}: {att.decision} -> next {att.next_action}")
        if att.proposal:
            pr = att.proposal
            print(f"    proposal: {pr.description}")
            print(f"    targets: {pr.target_dimensions}  protected: {pr.protected_strengths}")
            print(f"    tradeoffs: {pr.tradeoffs}  why smallest: {pr.why_smallest}")
            print(f"    selector: {pr.selector_reason[:300]}")
        ev = att.evaluation
        if ev is not None:
            if ev.change_verification:
                print(f"    verified: {ev.change_verification.verified}  {'; '.join(ev.change_verification.checks)[:500]}")
            if ev.vs_base:
                print(f"    C vs {ev.vs_base.second}: " + "; ".join(f"{d.dimension}={d.verdict}" for d in ev.vs_base.dimensions) + f"; overall={ev.vs_base.overall.verdict}")
            if ev.vs_other:
                print(f"    C vs {ev.vs_other.second}: " + "; ".join(f"{d.dimension}={d.verdict}" for d in ev.vs_other.dimensions) + f"; overall={ev.vs_other.overall.verdict}")
            if ev.target_region:
                print("    changed stretch: " + "; ".join(f"{d.dimension}={d.verdict}" for d in ev.target_region.dimensions) + f"; overall={ev.target_region.overall.verdict}")
            print(f"    protected: {ev.protected}")
            print(f"    new weaknesses: {ev.new_weaknesses or 'none'}")
            print(f"    outcome: {ev.outcome}: {ev.outcome_reason}")
        print(f"    reason: {att.reason}  | next: {att.next_action_reason}")
        if att.render_path:
            print(f"    C file: {att.render_path}")
    print(f"\nFINAL: {run.final_decision}")
    print(f"stop reason: {run.stop_reason}")
    print(f"usage: {run.usage}")
    print(f"mocked stages: {run.mocked_stages or 'none'}  manual interventions: {run.manual_interventions or 'none'}")
    print(f"weave: {run.weave_url}")


def cmd_abc(args: argparse.Namespace) -> int:
    from .compare.models import DeclaredContext
    from .observability import flush, init_weave
    from .providers import build_providers
    from .runtime.abc import ABCConfig, run_abc

    s = get_settings()
    a, b = Path(args.a).expanduser().resolve(), Path(args.b).expanduser().resolve()
    for p in (a, b):
        if not p.is_file():
            print(f"video not found: {p}", file=sys.stderr)
            return 2
    ctx = DeclaredContext(creative_type=args.creative_type, audience=args.audience, objective=args.objective, expected_payoff=args.expected_payoff or "",
                          encounter=args.encounter)
    config = ABCConfig(a_video_id=args.a_id or _video_id(a), a_path=str(a), b_video_id=args.b_id or _video_id(b), b_path=str(b), context=ctx,
                       constraints=args.constraint or [], iteration_budget=args.budget, max_model_calls=args.max_calls, deadline_s=args.deadline)
    w = init_weave(s)
    print(f"weave: {'connected to ' + w.project if w.connected else 'not connected (' + w.reason + ')'}")
    run = run_abc(config, build_providers(s), s.data_dir, on_stage=_progress, launched_via="cli")
    flush()
    print_abc(run)
    return 0 if run.status == "completed" else 1


def cmd_show(args: argparse.Namespace) -> int:
    from .runtime.director import load_run

    if args.run_id.startswith("abc_"):
        from .runtime.abc import load_abc

        abc = load_abc(get_settings().data_dir, args.run_id)
        if abc is None:
            print(f"run not found: {args.run_id}", file=sys.stderr)
            return 2
        print_abc(abc)
        return 0
    run = load_run(get_settings().data_dir, args.run_id)
    if run is None:
        print(f"run not found: {args.run_id}", file=sys.stderr)
        return 2
    print_run(run)
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    from .audit.review import run_audit
    from .observability import flush, init_weave
    from .providers import build_providers

    s = get_settings()
    video = Path(args.video).expanduser().resolve()
    init_weave(s)
    rep = run_audit(video_id=args.video_id or _video_id(video), video_path=video, version_id="v0", provider=build_providers(s).probe, data_dir=s.data_dir, on_stage=_progress)
    flush()
    print(f"audit {rep.id}: {rep.status}, {len(rep.findings)} findings, {len(rep.strengths)} strengths; weave {rep.weave_url}")
    for f in rep.findings:
        print(f"  {f.start_ms / 1000:.1f}-{f.end_ms / 1000:.1f}s [{f.issue_type}/{f.objective}, {f.severity}, uncertainty {f.uncertainty}] {f.weakness}")
    return 0


def cmd_serve(_: argparse.Namespace) -> int:
    from .api.app import main as serve

    serve()
    return 0


def main(argv: list[str] | None = None) -> int:
    from .audit.models import OBJECTIVES
    from .runtime.director import EDIT_TYPES

    ap = argparse.ArgumentParser(prog="directorloop", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="audit, repair, re-evaluate and decide, within an iteration budget")
    r.add_argument("video")
    r.add_argument("--objective", required=True)
    r.add_argument("--constraint", action="append", help="repeatable; free text checked by the selector and the protected-content review")
    r.add_argument("--budget", type=int, default=3, help="maximum number of rendered repair attempts")
    r.add_argument("--focus", choices=OBJECTIVES)
    r.add_argument("--allow-edit", action="append", choices=list(EDIT_TYPES), help="repeatable; restrict the edit types the runtime may execute")
    r.add_argument("--video-id")
    r.add_argument("--category", default="educational_short")
    r.set_defaults(fn=cmd_run)
    c = sub.add_parser("abc", help="compare two edits of the same idea, direct and test a third version")
    c.add_argument("a")
    c.add_argument("b")
    c.add_argument("--objective", required=True)
    c.add_argument("--creative-type", default="educational short")
    c.add_argument("--audience", default="general viewers who do not know the topic")
    c.add_argument("--expected-payoff")
    c.add_argument("--encounter", default="a cold scrolling feed on a phone with sound on")
    c.add_argument("--constraint", action="append")
    c.add_argument("--budget", type=int, default=2, help="maximum number of rendered C attempts")
    c.add_argument("--max-calls", type=int, default=260)
    c.add_argument("--deadline", type=int, default=1500, help="seconds")
    c.add_argument("--a-id")
    c.add_argument("--b-id")
    c.set_defaults(fn=cmd_abc)
    sh = sub.add_parser("show", help="print a stored run")
    sh.add_argument("run_id")
    sh.set_defaults(fn=cmd_show)
    a = sub.add_parser("audit", help="cold-audience audit only")
    a.add_argument("video")
    a.add_argument("--video-id")
    a.set_defaults(fn=cmd_audit)
    sv = sub.add_parser("serve", help="start the API and web app")
    sv.set_defaults(fn=cmd_serve)
    args = ap.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
