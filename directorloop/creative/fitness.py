"""Offline creative fitness for experiment arms. Not retention.

Components stay separate and labeled:
- MODEL EVAL: hook topic comprehension (first 3 s clip), core-message comprehension (full video),
  pairwise "keep watching" preference against control with both presentation orders.
- MECHANICAL / DERIVED FROM PLAN: payoff time, context before payoff, first visual change,
  longest static stretch, duration.
- Hard gates: the existing mechanical and protected-constraint checks.
The per-video question suite is written once from the ORIGINAL transcript by a text-only model,
frozen by hash before any variant exists, and checked with a no-media control. Viewer models only
ever receive media derived from the rendered arm.
"""

from __future__ import annotations

import json
import random
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import weave

from ..domain.assets import AssetManifest
from ..domain.brief import CreativeBrief
from ..domain.creative import (
    BeatRole,
    CreativeFitness,
    CreativeGenome,
    EvidenceClass,
    FitnessComponent,
)
from ..domain.edit_plan import EditPlan
from ..domain.evaluation import LeakageControlResult
from ..domain.ids import sha256_file, sha256_json
from ..domain.truth import EvaluationSuite, EvidenceModality, ProbeOption, ProbeQuestion, SuiteSplit
from ..evals.leakage import run_no_media_control
from ..evals.mechanical import run_constraint_checks, run_mechanical_checks
from ..evals.probes import run_probe_trials
from ..evals.scoring import score_answers
from ..media.frames import sample_frames
from ..media.probe import FFMPEG, MediaError
from ..media.transcribe import transcribe
from ..observability.weave_ops import set_display_name, traced
from ..providers.base import MediaProbeProvider, ProbeMedia, ProviderError, TextPlannerProvider
from .signals import detect_cuts, first_visual_change_ms, motion_curve, static_spans

HOOK_MS = 3000
SUITE_VERSION = "1"

SUITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "topic_question": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "correct": {"type": "string"}, "distractors": {"type": "array", "items": {"type": "string"}}},
            "required": ["text", "correct", "distractors"],
        },
        "message_questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "correct": {"type": "string"}, "distractors": {"type": "array", "items": {"type": "string"}}, "about": {"type": "string"}},
                "required": ["text", "correct", "distractors", "about"],
            },
        },
    },
    "required": ["topic_question", "message_questions"],
}

SUITE_SYSTEM = (
    "You write multiple-choice comprehension questions for a short video, using only its transcript. "
    "The questions will be answered later by a different model that watches the video and never sees the transcript. "
    "Write neutral questions that do not reveal their answers, one correct option supported by the transcript, and three "
    "plausible wrong options of similar length and style. Never make the correct option the longest or most detailed one."
)


def _suite_path(cache_dir: Path, artifact_hash: str) -> Path:
    return cache_dir / f"suite_{artifact_hash[:24]}_v{SUITE_VERSION}.json"


