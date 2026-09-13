"""Temporal attention analysis over cold-viewer window reviews.

Deterministic: no model calls, no I/O. The inputs are WindowReaction records from prefix-only reviews (each reviewer saw
the video only up to the end of its window). The outputs are ordinal, model-predicted structures: where predicted
attention risk first rises, where it peaks, whether a later window returns to low, which set-up the viewer said it was
still waiting on, and which regions a repair planner could consider. Nothing here estimates retention, survival
probability or confidence; risk levels are ordered labels, not probabilities.

Used by run_audit:
  coarse windows -> plan_precision() -> precision windows (same prompt, finer windows) -> build_attention_timeline()
  -> attention_brief() for the diagnosis -> link_findings()
and by compare_versions: compare_attention_timelines().
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from .models import (
    AttentionComparison,
    AttentionEvent,
    AttentionSummary,
    AttentionTimeline,
    EvaluatorRecord,
    ExpectationThread,
    FailureRegion,
    PrecisionScan,
    RiskChange,
    RiskSegment,
    TimeRange,
    ViewerState,
    WindowReaction,
)

COLD_VIEWER_PROMPT_VERSION = "cold-viewer-v2"  # v2: words must end by the window boundary, loudness is relative to the prefix, payoff fields

# Ordinal position of each predicted risk label. Used for ordering, transitions and drawing only; not a probability.
RISK_ORDINAL: dict[str, int] = {"low": 0, "medium": 1, "high": 2}
LEVEL_NAME = {v: k for k, v in RISK_ORDINAL.items()}
NEGATIVE_REACTIONS = frozenset({"losing_interest", "confused"})
SETUP_TOLERANCE_MS = 1000  # reported set-up times closer than this are treated as the same set-up


@dataclass(frozen=True)
class AttentionConfig:
    """Temporal analysis settings. Defaults keep the existing 2 s coarse scan and add bounded precision scans."""

    precision_enabled: bool = True
    coarse_window_ms: int = 2000
    precision_window_ms: int = 500
    precision_lead_ms: int = 500  # also inspect this much of the window before the one where risk rose
    precision_trail_ms: int = 500  # and this much of the next window when risk rises again there
    max_region_ms: int = 3000
    max_precision_regions: int = 2
    max_precision_windows: int = 10
    precision_frames: int = 3  # frames inside each precision window (start, middle, end of the moment)

    @classmethod
    def from_settings(cls, settings: Any) -> AttentionConfig:
        return cls(
            precision_enabled=bool(getattr(settings, "dl_attention_precision_enabled", True)),
            coarse_window_ms=int(getattr(settings, "dl_attention_coarse_window_ms", 2000)),
            precision_window_ms=int(getattr(settings, "dl_attention_precision_window_ms", 500)),
            max_precision_regions=int(getattr(settings, "dl_attention_max_precision_regions", 2)),
            max_precision_windows=int(getattr(settings, "dl_attention_max_precision_windows", 10)),
        )


def level(r: WindowReaction) -> int | None:
    """Ordinal risk of a reviewed window, or None when the review failed or returned no valid label."""
    if r.error or r.attention_risk not in RISK_ORDINAL:
        return None
    return RISK_ORDINAL[r.attention_risk]


def state_text(r: WindowReaction | ViewerState) -> str:
    return f"{r.attention_risk} / {r.reaction}"


def fmt_range(start_ms: int, end_ms: int) -> str:
    return f"{start_ms / 1000:.1f}-{end_ms / 1000:.1f}s"


def viewer_state(r: WindowReaction) -> ViewerState:
    return ViewerState(start_ms=r.start_ms, end_ms=r.end_ms, scan=r.scan, reaction=r.reaction, attention_risk=r.attention_risk, understanding=r.understanding,
                       expectation=r.expectation, open_question=r.open_question, cause=r.cause, payoff=r.payoff)


# ----------------------------------------------------------------------------- precision planning


def grid_windows(start_ms: int, end_ms: int, step_ms: int, duration_ms: int) -> list[tuple[int, int]]:
    """Windows on a global grid of step_ms inside [start, end], clipped to the video. A final piece shorter than 40% of a
    step joins the window before it, so no review covers only a sliver and nothing runs past the end of the video."""
    s = max(0, min(start_ms, duration_ms))
    e = max(0, min(end_ms, duration_ms))
    if e <= s or step_ms <= 0:
        return []
    t = (s // step_ms) * step_ms
    wins: list[tuple[int, int]] = []
    while t < e:
        we = min(e, t + step_ms)
        wins.append((t, we))
        t = we
    if len(wins) >= 2 and wins[-1][1] - wins[-1][0] < step_ms * 0.4:
        last, prev = wins.pop(), wins.pop()
        wins.append((prev[0], last[1]))
    return wins


def detect_deterioration(coarse: Iterable[WindowReaction]) -> list[tuple[int, str]]:
    """Coarse windows where predicted attention begins to deteriorate, with the reason. A rise in the risk label, a reaction
    turning to losing interest or confusion without the risk falling, or an opening that is already high or negative.
    A steady level, a question on the viewer's mind, or a failed window never triggers on its own."""
    ws = sorted(coarse, key=lambda r: r.start_ms)
    out: list[tuple[int, str]] = []
    prev: WindowReaction | None = None
    for i, w in enumerate(ws):
        lv = level(w)
        if lv is None:
            continue
        negative = w.reaction in NEGATIVE_REACTIONS
        if prev is None:
            if lv == 2 or negative:
                where = "the video opens" if w.start_ms == 0 else f"the first reviewed window ({fmt_range(w.start_ms, w.end_ms)}) is"
                out.append((i, f"{where} at {w.attention_risk} risk, {w.reaction}"))
        else:
            plv = level(prev)
            assert plv is not None
            if lv > plv:
                out.append((i, f"risk rose from {prev.attention_risk} to {w.attention_risk} at {fmt_range(w.start_ms, w.end_ms)}"))
            elif negative and prev.reaction not in NEGATIVE_REACTIONS and lv >= plv:
                out.append((i, f"reaction turned to {w.reaction} at {w.attention_risk} risk at {fmt_range(w.start_ms, w.end_ms)}"))
        prev = w
    return out


