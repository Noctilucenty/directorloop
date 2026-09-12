"""Connect audit findings to repairs the system can actually perform.

Route A (existing-video edit): deterministic, validated candidate edits on sentence beats near the finding (remove,
move earlier or later, trim edge silence, punch in). A text model chooses the candidate that implements the reviewer's
proposed repair, or none. Only typed ops that pass the plan validator are offered.
Route B (source-project edit): animation timing, camera, composition, burned-in captions. The source project is
inspected read-only and the needed materials are listed; DirectorLoop does not run the original production pipeline.
Route C (new or replacement asset): new shots or narration; reports the missing dependency.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..creative.genome import CreativeGenome
from ..creative.mutate import VIDEO_ASSET_ID, experiment_brief, identity_plan, source_manifest
from ..creative.signals import audio_rms, silence_gaps
from ..domain.creative import BeatRole, TimelineBeat
from ..domain.edit_plan import (
    CropRect,
    EditOp,
    EditPlan,
    InsertSegment,
    MoveSegment,
    RemoveSegment,
    Segment,
    SetCrop,
    TrimSegment,
    apply_ops,
)
from ..media.frames import locate_motion_region
from ..media.validate import validate_ops_scope, validate_plan
from ..observability.weave_ops import traced
from ..providers.base import ProviderError, TextPlannerProvider
from .models import AuditFinding, AuditRepair

PUNCH_MIN_MS = 1200
SOURCE_KINDS = {"source_animation_or_timing": "animation or object timing", "source_camera_or_composition": "camera movement or composition"}
ASSET_KINDS = {"new_or_replacement_shot": "a new or replacement shot", "narration_rewrite": "new narration and matching captions"}


@dataclass
class RepairCandidate:
    key: str
    mutation_type: str
    description: str
    ops: list[EditOp]
    secondary_changes: list[str]
    plan: EditPlan


def _quote(b: TimelineBeat, n: int = 50) -> str:
    t = b.text.strip()
    return f"'{t[:n]}{'...' if len(t) > n else ''}'"


def _overlap(a0: int, a1: int, b0: int, b1: int) -> int:
    return max(0, min(a1, b1) - max(a0, b0))


def build_candidates(finding: AuditFinding, genome: CreativeGenome, video_path: Path, project_id: str) -> tuple[list[RepairCandidate], EditPlan, Any, Any]:
    plan = identity_plan(genome)
    manifest = source_manifest(project_id, video_path, genome.artifact_hash)
    brief = experiment_brief(project_id, genome, "")
    brief.protected_constraints = [c for c in brief.protected_constraints if c.kind.value != "no_new_text"]  # the audit never adds text; keep the gate
    s, e = finding.start_ms, finding.end_ms
    near = [b for b in genome.beats if _overlap(b.start_ms, b.end_ms, s - 500, e + 500) > 0]
    cands: list[RepairCandidate] = []
    rms = audio_rms(video_path)
    payoff_ids = {b.id for b in genome.beats if b.role in (BeatRole.PAYOFF, BeatRole.PROOF)}

    def add(key: str, mtype: str, desc: str, ops: list[EditOp], secondary: list[str]) -> None:
        if validate_ops_scope(ops, brief):
            return
        try:
            new_plan = apply_ops(plan, ops)
        except Exception:  # noqa: BLE001
            return
        if not validate_plan(new_plan, manifest, brief, baseline=plan).ok:
            return
        cands.append(RepairCandidate(key, mtype, desc, ops, secondary, new_plan))

    for b in near:
        idx = plan.segment_index(b.id)
        if idx > 0 and len(plan.segments) > 2:
            add(f"remove_{b.id}", "REMOVE_BEAT", f"Remove the sentence {_quote(b)} ({b.start_ms / 1000:.1f}-{b.end_ms / 1000:.1f}s) with its footage.",
                [RemoveSegment(segment_id=b.id)], [f"the spoken sentence {_quote(b)} and its captions are removed", "everything after it starts earlier"])
        if idx < len(plan.segments) - 1:
            nxt = plan.segments[idx + 1]
            add(f"later_{b.id}", "MOVE_BEAT_LATER", f"Move {_quote(b)} to after the next sentence.", [MoveSegment(segment_id=b.id, after_segment_id=nxt.id)],
                ["two sentences swap order, including their footage, captions and music"])
        if idx > 0:
            after = plan.segments[idx - 2].id if idx >= 2 else None
            add(f"earlier_{b.id}", "MOVE_BEAT_EARLIER", f"Move {_quote(b)} one sentence earlier.", [MoveSegment(segment_id=b.id, after_segment_id=after)],
                ["two sentences swap order, including their footage, captions and music"])
        for g0, g1 in silence_gaps(rms, b.start_ms, b.end_ms, min_gap_ms=300):
            if _overlap(g0, g1, s - 300, e + 300) <= 0:
                continue
            next_is_payoff = idx < len(plan.segments) - 1 and plan.segments[idx + 1].id in payoff_ids
            if g1 >= b.end_ms - 60 and b.end_ms - (g0 + 80) >= 250:
                note = ["this pause sits right before the payoff and may be a deliberate beat"] if next_is_payoff else []
                add(f"trim_end_{b.id}_{g0}", "TRIM_PAUSE", f"Shorten the {(b.end_ms - g0 - 80) / 1000:.2f}s pause at the end of {_quote(b)}.",
                    [TrimSegment(segment_id=b.id, source_in_ms=b.start_ms, source_out_ms=g0 + 80)], ["the footage in the removed pause is dropped"] + note)
            elif g0 <= b.start_ms + 60 and (g1 - 80) - b.start_ms >= 250:
                add(f"trim_start_{b.id}_{g0}", "TRIM_PAUSE", f"Shorten the {(g1 - 80 - b.start_ms) / 1000:.2f}s pause at the start of {_quote(b)}.",
                    [TrimSegment(segment_id=b.id, source_in_ms=g1 - 80, source_out_ms=b.end_ms)], ["the footage in the removed pause is dropped"])
        cs, ce = max(s, b.start_ms), min(e, b.end_ms)
        if 0 < ce - cs < PUNCH_MIN_MS:  # a short flagged moment gets a zoom long enough to read, centered on it and kept inside the sentence
            mid = (cs + ce) // 2
            cs, ce = max(b.start_ms, mid - PUNCH_MIN_MS // 2), min(b.end_ms, mid + PUNCH_MIN_MS // 2)
        rec = manifest.get(VIDEO_ASSET_ID)
        if ce - cs < 700 or not (rec.stream and rec.stream.width and rec.stream.height):
            continue
        w, h = rec.stream.width, rec.stream.height
        cw, ch = int(w / 1.3) // 2 * 2, int(h / 1.3) // 2 * 2
        crops = [("center", "centered", CropRect(x=(w - cw) // 4 * 2, y=(h - ch) // 4 * 2, w=cw, h=ch))]
        try:
            region = locate_motion_region(video_path, cs, ce, w, h, output_aspect=(w, h), zoom=1.3)
            mx, my = region.crop.x + region.crop.w // 2, region.crop.y + region.crop.h // 2
            if abs(mx - w // 2) > w * 0.08 or abs(my - h // 2) > h * 0.08:
                x = max(0, min(w - cw, mx - cw // 2)) // 2 * 2
                y = max(0, min(h - ch, my - ch // 2)) // 2 * 2
                where = ("upper" if my < h * 0.42 else "lower" if my > h * 0.58 else "middle") + " " + ("left" if mx < w * 0.42 else "right" if mx > w * 0.58 else "center")
                crops.append(("motion", f"toward the {where} of the frame, where the most movement is", CropRect(x=x, y=y, w=cw, h=ch)))
        except Exception:  # noqa: BLE001 - the centered punch-in is still offered
            pass
        if cs < b.start_ms + 200:  # never leave a sliver of the sentence outside the zoom, and never drop it
            cs = b.start_ms
        if ce > b.end_ms - 200:
            ce = b.end_ms
        for tag, where_text, crop in crops:
            ops: list[EditOp] = []
            pid = f"{b.id}_zoom"
            if cs > b.start_ms:
                ops.append(TrimSegment(segment_id=b.id, source_in_ms=b.start_ms, source_out_ms=cs))
                ops.append(InsertSegment(after_segment_id=b.id, segment=Segment(id=pid, asset_id=VIDEO_ASSET_ID, source_in_ms=cs, source_out_ms=ce, audio_policy="keep")))
                anchor = pid
            else:
                ops.append(TrimSegment(segment_id=b.id, source_in_ms=b.start_ms, source_out_ms=ce))
                pid = b.id
                anchor = b.id
            ops.append(SetCrop(segment_id=pid, crop=crop))
            if ce < b.end_ms:
                ops.append(InsertSegment(after_segment_id=anchor, segment=Segment(id=f"{b.id}_rest", asset_id=VIDEO_ASSET_ID, source_in_ms=ce, source_out_ms=b.end_ms, audio_policy="keep")))
            add(f"zoom_{tag}_{b.id}", "PUNCH_IN", f"Punch in 1.3x {where_text} during {cs / 1000:.1f}-{ce / 1000:.1f}s (same footage, sound and timing; tighter framing).", ops,
                ["burned-in captions near the frame edges may be cropped during the punch-in"])
    return cands, plan, manifest, brief


CHOOSE_SYSTEM = ("You choose which executable edit, if any, should be tried against a weakness a reviewer found in a short video. Be strict: "
                 "an edit that is merely similar to the proposed repair does not implement it, and an edit that does not plausibly reduce the "
                 "specific weakness should not be tried. Trying an edit costs a render and a fresh review; choosing nothing is acceptable.")

CHOOSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "choice": {"type": ["string", "null"]},
        "implements_proposal": {"type": "boolean"},
        "reason": {"type": "string"},
        "route_if_none": {"type": "string", "enum": ["A", "B", "C"]},
        "needed_capability": {"type": "string"},
    },
    "required": ["choice", "implements_proposal", "reason", "route_if_none", "needed_capability"],
}


@traced("map_finding_to_repair", kind="agent")
def map_repair(finding: AuditFinding, genome: CreativeGenome, video_path: Path, project_id: str, planner: TextPlannerProvider | None, source_project: dict[str, Any] | None,
               exclude_keys: tuple[str, ...] = (), prior_attempts: tuple[str, ...] = (), constraints: tuple[str, ...] = (), objective: str = "",
               allowed_types: tuple[str, ...] | None = None) -> tuple[AuditRepair, RepairCandidate | None]:
    kind = finding.repair_kind
    cands, _, _, _ = build_candidates(finding, genome, video_path, project_id)
    excluded_by_constraint = [c.key for c in cands if allowed_types is not None and c.mutation_type not in allowed_types]
    cands = [c for c in cands if c.key not in exclude_keys and c.key not in excluded_by_constraint]
    keep = finding.keep_unchanged
    proposal_blocked = kind in SOURCE_KINDS or kind in ASSET_KINDS or kind == "caption_or_text"
    blocked = _non_runnable(finding, kind, source_project) if proposal_blocked else None
    if blocked is not None and not cands:
        return blocked, None
    if not cands:
        why = "no validated existing-video edit fits this interval"
        if exclude_keys:
            why += f" that has not already been tried ({len(exclude_keys)} excluded)"
        if excluded_by_constraint:
            why += f"; {len(excluded_by_constraint)} candidate edits are not allowed by the run constraints"
        return AuditRepair(summary=finding.proposed_repair, route="B", runnable=False, keep_unchanged=keep, why_not_runnable=why,
                           required_materials=_source_materials(source_project), dependency_state=_source_state(source_project)), None
    chosen: RepairCandidate | None = None
    implements = False
    reason = "no planner configured; not choosing automatically"
    needed = ""
    route_if_none = "B"
    if planner is not None:
        beats = "\n".join(f"{b.start_ms / 1000:.1f}-{b.end_ms / 1000:.1f}s: {b.text}" for b in genome.beats)
        options = "\n".join(f"- {c.key}: {c.description} Side effects: {'; '.join(c.secondary_changes)}" for c in cands)
        user = (
            f"A reviewer found this weakness at {finding.start_ms / 1000:.2f}-{finding.end_ms / 1000:.2f}s: {finding.weakness}\n"
            f"Proposed repair: {finding.proposed_repair}\nMust stay unchanged: {'; '.join(keep) or 'not specified'}\n"
            f"The video is a flattened MP4 with burned-in captions; its sentences with times:\n{beats}\n\n"
            f"Edits that can be executed on the existing file:\n{options}\n\n"
            + (f"Creator objective: {objective}\n" if objective else "")
            + (f"Creator constraints: {'; '.join(constraints)}\n" if constraints else "")
            + ("Earlier attempts on this video and what the fresh review found:\n" + "\n".join(f"- {a}" for a in prior_attempts) + "\n"
               "Do not choose an edit that repeats what already failed unless the evidence says the failure was unrelated.\n\n" if prior_attempts else "")
            + (f"The proposed repair itself cannot be executed on this file ({blocked.why_not_runnable}). No edit below implements it, so "
               "implements_proposal must be false. You may still choose an edit that reduces this specific weakness by a different means.\n"
               if blocked is not None else
               "Prefer the edit that implements the proposed repair (implements_proposal true). If none does, you may choose an edit that "
               "reduces this specific weakness by a different means (implements_proposal false).\n")
            + "Choose the single edit key to try, without breaking what must stay unchanged, or null if no edit plausibly helps. "
            "If null, say which route the repair needs (B = needs the original project: animation timing, camera, captions; C = needs new footage "
            "or narration) and name the missing capability."
        )
        try:
            res = planner.complete_json(CHOOSE_SYSTEM, user, CHOOSE_SCHEMA)
            key = res.data.get("choice")
            chosen = next((c for c in cands if c.key == key), None)
            implements = bool(res.data.get("implements_proposal")) and blocked is None
            reason = str(res.data.get("reason", ""))[:400]
            needed = str(res.data.get("needed_capability", ""))[:200]
            route_if_none = res.data.get("route_if_none") if res.data.get("route_if_none") in ("A", "B", "C") else "B"
        except ProviderError as exc:
            reason = f"planner failed: {str(exc)[:120]}"
    offered = [f"{c.key}: {c.description}" for c in cands]
    if chosen is None and blocked is not None:
        return blocked.model_copy(update={"selection_reason": reason, "candidates_offered": offered}), None
    if chosen is None:
        return AuditRepair(summary=finding.proposed_repair, route="C" if route_if_none == "C" else "B", runnable=False, keep_unchanged=keep,
                           why_not_runnable=f"no existing-video edit implements it: {reason}", required_materials=[needed] if needed else _source_materials(source_project),
                           dependency_state=_source_state(source_project) if route_if_none != "C" else "no generation or narration provider configured for automated use",
                           selection_reason=reason, candidates_offered=offered), None
    return AuditRepair(summary=finding.proposed_repair, route="A", runnable=True, mutation_type=chosen.mutation_type, edit_description=chosen.description,
                       keep_unchanged=keep, secondary_changes=chosen.secondary_changes, dependency_state="executable now on the existing file",
                       selection_reason=reason, candidates_offered=offered, implements_proposal=implements,
                       proposal_route=(blocked.route if blocked is not None else ("A" if implements else None)),
                       proposal_needs=(blocked.why_not_runnable or "") if blocked is not None else ""), chosen


def _non_runnable(finding: AuditFinding, kind: str, source_project: dict[str, Any] | None) -> AuditRepair:
    if kind in ASSET_KINDS:
        return AuditRepair(summary=finding.proposed_repair, route="C", runnable=False, keep_unchanged=finding.keep_unchanged,
                           why_not_runnable=f"needs {ASSET_KINDS[kind]}", required_materials=[ASSET_KINDS[kind]],
                           dependency_state="no generation or narration provider is enabled for automated repairs")
    what = SOURCE_KINDS.get(kind, "caption text and styling (captions are burned into the pixels)")
    return AuditRepair(summary=finding.proposed_repair, route="B", runnable=False, keep_unchanged=finding.keep_unchanged,
                       why_not_runnable=f"a flattened MP4 cannot change {what} independently",
                       required_materials=_source_materials(source_project) or [f"the original project with editable {what}"], dependency_state=_source_state(source_project))


def inspect_source_project(video_path: Path) -> dict[str, Any] | None:
    """Read-only look for the production that rendered this file (Curio layout: production.json next to the render)."""
    prod = video_path.parent / "production.json"
    if not prod.exists():
        return None
    try:
        data = json.loads(prod.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"path": str(prod), "readable": False}
    keys = sorted(data.keys()) if isinstance(data, dict) else []
    words = sorted(p.name for p in video_path.parent.glob("*-words.json"))[:4]
    plates = data.get("plates") if isinstance(data, dict) else None
    return {"path": str(prod), "readable": True, "top_level_fields": keys[:30], "has_script": "script" in keys, "has_plates": bool(plates),
            "plate_count": len(plates) if isinstance(plates, list) else None, "word_timing_files": words}


def _source_materials(src: dict[str, Any] | None) -> list[str]:
    if not src:
        return ["the original editing or animation project for this video (not found next to the file)"]
    out = [f"production file {src['path']}"]
    if src.get("has_plates"):
        out.append(f"{src.get('plate_count')} scene plates referenced in the production file")
    if src.get("word_timing_files"):
        out.append("narration word timings: " + ", ".join(src["word_timing_files"]))
    return out


def _source_state(src: dict[str, Any] | None) -> str:
    if not src:
        return "source project not available; the repair can be described but not executed"
    return "source project found (read-only); re-rendering it requires the original production pipeline, which DirectorLoop does not run"