@traced("freeze_video_suite", kind="tool")
def build_video_suite(genome: CreativeGenome, planner: TextPlannerProvider, cache_dir: Path, video_id: str) -> EvaluationSuite:
    """Written once from the original video's transcript, then frozen (cached by artifact hash)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _suite_path(cache_dir, genome.artifact_hash)
    if path.exists():
        return EvaluationSuite.model_validate_json(path.read_text(encoding="utf-8"))
    transcript = "\n".join(f"{b.start_ms / 1000:.1f}s: {b.text}" for b in genome.beats)
    user = (
        "Transcript of the video:\n" + transcript + "\n\n"
        "Write (1) one topic question: what the video is about, answerable from its opening seconds; and "
        "(2) three message questions about its main surprising point, the reason or mechanism it gives, and what a viewer "
        "should take away. Return JSON."
    )
    res = planner.complete_json(SUITE_SYSTEM, user, SUITE_SCHEMA, temperature=0.2)
    rng = random.Random(genome.artifact_hash)
    ns = ProbeOption(id="not_shown", text="Not shown or cannot tell from the video")

    def question(qid: str, item: dict[str, Any], guard: bool) -> ProbeQuestion:
        texts = [str(item["correct"]).strip()] + [str(t).strip() for t in item.get("distractors", [])][:3]
        ids = ["o1", "o2", "o3", "o4"][: len(texts)]
        order = list(range(len(texts)))
        rng.shuffle(order)
        options = [ProbeOption(id=ids[k], text=texts[i]) for k, i in enumerate(order)]
        correct_id = ids[order.index(0)]
        return ProbeQuestion(id=qid, text=str(item["text"]).strip(), modality=EvidenceModality.EITHER, options=options + [ns], correct_option_id=correct_id, regression_guard=guard)

    data = res.data
    questions = [question("q_topic", data["topic_question"], False)]
    for i, item in enumerate(data.get("message_questions", [])[:3]):
        questions.append(question(f"q_message_{i + 1}", item, True))
    suite = EvaluationSuite(id=f"suite_{video_id}", version=1, split=SuiteSplit.DEV, story_family=video_id, questions=questions, scoring="exact_option", frozen=True)
    path.write_text(suite.model_dump_json(indent=2), encoding="utf-8")
    return suite


def load_or_run_leakage(suite: EvaluationSuite, provider: MediaProbeProvider, cache_dir: Path, trials: int = 3) -> LeakageControlResult:
    path = cache_dir / f"leakage_{suite.content_hash()[:24]}_{provider.capability.model.replace('/', '_')}.json"
    if path.exists():
        return LeakageControlResult.model_validate_json(path.read_text(encoding="utf-8"))
    result = run_no_media_control(provider, suite, trials=trials)
    path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return result


def render_hook_clip(artifact: Path, out_dir: Path, hook_ms: int = HOOK_MS) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{artifact.stem[:40]}_hook{hook_ms}.mp4"
    if out.exists() and out.stat().st_size > 0:
        return out
    tmp = out.with_suffix(".tmp.mp4")
    cmd = [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(artifact), "-t", f"{hook_ms / 1000:.3f}",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", str(tmp)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    if proc.returncode != 0:
        raise MediaError(f"hook clip failed: {proc.stderr[-200:]}")
    tmp.replace(out)
    return out


@dataclass
class ArmMedia:
    label: str
    artifact: Path
    duration_ms: int
    hook_clip: Path
    full_media: ProbeMedia
    hook_media: ProbeMedia
    opening_signature: str  # sha of the first HOOK_MS of the plan (same plan prefix -> identical opening)


def _transcript_text(path: Path) -> str:
    t = transcribe(path)
    return t.text


def prepare_arm_media(label: str, artifact: Path, duration_ms: int, plan: EditPlan, cache_dir: Path) -> ArmMedia:
    hook = render_hook_clip(artifact, cache_dir / "hooks")
    tcache = cache_dir / f"transcript_{sha256_file(artifact)[:24]}.txt"
    if tcache.exists():
        full_text = tcache.read_text(encoding="utf-8")
    else:
        full_text = _transcript_text(artifact)
        tcache.write_text(full_text, encoding="utf-8")
    hcache = cache_dir / f"transcript_{sha256_file(hook)[:24]}.txt"
    if hcache.exists():
        hook_text = hcache.read_text(encoding="utf-8")
    else:
        hook_text = _transcript_text(hook)
        hcache.write_text(hook_text, encoding="utf-8")
    full = ProbeMedia(kind="frames", duration_ms=duration_ms, frames=sample_frames(artifact, duration_ms, count=8, max_width=512), transcript=full_text)
    hook_media = ProbeMedia(kind="frames", duration_ms=HOOK_MS, frames=sample_frames(hook, HOOK_MS, count=4, max_width=512), transcript=hook_text)
    prefix: list[tuple[str, int, int]] = []
    t = 0
    for s in plan.segments:
        if t >= HOOK_MS:
            break
        take = min(s.duration_ms, HOOK_MS - t)
        prefix.append((s.asset_id, s.source_in_ms, s.source_in_ms + take))
        t += take
    return ArmMedia(label=label, artifact=artifact, duration_ms=duration_ms, hook_clip=hook, full_media=full, hook_media=hook_media, opening_signature=sha256_json(prefix))


PAIR_SCHEMA = {
    "type": "object",
    "properties": {"choice": {"type": "string", "enum": ["1", "2", "no_preference"]}, "reason": {"type": "string"}},
    "required": ["choice", "reason"],
}


def _pair_media(a: ProbeMedia, b: ProbeMedia) -> ProbeMedia:
    """Two videos in one request: frames of version 1 then version 2, each with its own transcript block."""
    from ..media.frames import SampledFrame

    frames: list[SampledFrame] = []
    for f in a.frames:
        frames.append(SampledFrame(timestamp_ms=f.timestamp_ms, jpeg=f.jpeg, width=f.width, height=f.height))
    for f in b.frames:
        frames.append(SampledFrame(timestamp_ms=100000 + f.timestamp_ms, jpeg=f.jpeg, width=f.width, height=f.height))
    transcript = f"Version 1 transcript: {a.transcript or '(no speech)'}\nVersion 2 transcript: {b.transcript or '(no speech)'}"
    return ProbeMedia(kind="frames", duration_ms=max(a.duration_ms, b.duration_ms), frames=frames, transcript=transcript)


def pairwise_preference(provider: MediaProbeProvider, control: ProbeMedia, variant: ProbeMedia, question: str, repeats: int = 2) -> tuple[float | None, int, list[str]]:
    """Share of calls preferring the variant (ties count half), over both presentation orders."""
    calls: list[tuple[ProbeMedia, bool]] = []
    for _ in range(repeats):
        calls.append((_pair_media(control, variant), False))  # variant is version 2
        calls.append((_pair_media(variant, control), True))  # variant is version 1
    instruction = (
        f"Frames with timestamps under 100 s belong to Version 1; frames at 100 s and later belong to Version 2 "
        f"(subtract 100 s for their real time). {question} Answer '1', '2' or 'no_preference' and give one short reason."
    )
    score = 0.0
    valid = 0
    reasons: list[str] = []

    def one(call: tuple[ProbeMedia, bool]) -> tuple[str | None, str]:
        media, variant_first = call
        try:
            res = provider.judge_json(media, instruction, PAIR_SCHEMA)
        except ProviderError as exc:
            return None, str(exc)[:80]
        choice = str(res.data.get("choice", ""))
        if choice not in ("1", "2", "no_preference"):
            return None, "invalid choice"
        if choice == "no_preference":
            return "tie", str(res.data.get("reason", ""))[:120]
        picked_variant = (choice == "1") == variant_first
        return ("variant" if picked_variant else "control"), str(res.data.get("reason", ""))[:120]

    with weave.ThreadPoolExecutor(max_workers=len(calls)) as ex:
        for outcome, reason in ex.map(one, calls):
            if outcome is None:
                continue
            valid += 1
            score += 1.0 if outcome == "variant" else 0.5 if outcome == "tie" else 0.0
            reasons.append(f"{outcome}: {reason}")
    return (score / valid if valid else None), valid, reasons


def plan_timings(plan: EditPlan, genome: CreativeGenome) -> dict[str, int | None]:
    """Where the first proof/payoff lands in THIS arm's timeline, and how much context precedes it (exact, from the plan)."""
    by_id = {b.id: b for b in genome.beats}
    t = 0
    payoff_at: int | None = None
    context_before = 0
    for s in plan.segments:
        beat = by_id.get(s.id.removesuffix("_punch"))
        if beat is not None and payoff_at is None:
            if beat.role in (BeatRole.PROOF, BeatRole.PAYOFF):
                payoff_at = t
            elif beat.role in (BeatRole.SETUP, BeatRole.CONTEXT, BeatRole.PROBLEM) and t > 0:
                context_before += s.duration_ms
        t += s.duration_ms
    return {"payoff_ms": payoff_at, "context_before_payoff_ms": context_before if payoff_at is not None else None, "duration_ms": t}