@dataclass
class PlannedScan:
    scan: PrecisionScan
    windows: list[tuple[int, int]] = field(default_factory=list)


def plan_precision(coarse: list[WindowReaction], duration_ms: int, cfg: AttentionConfig) -> list[PlannedScan]:
    """Precision regions around deterioration points, merged when they overlap, limited by region and window budgets.
    Windows sit on a shared grid, so two regions never review the same moment twice."""
    if not cfg.precision_enabled or duration_ms <= 0:
        return []
    ws = sorted(coarse, key=lambda r: r.start_ms)
    reviewed = [i for i, w in enumerate(ws) if level(w) is not None]
    candidates: list[dict[str, Any]] = []
    for i, reason in detect_deterioration(ws):
        w = ws[i]
        lv = level(w) or 0
        prev_known = [j for j in reviewed if j < i]
        prev_lv = level(ws[prev_known[-1]]) if prev_known else None
        s = w.start_ms - (cfg.precision_lead_ms if w.start_ms > 0 else 0)
        e = w.end_ms
        nxt = next((j for j in reviewed if j > i), None)
        if nxt is not None and nxt == i + 1 and (level(ws[nxt]) or 0) > lv:
            e = min(ws[nxt].end_ms, w.end_ms + cfg.precision_trail_ms)
        s, e = max(0, s), min(duration_ms, e)
        over = (e - s) - cfg.max_region_ms
        if over > 0:
            s = min(w.start_ms, s + over)
            over = (e - s) - cfg.max_region_ms
        if over > 0:
            e = max(w.end_ms, e - over)
        jump = lv - (prev_lv if prev_lv is not None else 0)
        candidates.append({"i": i, "reason": reason, "start": s, "end": e, "priority": (-lv, -jump, w.start_ms), "trigger": (w.start_ms, w.end_ms)})
    candidates.sort(key=lambda c: c["priority"])
    chosen, skipped = candidates[: cfg.max_precision_regions], candidates[cfg.max_precision_regions:]
    chosen.sort(key=lambda c: c["start"])
    merged: list[dict[str, Any]] = []
    for c in chosen:
        if merged and c["start"] < merged[-1]["end"]:
            m = merged[-1]
            m["end"] = max(m["end"], c["end"])
            m["reason"] = f"{m['reason']}; {c['reason']}"
            m["priority"] = min(m["priority"], c["priority"])
        else:
            merged.append(dict(c))
    plans: list[PlannedScan] = []
    used: set[tuple[int, int]] = set()
    budget = cfg.max_precision_windows
    for n, c in enumerate(sorted(merged, key=lambda m: m["priority"]), start=1):
        grid = [g for g in grid_windows(c["start"], c["end"], cfg.precision_window_ms, duration_ms) if g not in used]
        # nearest the moment where the coarse risk changed first, so a tight budget still covers the onset
        grid.sort(key=lambda g: abs(g[0] - c["trigger"][0]))
        take = sorted(grid[: max(0, budget)])
        scan = PrecisionScan(id=f"p{n}", trigger=c["reason"], trigger_window=TimeRange(start_ms=c["trigger"][0], end_ms=c["trigger"][1]),
                             region=TimeRange(start_ms=take[0][0] if take else c["start"], end_ms=take[-1][1] if take else c["end"]),
                             window_ms=cfg.precision_window_ms, windows_planned=len(take))
        if not take:
            scan.status, scan.skipped_reason = "skipped", "the precision window budget was already used"
        budget -= len(take)
        used.update(take)
        plans.append(PlannedScan(scan=scan, windows=take))
    for c in skipped:
        plans.append(PlannedScan(scan=PrecisionScan(id=f"p{len(plans) + 1}", trigger=c["reason"], trigger_window=TimeRange(start_ms=c["trigger"][0], end_ms=c["trigger"][1]),
                                                    region=TimeRange(start_ms=c["start"], end_ms=c["end"]), window_ms=cfg.precision_window_ms, status="skipped",
                                                    skipped_reason=f"more than {cfg.max_precision_regions} deterioration points; lower-priority region not scanned")))
    plans.sort(key=lambda p: p.scan.region.start_ms)
    return plans


