"""Frozen comparison rubric and two-order pairwise judgments by dimension.

The rubric (questions, sampling, verdict rules) is serialized and hashed before any C is chosen, and every pair (A-B, C-A,
C-B) is judged with the same instruction, sampling and schema. The judge never learns which version is the director's.
Each comparison runs in both presentation orders; when the orders disagree the verdict is 'unstable', never averaged away.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import weave

from ..audit.revise import _pair
from ..media.frames import extract_frame, sample_frames
from ..observability.weave_ops import set_display_name, traced
from ..providers.base import MediaProbeProvider, ProbeMedia, ProviderError
from .models import DeclaredContext, DimensionJudgment, PairComparison

RUBRIC_VERSION = "abc-rubric-v2"  # v2: blunt reasons; denser whole-video sampling (v1 gave unstable verdicts on 7 of 8 dimensions for a trim)

WHOLE_DIMENSIONS: dict[str, str] = {
    "opening_interest": "In the first seconds, which version gives this viewer a clearer reason to keep watching?",
    "comprehension": "By the end, which version leaves this viewer understanding what happened and why?",
    "information_progression": "Which version adds new information or meaning at a better rate, without confusing jumps or needless repetition?",
    "pacing": "Which version's pacing better fits what is shown and said at each moment (neither rushed nor dragging)?",
    "visual_narration_alignment": "Which version's pictures better show what is being said at the same moment?",
    "emotional_effect": "Which version creates more curiosity, anticipation, surprise or delight at the right moments?",
    "payoff": "Which version lands its final point or payoff more clearly?",
    "memorability": "Which version is this viewer more likely to remember or retell?",
}
REGION_DIMENSIONS: dict[str, str] = {
    "clarity": "In this stretch, which version makes what is happening easier to follow?",
    "curiosity_or_anticipation": "In this stretch, which version builds or keeps more curiosity or anticipation?",
    "visual_narration_alignment": "In this stretch, which version's pictures better show what is being said?",
    "pacing": "In this stretch, which version's timing better fits what is shown and said?",
}
SAMPLING = {"whole": {"fps": 2, "max_frames": 24, "width": 320, "words": "timed words of the rendered speech"},
            "region": {"fps": 3, "max_frames": 10, "width": 384, "words": "timed words inside the stretch"}}
VERDICT_RULES = ("Each dimension is asked in both presentation orders (repeats=1). Same label in both orders -> that label; 'same' in both -> "
                 "same; different answers -> unstable; no valid answer -> unclear. Votes are kept.")
CORE_DIMENSIONS = ("comprehension", "payoff")

JUDGE_INSTRUCTION = """Frames with timestamps under 100 s belong to Version 1; frames at 100 s or later belong to Version 2 (subtract 100 s).
Both versions present the same idea. They are {scope_text}.
The viewing situation for both: {encounter}. The intended viewer: {audience}. The purpose of the video: {objective}.{payoff}
You did not hear the audio; the words are a transcript of the rendered speech with times.
Answer every question with '1', '2', 'same' (no meaningful difference) or 'unclear' (these frames and words cannot tell), and one short, blunt reason tied to what you saw or read: say plainly what is worse in the losing version, not polite generalities.
Questions:
{questions}
Finally, overall: which version better serves this viewer and purpose {overall_text}?"""


def rubric_record(context: DeclaredContext, reviewer: str, selector: str) -> dict[str, Any]:
    body = {"version": RUBRIC_VERSION, "whole_dimensions": WHOLE_DIMENSIONS, "region_dimensions": REGION_DIMENSIONS, "sampling": SAMPLING,
            "verdict_rules": VERDICT_RULES, "core_dimensions": list(CORE_DIMENSIONS), "instruction_template": JUDGE_INSTRUCTION,
            "context": context.model_dump(), "reviewer": reviewer, "selector": selector}
    body["sha256"] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return body


def words_text(words: list[Any], start_ms: int = 0, end_ms: int | None = None) -> str:
    hi = end_ms if end_ms is not None else 10**9
    sel = [w for w in words if start_ms <= (w.start_ms + w.end_ms) // 2 < hi]
    return " ".join(f"{(w.start_ms - start_ms) / 1000:.1f}s {w.text}" for w in sel) or "(no speech)"


def whole_media(path: Path, duration_ms: int, words: list[Any]) -> ProbeMedia:
    count = max(8, min(SAMPLING["whole"]["max_frames"], int(duration_ms / 1000 * SAMPLING["whole"]["fps"])))
    frames = sample_frames(path, duration_ms, count=count, max_width=SAMPLING["whole"]["width"])
    return ProbeMedia(kind="frames", duration_ms=duration_ms, frames=frames, transcript=words_text(words))


def region_media(path: Path, start_ms: int, end_ms: int, words: list[Any]) -> ProbeMedia:
    """Frames re-timed to start at 0 so both versions of a stretch are presented on the same clock."""
    from ..media.frames import SampledFrame

    step = 1000.0 / SAMPLING["region"]["fps"]
    n = max(1, min(SAMPLING["region"]["max_frames"], int((end_ms - start_ms) / step)))
    times = [int(start_ms + (i + 0.5) * (end_ms - start_ms) / n) for i in range(n)]
    frames = []
    for t in times:
        fr = extract_frame(path, t, SAMPLING["region"]["width"])
        frames.append(SampledFrame(timestamp_ms=t - start_ms, jpeg=fr.jpeg, width=fr.width, height=fr.height))
    return ProbeMedia(kind="frames", duration_ms=end_ms - start_ms, frames=frames, transcript=words_text(words, start_ms, end_ms))


def _schema(dims: list[str]) -> dict[str, Any]:
    ans = {"type": "object", "properties": {"choice": {"type": "string", "enum": ["1", "2", "same", "unclear"]}, "reason": {"type": "string"}}, "required": ["choice", "reason"]}
    return {"type": "object", "properties": {"answers": {"type": "object", "properties": {d: ans for d in dims}, "required": dims}, "overall": ans},
            "required": ["answers", "overall"]}


@traced("compare_pair", kind="llm")
def compare_pair(provider: MediaProbeProvider, first_label: str, first: ProbeMedia, second_label: str, second: ProbeMedia, context: DeclaredContext,
                 scope: str, region: dict[str, Any] | None = None) -> PairComparison:
    set_display_name(f"compare_{first_label}_vs_{second_label}_{scope}")
    dims = WHOLE_DIMENSIONS if scope == "whole" else REGION_DIMENSIONS
    scope_text = "two complete versions" if scope == "whole" else "the same stretch of the story in each version"
    instruction = JUDGE_INSTRUCTION.format(
        scope_text=scope_text, encounter=context.encounter, audience=context.audience, objective=context.objective,
        payoff=f" The expected payoff: {context.expected_payoff}." if context.expected_payoff else "",
        questions="\n".join(f"- {k}: {q}" for k, q in dims.items()), overall_text="as a whole" if scope == "whole" else "in this stretch")
    schema = _schema(list(dims))
    orders = [(first_label, first, second_label, second), (second_label, second, first_label, first)]

    def one(order: tuple[str, ProbeMedia, str, ProbeMedia]) -> tuple[str, str, dict[str, Any] | None, str]:
        l1, m1, l2, m2 = order
        try:
            return l1, l2, provider.judge_json(_pair(m1, m2), instruction, schema).data, ""
        except ProviderError as exc:
            return l1, l2, None, str(exc)[:120]

    with weave.ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(one, orders))
    failed = sum(1 for r in results if r[2] is None)

    def judge(key: str, getter: Any) -> DimensionJudgment:
        votes: dict[str, int] = {}
        outcomes: list[str] = []
        reasons: list[str] = []
        for l1, l2, data, err in results:
            if data is None:
                reasons.append(f"[{l1} shown first] call failed: {err}")
                continue
            item = getter(data) or {}
            choice = item.get("choice") if isinstance(item, dict) else None
            label = {"1": l1, "2": l2, "same": "same"}.get(str(choice))
            if label is None:
                outcomes.append("unclear")
                reasons.append(f"[{l1} shown first] unclear: {str(item.get('reason', '') if isinstance(item, dict) else '')[:160]}")
                continue
            outcomes.append(label)
            votes[label] = votes.get(label, 0) + 1
            reasons.append(f"[{l1} shown first] {label}: {str(item.get('reason', ''))[:180]}")
        valid = [o for o in outcomes if o != "unclear"]
        if not valid:
            verdict = "unclear"
        elif len(valid) == len(results) and len(set(valid)) == 1:
            verdict = valid[0]
        elif len(set(valid)) == 1 and len(valid) < len(results):
            verdict = "unstable"  # one order failed or was unclear: a single answer is not a stable verdict
        else:
            verdict = "unstable"
        return DimensionJudgment(dimension=key, verdict=verdict, votes=votes, reasons=reasons)

    dim_j = [judge(d, lambda data, d=d: (data.get("answers") or {}).get(d)) for d in dims]
    overall = judge("overall", lambda data: data.get("overall"))
    return PairComparison(first=first_label, second=second_label, scope="whole" if scope == "whole" else "region", region=region, dimensions=dim_j,  # type: ignore[arg-type]
                          overall=overall, calls=len(results), failed_calls=failed, rubric_version=RUBRIC_VERSION)