@traced("evaluate_arm", kind="agent")
def evaluate_arm(
    *,
    label: str,
    arm_id: str,
    media: ArmMedia,
    control_media: ArmMedia | None,
    plan: EditPlan,
    control_plan: EditPlan,
    genome: CreativeGenome,
    manifest: AssetManifest,
    brief: CreativeBrief,
    suite: EvaluationSuite,
    weak_question_ids: set[str],
    provider: MediaProbeProvider,
    trials: int,
    render_ms: int | None,
) -> tuple[CreativeFitness, dict[str, Any]]:
    set_display_name(f"evaluate_{label}")
    t0 = time.monotonic()
    detail: dict[str, Any] = {"label": label}
    mechanical = run_mechanical_checks(media.artifact, plan, manifest, brief)
    constraints = run_constraint_checks(plan, control_plan if control_media is not None else None, brief, manifest)
    gates_ok = all(m.passed for m in mechanical if m.severity == "critical") and all(c.passed for c in constraints)
    gate_notes = [f"{m.id}: {m.detail}" for m in mechanical if not m.passed and m.severity == "critical"] + [f"{c.constraint_id}: {c.detail}" for c in constraints if not c.passed]

    topic_suite = EvaluationSuite(id=suite.id + "_topic", story_family=suite.story_family, questions=[q for q in suite.questions if q.id == "q_topic"])
    message_suite = EvaluationSuite(id=suite.id + "_message", story_family=suite.story_family, questions=[q for q in suite.questions if q.id != "q_topic"])
    topic_answers = run_probe_trials(provider, media.hook_media, topic_suite, trials=trials, seed_base=2000)
    message_answers = run_probe_trials(provider, media.full_media, message_suite, trials=trials, seed_base=3000)
    _, topic_sum, topic_tot = score_answers(topic_answers, topic_suite)
    _, msg_sum, _ = score_answers(message_answers, message_suite)
    usable_msgs = [s for s in msg_sum if s.question_id not in weak_question_ids]
    msg_passed = sum(1 for s in usable_msgs if s.passed)
    detail["message_questions"] = {s.question_id: {"passed": s.passed, "rate": round(s.pass_rate, 2), "valid": s.valid_trials, "weak_leakage": s.question_id in weak_question_ids} for s in msg_sum}
    topic = topic_sum[0] if topic_sum else None

    components: list[FitnessComponent] = [
        FitnessComponent(
            name="hook_topic_comprehension", value=None if topic is None or topic.valid_trials == 0 or "q_topic" in weak_question_ids else round(topic.pass_rate, 3),
            unit="share of trials", evidence=EvidenceClass.MODEL_EVAL, trials=trials, valid=topic.valid_trials if topic else 0,
            detail="model sees only the first 3 s (4 frames + transcript of those 3 s)" + ("; excluded: answerable without media" if "q_topic" in weak_question_ids else ""),
        ),
        FitnessComponent(
            name="message_comprehension", value=float(msg_passed) if usable_msgs else None, unit=f"of {len(usable_msgs)} questions",
            evidence=EvidenceClass.MODEL_EVAL, trials=trials, valid=sum(s.valid_trials for s in usable_msgs),
            detail="model sees 8 frames + transcript of the rendered audio; questions frozen from the original transcript",
        ),
    ]
    pref_calls = 0
    if control_media is not None:
        full_pref, n_full, reasons_full = pairwise_preference(
            provider, control_media.full_media, media.full_media,
            "These are two versions of the same short video. Which version would a typical viewer be more likely to watch to the end?",
        )
        pref_calls += 4
        components.append(FitnessComponent(name="model_full_preference_vs_control", value=None if full_pref is None else round(full_pref, 3), unit=f"share of {n_full} calls",
                                           evidence=EvidenceClass.MODEL_EVAL, valid=n_full, detail="both presentation orders, ties count half; a model judgment, not viewer behaviour"))
        detail["full_preference_reasons"] = reasons_full
        if media.opening_signature != control_media.opening_signature:
            hook_pref, n_hook, reasons_hook = pairwise_preference(
                provider, control_media.hook_media, media.hook_media,
                "These are the first 3 seconds of two versions of the same short video. After only this opening, which version would a typical viewer be more likely to keep watching?",
            )
            pref_calls += 4
            components.append(FitnessComponent(name="model_hook_preference_vs_control", value=None if hook_pref is None else round(hook_pref, 3), unit=f"share of {n_hook} calls",
                                               evidence=EvidenceClass.MODEL_EVAL, valid=n_hook, detail="first 3 s only, both orders"))
            detail["hook_preference_reasons"] = reasons_hook
        else:
            components.append(FitnessComponent(name="model_hook_preference_vs_control", value=None, unit="n/a", evidence=EvidenceClass.MODEL_EVAL, detail="identical opening to control; not tested"))

    timings = plan_timings(plan, genome)
    components.append(FitnessComponent(name="payoff_ms", value=timings["payoff_ms"], unit="ms", better="lower", evidence=EvidenceClass.MECHANICAL, detail="timeline start of the first proof or payoff beat (derived from the plan; roles are model labels)"))
    components.append(FitnessComponent(name="context_before_payoff_ms", value=timings["context_before_payoff_ms"], unit="ms", better="lower", evidence=EvidenceClass.MECHANICAL, detail="setup/context/problem beats after the opening and before the payoff"))
    try:
        cuts = detect_cuts(media.artifact)
        curve = motion_curve(media.artifact, plan.output.width, plan.output.height)
        spans = static_spans(cuts, curve, media.duration_ms)
        fvc = first_visual_change_ms(cuts, curve)
        components.append(FitnessComponent(name="first_visual_change_ms", value=fvc, unit="ms", better="lower", evidence=EvidenceClass.MECHANICAL, detail="scene cut or motion spike on the rendered arm"))
        components.append(FitnessComponent(name="longest_static_span_ms", value=(spans[0][1] - spans[0][0]) if spans else 0, unit="ms", better="lower", evidence=EvidenceClass.MECHANICAL, detail="no cut and low motion on the rendered arm"))
    except MediaError as exc:
        gate_notes.append(f"signal extraction failed: {exc}")
    components.append(FitnessComponent(name="duration_ms", value=media.duration_ms, unit="ms", better="lower", evidence=EvidenceClass.MECHANICAL))
    if render_ms is not None:
        components.append(FitnessComponent(name="render_ms", value=render_ms, unit="ms", better="lower", evidence=EvidenceClass.MECHANICAL))
    detail["model_calls"] = len(topic_answers) + len(message_answers) + pref_calls
    detail["eval_ms"] = int((time.monotonic() - t0) * 1000)
    detail["mechanical_failures"] = gate_notes
    return CreativeFitness(arm_id=arm_id, components=components, hard_gates_passed=gates_ok, gate_notes=gate_notes), detail


def fitness_table(fits: dict[str, CreativeFitness]) -> list[dict[str, Any]]:
    names: list[str] = []
    for f in fits.values():
        for c in f.components:
            if c.name not in names:
                names.append(c.name)
    return [{"component": n, **{arm: (f.get(n).value if f.get(n) else None) for arm, f in fits.items()}} for n in names]


def dump(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=str)