# ----------------------------------------------------------------------------- timeline


def build_segments(coarse: list[WindowReaction], precision: list[WindowReaction]) -> tuple[list[RiskSegment], list[WindowReaction]]:
    """Non-overlapping segments at the finest reviewed resolution: a reviewed precision window replaces the part of a coarse
    window it covers; a failed precision window leaves the coarse reading in place. Returns the segments and, for each, the
    reading it came from."""
    sources = sorted(coarse, key=lambda r: r.start_ms)
    fine = [p for p in sorted(precision, key=lambda r: r.start_ms) if level(p) is not None]
    bounds = sorted({t for r in sources + fine for t in (r.start_ms, r.end_ms)})
    pieces: list[tuple[int, int, WindowReaction]] = []
    for a, b in zip(bounds, bounds[1:], strict=False):
        if b <= a:
            continue
        mid = (a + b) / 2
        src = next((p for p in fine if p.start_ms <= mid < p.end_ms), None) or next((c for c in sources if c.start_ms <= mid < c.end_ms), None)
        if src is None:
            continue
        if pieces and pieces[-1][2] is src and pieces[-1][1] == a:
            pieces[-1] = (pieces[-1][0], b, src)
        else:
            pieces.append((a, b, src))
    segments = []
    for a, b, src in pieces:
        lv = level(src)
        segments.append(RiskSegment(start_ms=a, end_ms=b, scan=src.scan, attention_risk=src.attention_risk if lv is not None else "unknown", risk_ordinal=lv,
                                    predicted_attention_score=None if lv is None else round(1 - lv / 2, 2), reaction=src.reaction if lv is not None else "unknown"))
    return segments, [p[2] for p in pieces]


def _runs(indices: list[int]) -> list[list[int]]:
    runs: list[list[int]] = []
    for i in indices:
        if runs and runs[-1][-1] == i - 1:
            runs[-1].append(i)
        else:
            runs.append([i])
    return runs


def expectation_threads(readings: list[WindowReaction]) -> list[ExpectationThread]:
    """Threads of consecutive readings that report waiting on a set-up at compatible reported times. Wording is never
    matched; a reading that reports waiting without a set-up time cannot join or start a thread."""
    threads: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for r in readings:
        if level(r) is None:
            continue
        if r.payoff == "waiting" and r.waiting_since_ms is not None:
            if current is not None and abs((current["set_up"] or 0) - r.waiting_since_ms) <= SETUP_TOLERANCE_MS and not current["resolved"]:
                current["last"] = r
                current["items"].append(r)
            else:
                current = {"set_up": r.waiting_since_ms, "first": r, "last": r, "items": [r], "resolved": None}
                threads.append(current)
        elif r.payoff == "delivered" and current is not None and current["resolved"] is None and r.start_ms >= current["last"].start_ms:
            current["resolved"] = r
            current = None
        elif r.payoff == "none":
            current = None
    out = []
    for n, t in enumerate(threads, start=1):
        elevated = next((x for x in t["items"] if (level(x) or 0) > 0), None)
        out.append(ExpectationThread(
            id=f"x{n}", set_up_ms=t["set_up"], first_waiting=TimeRange(start_ms=t["first"].start_ms, end_ms=t["first"].end_ms),
            last_waiting=TimeRange(start_ms=t["last"].start_ms, end_ms=t["last"].end_ms),
            resolved=TimeRange(start_ms=t["resolved"].start_ms, end_ms=t["resolved"].end_ms) if t["resolved"] else None,
            expectations=list(dict.fromkeys(x.expectation for x in t["items"] if x.expectation))[:4],
            open_questions=list(dict.fromkeys(x.open_question for x in t["items"] if x.open_question))[:4],
            elevated_while_waiting=TimeRange(start_ms=elevated.start_ms, end_ms=elevated.end_ms) if elevated else None,
        ))
    return out


