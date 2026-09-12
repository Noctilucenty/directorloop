"""Mechanical checks on the rendered artifact and constraint checks on the plan.

Everything here is deterministic and labeled MECHANICAL. These are the strongest
checks in the system because they measure the file, not an opinion about it.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..domain.assets import AssetManifest, AssetOrigin
from ..domain.brief import ConstraintKind, CreativeBrief
from ..domain.edit_plan import EditPlan
from ..domain.evaluation import ConstraintCheck, MechanicalCheck
from ..media.captions import layout_caption
from ..media.probe import MediaError, has_faststart, inspect_media
from ..media.validate import _interval_preserved


def run_mechanical_checks(artifact_path: Path, plan: EditPlan, manifest: AssetManifest, brief: CreativeBrief) -> list[MechanicalCheck]:
    checks: list[MechanicalCheck] = []
    try:
        info = inspect_media(artifact_path)
        checks.append(MechanicalCheck(id="decodes", passed=True, detail=f"{info.video_codec} {info.width}x{info.height}"))
    except MediaError as exc:
        checks.append(MechanicalCheck(id="decodes", passed=False, detail=str(exc)))
        return checks

    expected = plan.timeline_duration_ms()
    dur = info.duration_ms or 0
    checks.append(
        MechanicalCheck(id="duration_matches_plan", passed=abs(dur - expected) <= 150, value=dur, threshold=expected, detail=f"{dur} ms vs plan {expected} ms")
    )
    checks.append(
        MechanicalCheck(
            id="duration_within_brief",
            passed=brief.min_duration_ms() <= dur <= brief.max_duration_ms(),
            value=dur,
            threshold=f"{brief.min_duration_ms()}-{brief.max_duration_ms()}",
            detail=f"brief allows {brief.min_duration_ms()}-{brief.max_duration_ms()} ms",
        )
    )
    checks.append(
        MechanicalCheck(
            id="geometry",
            passed=(info.width, info.height) == (plan.output.width, plan.output.height),
            value=f"{info.width}x{info.height}",
            threshold=f"{plan.output.width}x{plan.output.height}",
        )
    )
    checks.append(MechanicalCheck(id="has_audio_stream", passed=info.has_audio, detail="narration/music require an audio stream"))
    checks.append(MechanicalCheck(id="browser_progressive", passed=has_faststart(artifact_path), severity="warning", detail="moov atom before mdat"))

    unauthorized = [a for a in plan.asset_ids() if not manifest.has(a)]
    checks.append(MechanicalCheck(id="assets_authorized", passed=not unauthorized, detail=", ".join(unauthorized) or "all assets in manifest"))
    generated = [s.id for s in plan.segments if manifest.has(s.asset_id) and manifest.get(s.asset_id).origin == AssetOrigin.GENERATED]
    gen_ok = not generated or (brief.generation_permitted and not brief.has_constraint(ConstraintKind.NO_GENERATION))
    checks.append(MechanicalCheck(id="generation_authorized", passed=gen_ok, detail=", ".join(generated) or "no generated segments"))

    if plan.narration is not None and manifest.has(plan.narration.asset_id):
        rec = manifest.get(plan.narration.asset_id)
        end = (rec.duration_ms or 0) + plan.narration.offset_ms
        checks.append(
            MechanicalCheck(id="narration_not_cut", passed=end <= expected + 100, value=end, threshold=expected, detail="narration must end before the video ends")
        )

    for cap in plan.captions:
        layout = layout_caption(cap, plan.output)
        checks.append(MechanicalCheck(id=f"caption_fit:{cap.id}", passed=layout.fits, value=len(layout.lines), threshold=3, detail=f"{len(layout.lines)} line(s)"))
        checks.append(
            MechanicalCheck(id=f"caption_within_video:{cap.id}", passed=cap.end_ms <= expected, value=cap.end_ms, threshold=expected)
        )
        checks.append(
            MechanicalCheck(
                id=f"caption_reading_speed:{cap.id}",
                passed=layout.chars_per_second <= 25,
                severity="warning",
                value=round(layout.chars_per_second, 1),
                threshold=25,
                detail="configurable heuristic, not a universal truth",
            )
        )
        checks.append(
            MechanicalCheck(id=f"caption_min_duration:{cap.id}", passed=cap.duration_ms >= 700, severity="warning", value=cap.duration_ms, threshold=700)
        )
    return checks


def _restates_approved(text: str, approved_texts: list[str] | None) -> bool:
    """A caption restates an approved claim when most of its content words come from one approved claim."""
    if not approved_texts:
        return False
    words = {w for w in re.findall(r"[a-z0-9']+", text.lower()) if len(w) > 2}
    if not words:
        return False
    for claim in approved_texts:
        cw = {w for w in re.findall(r"[a-z0-9']+", claim.lower()) if len(w) > 2}
        if len(words & cw) >= 0.7 * len(words):
            return True
    return False


def run_constraint_checks(
    plan: EditPlan, baseline: EditPlan | None, brief: CreativeBrief, manifest: AssetManifest, approved_texts: list[str] | None = None
) -> list[ConstraintCheck]:
    out: list[ConstraintCheck] = []
    for c in brief.protected_constraints:
        passed = True
        detail = "unchanged"
        if c.kind == ConstraintKind.KEEP_NARRATION:
            passed = baseline is None or plan.narration == baseline.narration
            detail = "narration track identical to baseline" if passed else "narration changed"
        elif c.kind == ConstraintKind.KEEP_MUSIC:
            passed = baseline is None or plan.music == baseline.music
            detail = "music track identical to baseline" if passed else "music changed"
        elif c.kind == ConstraintKind.NO_NEW_TEXT:
            before = {cap.text for cap in baseline.captions} if baseline else {cap.text for cap in plan.captions}
            new = [cap.text for cap in plan.captions if cap.text not in before]
            passed = not new
            detail = ("no new on-screen text" if baseline else "baseline is the reference") if passed else f"new text: {new}"
        elif c.kind == ConstraintKind.NO_GENERATION:
            gen = [s.id for s in plan.segments if manifest.has(s.asset_id) and manifest.get(s.asset_id).origin == AssetOrigin.GENERATED]
            passed = not gen
            detail = "no generated footage" if passed else f"generated segments: {gen}"
        elif c.kind == ConstraintKind.MAX_DURATION_MS:
            passed = plan.timeline_duration_ms() <= (c.value_ms or 0)
            detail = f"{plan.timeline_duration_ms()} ms <= {c.value_ms} ms" if passed else f"{plan.timeline_duration_ms()} ms exceeds {c.value_ms} ms"
        elif c.kind == ConstraintKind.MIN_DURATION_MS:
            passed = plan.timeline_duration_ms() >= (c.value_ms or 0)
        elif c.kind in (ConstraintKind.PRESERVE_INTERVAL, ConstraintKind.KEEP_SILENCE_INTERVAL):
            passed = baseline is None or _interval_preserved(plan, baseline, c.start_ms or 0, c.end_ms or 0)
            detail = f"interval {c.start_ms}-{c.end_ms} ms preserved" if passed else f"interval {c.start_ms}-{c.end_ms} ms altered"
        elif c.kind == ConstraintKind.PRESERVE_PRODUCT_APPEARANCE:
            gen = [s.id for s in plan.segments if manifest.has(s.asset_id) and manifest.get(s.asset_id).origin == AssetOrigin.GENERATED]
            passed = not gen
            detail = "only recorded/fixture footage of the product" if passed else f"generated footage may alter appearance: {gen}"
        elif c.kind == ConstraintKind.NO_NEW_FACTS:
            before = {cap.text for cap in baseline.captions} if baseline else {cap.text for cap in plan.captions}
            new = [cap.text for cap in plan.captions if cap.text not in before]
            unsupported = [n for n in new if not _restates_approved(n, approved_texts)]
            passed = not unsupported  # new on-screen text must restate an approved source-truth claim
            if passed:
                detail = "baseline is the reference" if not baseline else ("no new textual claims" if not new else f"new text restates approved claims: {new}")
            else:
                detail = f"new textual claims are not in the approved source truth: {unsupported}"
        elif c.kind == ConstraintKind.NO_CTA:
            cta_words = ("subscribe", "follow", "link in bio", "buy now", "sign up")
            hits = [cap.text for cap in plan.captions if any(w in cap.text.lower() for w in cta_words)]
            passed = not hits
            detail = "no call to action" if passed else f"CTA text found: {hits}"
        out.append(ConstraintCheck(constraint_id=c.id, kind=str(c.kind), passed=passed, detail=detail))
    return out