def _failure_type(run: list[WindowReaction], at_start: bool, peak: int) -> str:
    if at_start and peak == 2:
        return "hook_failure"
    reactions = {r.reaction for r in run}
    if "losing_interest" in reactions and "confused" in reactions:
        return "interest_loss_and_confusion"
    if "losing_interest" in reactions:
        return "interest_loss"
    if "confused" in reactions:
        return "confusion"
    return "elevated_risk"


def build_attention_timeline(coarse: list[WindowReaction], precision: list[WindowReaction], scans: list[PrecisionScan], duration_ms: int,
                             evaluator: EvaluatorRecord) -> AttentionTimeline:
    segments, readings = build_segments(coarse, precision)
    events: list[AttentionEvent] = []
    known = [i for i, r in enumerate(readings) if level(r) is not None]

    # transitions between consecutive reviewed readings, at the finest resolution available
    prev: WindowReaction | None = None
    for i in known:
        r = readings[i]
        seg = segments[i]
        lv = level(r)
        assert lv is not None
        if prev is None:
            if seg.start_ms == 0 and lv == 2:
                events.append(AttentionEvent(type="initial_high_risk", start_ms=seg.start_ms, end_ms=seg.end_ms, resolution=r.scan, severity="high", to_state=state_text(r),
                                             evidence=[r.cause] if r.cause else [], note="the opening itself carries high predicted scroll risk"))
            elif seg.start_ms == 0 and (lv == 1 or r.reaction in NEGATIVE_REACTIONS):
                events.append(AttentionEvent(type="initial_elevated_risk", start_ms=seg.start_ms, end_ms=seg.end_ms, resolution=r.scan, severity=LEVEL_NAME[lv],  # type: ignore[arg-type]
                                             to_state=state_text(r), evidence=[r.cause] if r.cause else []))
        else:
            plv = level(prev)
            assert plv is not None
            if prev is not r and lv > plv:
                events.append(AttentionEvent(type="risk_increase", start_ms=seg.start_ms, end_ms=seg.end_ms, resolution=r.scan, severity=LEVEL_NAME[lv],  # type: ignore[arg-type]
                                             from_state=state_text(prev), to_state=state_text(r), evidence=[r.cause] if r.cause else []))
            if prev is not r and r.reaction != prev.reaction and r.reaction in NEGATIVE_REACTIONS and prev.reaction not in NEGATIVE_REACTIONS:
                events.append(AttentionEvent(type="interest_loss" if r.reaction == "losing_interest" else "confusion_onset", start_ms=seg.start_ms, end_ms=seg.end_ms,
                                             resolution=r.scan, severity=LEVEL_NAME[lv], from_state=state_text(prev), to_state=state_text(r),  # type: ignore[arg-type]
                                             evidence=[r.cause] if r.cause else []))
            if prev is not r and lv == 0 and plv > 0:
                events.append(AttentionEvent(type="recovery", start_ms=seg.start_ms, end_ms=seg.end_ms, resolution=r.scan, severity="low", from_state=state_text(prev),
                                             to_state=state_text(r), evidence=[r.cause] if r.cause else [],
                                             note="what this moment shows is a candidate to keep or move earlier" + (
                                                 "; viewers only reach it if they did not scroll away during the high-risk stretch before it" if plv == 2 else "")))
        if r.payoff == "delivered" and (prev is None or prev.payoff != "delivered"):
            events.append(AttentionEvent(type="payoff_delivered", start_ms=seg.start_ms, end_ms=seg.end_ms, resolution=r.scan, severity=LEVEL_NAME[lv],  # type: ignore[arg-type]
                                         to_state=state_text(r), evidence=[r.cause] if r.cause else [], note="the viewer said this moment delivered what was set up"))
        prev = r

    # contiguous high stretches are predicted drop-off regions
    high_runs = _runs([i for i in known if level(readings[i]) == 2])
    dropoffs = []
    for run in high_runs:
        s, e = segments[run[0]].start_ms, segments[run[-1]].end_ms
        dropoffs.append(TimeRange(start_ms=s, end_ms=e))
        events.append(AttentionEvent(type="predicted_dropoff", start_ms=s, end_ms=e, resolution="precision" if any(segments[i].scan == "precision" for i in run) else "coarse",
                                     severity="high", evidence=list(dict.fromkeys(readings[i].cause for i in run if readings[i].cause))[:3],
                                     note="high predicted scroll risk; a model prediction, not a measured drop-off"))

    threads = expectation_threads(readings)
    for t in threads:
        if t.elevated_while_waiting is not None:
            events.append(AttentionEvent(type="unresolved_expectation", start_ms=t.elevated_while_waiting.start_ms, end_ms=t.elevated_while_waiting.end_ms, resolution="precision"
                                         if any(s.scan == "precision" and s.start_ms <= t.elevated_while_waiting.start_ms < s.end_ms for s in segments) else "coarse",
                                         severity="medium", evidence=t.expectations[:2],
                                         note=f"still waiting on a set-up the viewer placed at {t.set_up_ms / 1000:.1f}s when risk rose" if t.set_up_ms is not None else ""))
    events.sort(key=lambda ev: (ev.start_ms, ev.type))

    # failure regions: consecutive reviewed readings above low risk
    regions: list[FailureRegion] = []
    elevated = [i for i in known if (level(readings[i]) or 0) > 0]
    for n, run in enumerate(_runs(elevated), start=1):
        rr = [readings[i] for i in run]
        peak = max(level(r) or 0 for r in rr)
        first, last = run[0], run[-1]
        onset_seg = segments[first]
        high_i = next((i for i in run if level(readings[i]) == 2), None)
        before_i = max((i for i in known if i < first), default=None)
        after_i = min((i for i in known if i > last), default=None)
        before = readings[before_i] if before_i is not None and before_i == first - 1 else None
        after = readings[after_i] if after_i is not None and after_i == last + 1 else None
        onset_r = readings[first]
        # the set-up the viewer was still waiting on when risk rose: the thread covering the onset, dated by its first report
        thread = next((th for th in threads if th.set_up_ms is not None and th.set_up_ms < onset_seg.start_ms
                       and th.first_waiting.start_ms <= onset_seg.start_ms < max(th.last_waiting.end_ms, th.first_waiting.end_ms)), None)
        region = FailureRegion(
            id=f"fr{n}", start_ms=onset_seg.start_ms, end_ms=segments[last].end_ms, failure_type=_failure_type(rr, onset_seg.start_ms == 0, peak),  # type: ignore[arg-type]
            severity=LEVEL_NAME[peak], onset=TimeRange(start_ms=onset_seg.start_ms, end_ms=onset_seg.end_ms),  # type: ignore[arg-type]
            high_risk_onset=TimeRange(start_ms=segments[high_i].start_ms, end_ms=segments[high_i].end_ms) if high_i is not None else None,
            temporal_resolution_ms=onset_seg.end_ms - onset_seg.start_ms, refined_by_precision=onset_seg.scan == "precision",
            viewer_before=viewer_state(before) if before is not None else None, viewer_during=[viewer_state(r) for r in {id(x): x for x in rr}.values()][:6],
            viewer_after=viewer_state(after) if after is not None else None,
            recovered=after is not None and level(after) == 0, recovery=TimeRange(start_ms=segments[last + 1].start_ms, end_ms=segments[last + 1].end_ms)
            if after is not None and level(after) == 0 else None,
            expectation=onset_r.expectation or (before.expectation if before is not None else ""), open_question=onset_r.open_question or (before.open_question if before is not None else ""),
            cause=onset_r.cause,
            cause_region=TimeRange(start_ms=thread.set_up_ms, end_ms=onset_seg.start_ms) if thread is not None and thread.set_up_ms is not None else None,
            cause_basis=(f"the viewer reported waiting since {thread.set_up_ms / 1000:.1f}s for: {thread.expectations[0] if thread.expectations else '(not stated)'}"
                         if thread is not None and thread.set_up_ms is not None else ""),
        )
        region.later_content_needs_survival = region.recovered and peak == 2
        regions.append(region)

    peak_level = max((level(readings[i]) or 0 for i in known), default=None)
    first_rise = next((ev for ev in events if ev.type in ("initial_high_risk", "initial_elevated_risk", "risk_increase")), None)
    first_high = next((segments[i] for i in known if level(readings[i]) == 2), None)
    peak_regions = [TimeRange(start_ms=segments[r[0]].start_ms, end_ms=segments[r[-1]].end_ms) for r in _runs([i for i in known if level(readings[i]) == peak_level])] if peak_level else []
    coarse_known = [c for c in coarse if level(c) is not None]
    summary = AttentionSummary(
        windows_reviewed=len(coarse_known), windows_failed=len(coarse) - len(coarse_known),
        precision_windows_reviewed=sum(1 for p in precision if level(p) is not None), precision_scans_triggered=sum(1 for s in scans if s.status != "skipped"),
        peak_risk=LEVEL_NAME[peak_level] if peak_level is not None else None,  # type: ignore[arg-type]
        starts_elevated=bool(known) and segments[known[0]].start_ms == 0 and (level(readings[known[0]]) or 0) > 0,
        ends_elevated=bool(known) and (level(readings[known[-1]]) or 0) > 0,
        first_risk_increase=TimeRange(start_ms=first_rise.start_ms, end_ms=first_rise.end_ms) if first_rise else None,
        first_high_risk=TimeRange(start_ms=first_high.start_ms, end_ms=first_high.end_ms) if first_high else None,
        peak_regions=peak_regions, recovery_regions=[TimeRange(start_ms=ev.start_ms, end_ms=ev.end_ms) for ev in events if ev.type == "recovery"],
        predicted_dropoff_regions=dropoffs,
    )
    summary.text = summary_text(summary, regions)
    for scan in scans:
        inside = [r for r in regions if r.refined_by_precision and scan.region.start_ms <= r.onset.start_ms < scan.region.end_ms]
        if inside:
            scan.refined_onset = inside[0].onset
    return AttentionTimeline(duration_ms=duration_ms, evaluator=evaluator, precision_scans=scans, segments=segments, events=events, failure_regions=regions,
                             expectation_threads=threads, summary=summary)


def summary_text(s: AttentionSummary, regions: list[FailureRegion]) -> str:
    if not s.windows_reviewed:
        return "No window could be reviewed, so there is no attention reading."
    if not regions:
        return f"Low predicted attention risk in all {s.windows_reviewed} reviewed windows."
    parts = []
    r0 = regions[0]
    if r0.start_ms == 0:
        parts.append(f"Starts at {r0.severity} predicted risk ({fmt_range(r0.onset.start_ms, r0.onset.end_ms)}).")
    else:
        parts.append(f"Risk first rises at {fmt_range(r0.onset.start_ms, r0.onset.end_ms)}" + (" (refined by a precision scan)." if r0.refined_by_precision else "."))
    if s.first_high_risk:
        parts.append(f"High risk from {fmt_range(s.first_high_risk.start_ms, s.first_high_risk.end_ms)}.")
    if s.recovery_regions:
        parts.append(f"Back to low at {fmt_range(s.recovery_regions[0].start_ms, s.recovery_regions[0].end_ms)}.")
    elif s.ends_elevated:
        parts.append("Still elevated at the end.")
    return " ".join(parts)


def link_findings(timeline: AttentionTimeline | None, findings: Iterable[Any]) -> None:
    """Record which diagnosed findings overlap each failure region (by time only)."""
    if timeline is None:
        return
    items = list(findings)
    for region in timeline.failure_regions:
        region.finding_ids = [f.id for f in items if f.start_ms < region.end_ms and f.end_ms > region.start_ms]


def attention_brief(timeline: AttentionTimeline | None, precision: list[WindowReaction]) -> str:
    """Lines for the whole-story diagnosis: the closer-look readings and the derived changes, labelled as model predictions."""
    if timeline is None:
        return "(no attention analysis available)"
    lines = []
    for p in sorted(precision, key=lambda r: r.start_ms):
        if level(p) is None:
            lines.append(f"closer look {fmt_range(p.start_ms, p.end_ms)}: review unavailable")
        else:
            lines.append(f"closer look {fmt_range(p.start_ms, p.end_ms)}: {p.reaction}, attention risk {p.attention_risk}; expects: {p.expectation}; cause: {p.cause}")
    for r in timeline.failure_regions:
        line = (f"predicted attention problem {fmt_range(r.start_ms, r.end_ms)} ({r.failure_type}, peak {r.severity}): onset {fmt_range(r.onset.start_ms, r.onset.end_ms)}"
                f" at {r.temporal_resolution_ms / 1000:.1f} s resolution")
        if r.high_risk_onset:
            line += f"; high from {fmt_range(r.high_risk_onset.start_ms, r.high_risk_onset.end_ms)}"
        line += f"; back to low at {fmt_range(r.recovery.start_ms, r.recovery.end_ms)}" if r.recovery else "; no return to low afterwards"
        if r.cause_basis:
            line += f"; {r.cause_basis}"
        lines.append(line)
    for t in timeline.expectation_threads:
        if t.set_up_ms is None:
            continue
        status = f"delivered at {fmt_range(t.resolved.start_ms, t.resolved.end_ms)}" if t.resolved else f"still waiting at {t.last_waiting.end_ms / 1000:.1f}s"
        lines.append(f"the viewer waited from {t.set_up_ms / 1000:.1f}s for: {t.expectations[0] if t.expectations else '(not stated)'}; {status}")
    return "\n".join(lines) or "no rise in predicted attention risk"


# ----------------------------------------------------------------------------- comparison


def _peak(segments: list[RiskSegment], start_ms: int, end_ms: int) -> int | None:
    levels = [s.risk_ordinal for s in segments if s.risk_ordinal is not None and s.start_ms < end_ms and s.end_ms > start_ms]
    return max(levels) if levels else None


def _ms_at(segments: list[RiskSegment], minimum: int) -> int:
    return sum(s.end_ms - s.start_ms for s in segments if s.risk_ordinal is not None and s.risk_ordinal >= minimum)


def compare_attention_timelines(before: AttentionTimeline | None, after: AttentionTimeline | None, *, target: tuple[int, int] | None = None,
                                to_after: Callable[[int, int], tuple[int, int] | None] | None = None,
                                to_before: Callable[[int], int | None] | None = None) -> AttentionComparison:
    """Compare two predicted attention timelines of different versions (after = the candidate). Mapping callables translate
    between the two timelines when an edit changed timing; without them times are compared directly."""
    cmp = AttentionComparison()
    if before is None or after is None or not before.segments or not after.segments:
        cmp.decision_evidence.append("an attention timeline is missing on one side, so no attention comparison was made")
        return cmp
    eb, ea = before.evaluator, after.evaluator
    for name in ("cold_viewer_prompt_version", "provider", "model", "coarse_window_ms", "precision_window_ms", "precision_enabled"):
        if getattr(eb, name) != getattr(ea, name):
            cmp.comparability_notes.append(f"{name} differs: {getattr(eb, name)} vs {getattr(ea, name)}")
    cmp.comparable = not cmp.comparability_notes
    to_after = to_after or (lambda s, e: (s, e))
    to_before = to_before or (lambda t: t)
    cmp.elevated_ms_before, cmp.elevated_ms_after = _ms_at(before.segments, 1), _ms_at(after.segments, 1)
    cmp.high_ms_before, cmp.high_ms_after = _ms_at(before.segments, 2), _ms_at(after.segments, 2)
    cmp.first_deterioration_before_ms = next((s.start_ms for s in before.segments if (s.risk_ordinal or 0) > 0), None)
    cmp.first_deterioration_after_ms = next((s.start_ms for s in after.segments if (s.risk_ordinal or 0) > 0), None)
    target_improved: bool | None = None
    if target is not None:
        cmp.target_region = TimeRange(start_ms=target[0], end_ms=target[1])
        pb = _peak(before.segments, *target)
        cmp.target_peak_before = LEVEL_NAME.get(pb) if pb is not None else None
        mapped = to_after(*target)
        if mapped is None:
            cmp.decision_evidence.append(f"the target {fmt_range(*target)} no longer exists in the candidate")
        else:
            cmp.target_region_in_candidate = TimeRange(start_ms=mapped[0], end_ms=mapped[1])
            pa = _peak(after.segments, *mapped)
            cmp.target_peak_after = LEVEL_NAME.get(pa) if pa is not None else None
            if pb is not None and pa is not None:
                target_improved = pa < pb if pb > 0 else None
                cmp.decision_evidence.append(f"target {fmt_range(*target)}: peak predicted risk {cmp.target_peak_before} before, {cmp.target_peak_after} after (candidate {fmt_range(*mapped)})")
    cmp.target_region_improved = target_improved
    for seg in after.segments:
        if seg.risk_ordinal is None:
            continue
        t = to_before((seg.start_ms + seg.end_ms) // 2)
        if t is None:
            continue
        ref = next((s for s in before.segments if s.start_ms <= t < s.end_ms and s.risk_ordinal is not None), None)
        if ref is None or ref.risk_ordinal is None:
            continue
        if seg.risk_ordinal == ref.risk_ordinal:
            continue
        change = RiskChange(start_ms=seg.start_ms, end_ms=seg.end_ms, before=ref.attention_risk, after=seg.attention_risk,  # type: ignore[arg-type]
                            original_start_ms=ref.start_ms, original_end_ms=ref.end_ms)
        text = f"{fmt_range(seg.start_ms, seg.end_ms)}: {seg.attention_risk} predicted risk, was {ref.attention_risk} at {fmt_range(ref.start_ms, ref.end_ms)} in the original"
        if seg.risk_ordinal > ref.risk_ordinal:
            cmp.regression_changes.append(change)
            cmp.new_regressions.append(text)
        else:
            cmp.improvement_changes.append(change)
            cmp.improvements.append(text)
    improved = bool(target_improved) or bool(cmp.improvements)
    if cmp.new_regressions and improved:
        cmp.global_direction = "mixed"
    elif cmp.new_regressions:
        cmp.global_direction = "regressed"
    elif improved:
        cmp.global_direction = "improved"
    else:
        cmp.global_direction = "unchanged"
    cmp.decision_evidence.append(f"time at medium or high predicted risk: {cmp.elevated_ms_before / 1000:.1f}s before, {cmp.elevated_ms_after / 1000:.1f}s after; "
                                 f"at high: {cmp.high_ms_before / 1000:.1f}s before, {cmp.high_ms_after / 1000:.1f}s after")
    if not cmp.comparable:
        cmp.decision_evidence.append("the two timelines were produced under different evaluation settings; treat the comparison as indicative only")
    return cmp


# ----------------------------------------------------------------------------- debugging view


def render_attention_text(timeline: AttentionTimeline, coarse: list[WindowReaction], precision: list[WindowReaction]) -> str:
    """Plain-text view of one analysis for logs and terminals."""
    out = [f"ATTENTION TIMELINE ({timeline.label})", f"evaluator {timeline.evaluator.cold_viewer_prompt_version} {timeline.evaluator.provider}/{timeline.evaluator.model}",
           "", "coarse scan"]
    for r in sorted(coarse, key=lambda x: x.start_ms):
        out.append(f"  {fmt_range(r.start_ms, r.end_ms):12s} {r.reaction.upper():16s} {r.attention_risk.upper():8s} payoff {r.payoff}" + (f"  (review failed: {r.error[:60]})" if r.error else ""))
    for scan in timeline.precision_scans:
        out.append("")
        out.append(f"precision scan {fmt_range(scan.region.start_ms, scan.region.end_ms)} [{scan.status}] trigger: {scan.trigger}" + (f"; {scan.skipped_reason}" if scan.skipped_reason else ""))
        for r in sorted((p for p in precision if scan.region.start_ms <= p.start_ms < scan.region.end_ms), key=lambda x: x.start_ms):
            out.append(f"  {fmt_range(r.start_ms, r.end_ms):12s} {r.reaction.upper():16s} {r.attention_risk.upper():8s}" + (f"  (review failed: {r.error[:60]})" if r.error else ""))
    if timeline.events:
        out += ["", "events"]
        for ev in timeline.events:
            out.append(f"  {ev.type:24s} {fmt_range(ev.start_ms, ev.end_ms):12s} {ev.severity:6s} {ev.resolution:9s} {(ev.from_state or '') + (' -> ' if ev.from_state else '') + (ev.to_state or '')}")
    if timeline.failure_regions:
        out += ["", "failure regions"]
        for r in timeline.failure_regions:
            out.append(f"  {r.id} {fmt_range(r.start_ms, r.end_ms)} {r.failure_type} peak {r.severity}; onset {fmt_range(r.onset.start_ms, r.onset.end_ms)} "
                       f"({r.temporal_resolution_ms} ms resolution{', precision' if r.refined_by_precision else ''})"
                       + (f"; high from {fmt_range(r.high_risk_onset.start_ms, r.high_risk_onset.end_ms)}" if r.high_risk_onset else "")
                       + (f"; back to low at {fmt_range(r.recovery.start_ms, r.recovery.end_ms)}" if r.recovery else "; no return to low"))
            if r.cause:
                out.append(f"     cause: {r.cause[:160]}")
            if r.cause_basis:
                out.append(f"     {r.cause_basis[:160]}")
    out += ["", f"summary: {timeline.summary.text}"]
    return "\n".join(out)
