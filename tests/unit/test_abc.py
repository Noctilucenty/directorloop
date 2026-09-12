"""A/B-to-C: alignment, the deterministic C decision, two-order aggregation, run limits, and full runs over real ffmpeg renders
of two generated edits with scripted model answers (no network)."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from directorloop.audit.models import AuditFinding, AuditReport, ChangeVerification, CoverageRecord, Strength
from directorloop.compare.align import align_beats
from directorloop.compare.judge import WHOLE_DIMENSIONS, compare_pair
from directorloop.compare.models import DeclaredContext, DimensionJudgment, PairComparison
from directorloop.domain.creative import BeatRole
from directorloop.domain.ids import sha256_file
from directorloop.media.probe import FFMPEG
from directorloop.media.transcribe import Transcript, TranscriptSegment
from directorloop.providers.base import CompletionResult, ProbeMedia, ProviderCapability, ProviderError
from directorloop.providers.registry import ProviderBundle
from directorloop.runtime import abc as R
from directorloop.runtime.budget import BudgetedProvider, BudgetExceeded, CallBudget
from tests.unit.test_creative_mutations import make_genome

CTX = DeclaredContext(objective="understand how the bridge was measured", audience="general viewers")


@dataclass
class Beat:
    id: str
    text: str
    start_ms: int
    end_ms: int


# ----------------------------------------------------------------------------- alignment


def test_alignment_merges_splits_and_one_sided_sentences() -> None:
    a = [Beat("a0", "His friends measured a bridge using his body.", 0, 2000), Beat("a1", "They laid him down and marked his height.", 2000, 4000),
         Beat("a2", "His name was Smoot.", 4000, 5500), Beat("a3", "The bridge is still marked in Smoots.", 5500, 8000)]
    b = [Beat("b0", "His friends measured a bridge using his body.", 0, 2200), Beat("b1", "His name was Smoot.", 2200, 3500),
         Beat("b2", "The bridge is still marked in Smoots today.", 3500, 6000), Beat("b3", "Police later used the marks to locate accidents.", 6000, 8000)]
    units = align_beats(a, b)
    assert [(u.kind, u.a_beats, u.b_beats) for u in units] == [
        ("shared", ["a0"], ["b0"]), ("only_a", ["a1"], []), ("shared", ["a2"], ["b1"]), ("shared", ["a3"], ["b2"]), ("only_b", [], ["b3"])]
    merged = align_beats([Beat("x0", "His friends measured a bridge using his body.", 0, 2000), Beat("x1", "Then they moved him forward again and again.", 2000, 5000)],
                         [Beat("y0", "His friends measured a bridge using his body, then moved him forward again and again.", 0, 4000)])
    assert len(merged) == 1 and merged[0].a_beats == ["x0", "x1"] and merged[0].b_beats == ["y0"] and merged[0].kind in ("shared", "reworded")
    silent = align_beats([Beat("s0", "", 0, 9000)], [Beat("t0", "", 0, 6000)])
    assert silent and all(u.kind == "time_aligned" for u in silent)


# ----------------------------------------------------------------------------- decision rules


def pc(first: str, second: str, verdicts: dict[str, str], overall: str, scope: str = "whole") -> PairComparison:
    dims = WHOLE_DIMENSIONS if scope == "whole" else {"clarity": "", "pacing": ""}
    return PairComparison(first=first, second=second, scope=scope, dimensions=[DimensionJudgment(dimension=d, verdict=verdicts.get(d, "same")) for d in dims],  # type: ignore[arg-type]
                          overall=DimensionJudgment(dimension="overall", verdict=overall), rubric_version="test")


def test_decide_c_rules() -> None:
    kw = {"target_dimensions": ["opening_interest"], "base_label": "A", "other_label": "B", "protected": [], "new_weaknesses": []}
    win = R.decide_c(vs_base=pc("C", "A", {"opening_interest": "C"}, "C"), vs_other=pc("C", "B", {}, "same"), target=pc("C", "A", {}, "C", "region"), **kw)
    assert win["outcome"] == "improvement" and "opening_interest vs A" in win["improved"]
    lost = R.decide_c(vs_base=pc("C", "A", {"opening_interest": "C"}, "C"), vs_other=None, target=None,
                      **{**kw, "protected": [{"item": "the reveal", "kind": "content", "status": "lost"}]})
    assert lost["outcome"] == "regression" and "protected" in lost["reason"]
    worse = R.decide_c(vs_base=pc("C", "A", {"opening_interest": "C"}, "A"), vs_other=None, target=None, **kw)
    assert worse["outcome"] == "regression"
    core = R.decide_c(vs_base=pc("C", "A", {"opening_interest": "C", "comprehension": "A"}, "unstable"), vs_other=None, target=None, **kw)
    assert core["outcome"] == "mixed" and "comprehension" in core["reason"]
    core_only = R.decide_c(vs_base=pc("C", "A", {"payoff": "A"}, "same"), vs_other=None, target=None, **kw)
    assert core_only["outcome"] == "regression"
    beaten = R.decide_c(vs_base=pc("C", "A", {"opening_interest": "C"}, "C"), vs_other=pc("C", "B", {}, "B"), target=None, **kw)
    assert beaten["outcome"] == "mixed" and "B was preferred" in beaten["reason"]
    new_weak = R.decide_c(vs_base=pc("C", "A", {"opening_interest": "C"}, "C"), vs_other=None, target=None, **{**kw, "new_weaknesses": ["4.0-5.0s across a join"]})
    assert new_weak["outcome"] == "mixed"
    unclear = R.decide_c(vs_base=pc("C", "A", {"opening_interest": "unstable"}, "unstable"), vs_other=None, target=pc("C", "A", {}, "unclear", "region"), **kw)
    assert unclear["outcome"] == "insufficient_evidence"
    tie = R.decide_c(vs_base=pc("C", "A", {}, "same"), vs_other=None, target=pc("C", "A", {}, "same", "region"), **kw)
    assert tie["outcome"] == "tie"
    assert R.decide_c(vs_base=None, vs_other=None, target=None, **kw)["outcome"] == "insufficient_evidence"


# ----------------------------------------------------------------------------- two-order aggregation


class OrderJudge:
    """Answers by which label is shown first: prefers a fixed label on some dimensions, disagrees across orders on others."""

    capability = ProviderCapability(name="fake", role="probe", model="scripted", modalities={"text", "image"}, structured_output=True, docs_ref="", present=True)

    def __init__(self, fail_second: bool = False) -> None:
        self.calls = 0
        self.fail_second = fail_second

    def judge_json(self, media: ProbeMedia, instruction: str, schema: dict[str, Any]) -> CompletionResult:
        self.calls += 1
        first_is_x = media.transcript.startswith("Version 1 words: X")  # type: ignore[union-attr]
        if self.fail_second and not first_is_x:
            raise ProviderError("rate limited")
        answers = {d: {"choice": "same", "reason": "no difference"} for d in schema["properties"]["answers"]["properties"]}
        answers["comprehension"] = {"choice": "1" if first_is_x else "2", "reason": "X explains the steps"}  # X in both orders
        answers["pacing"] = {"choice": "1", "reason": "whichever is first"}  # order effect
        answers["payoff"] = {"choice": "maybe", "reason": "invalid"}
        return CompletionResult(data={"answers": answers, "overall": {"choice": "1" if first_is_x else "2", "reason": "X"}}, latency_ms=1, model="scripted")


def test_compare_pair_keeps_order_disagreement() -> None:
    x = ProbeMedia(kind="frames", duration_ms=1000, frames=[], transcript="X words")
    y = ProbeMedia(kind="frames", duration_ms=1000, frames=[], transcript="Y words")
    judge = OrderJudge()
    res = compare_pair(judge, "A", x, "B", y, CTX, "whole")  # type: ignore[arg-type]
    assert judge.calls == 2 and res.calls == 2 and res.failed_calls == 0
    assert res.verdict("comprehension") == "A" and res.verdict("pacing") == "unstable" and res.verdict("payoff") == "unclear"
    assert res.verdict("opening_interest") == "same" and res.overall.verdict == "A"
    one_failed = compare_pair(OrderJudge(fail_second=True), "A", x, "B", y, CTX, "whole")  # type: ignore[arg-type]
    assert one_failed.failed_calls == 1 and one_failed.verdict("comprehension") == "unstable", "a single order is never a stable verdict"


def test_call_budget_limits() -> None:
    budget = CallBudget(max_calls=2)
    wrapped = BudgetedProvider(OrderJudge(), budget)
    media = ProbeMedia(kind="frames", duration_ms=1, frames=[], transcript="Version 1 words: X\nVersion 2 words: Y")
    schema = {"properties": {"answers": {"properties": {"comprehension": {}, "pacing": {}, "payoff": {}}}}}
    wrapped.judge_json(media, "q", schema)
    wrapped.judge_json(media, "q", schema)
    with pytest.raises(BudgetExceeded):
        wrapped.judge_json(media, "q", schema)
    assert budget.summary()["model_calls"] == 2 and budget.summary()["cost_usd"] is None
    late = CallBudget(deadline_s=0.0)
    with pytest.raises(BudgetExceeded):
        late.charge()


# ----------------------------------------------------------------------------- full scripted runs over real renders

A_TEXTS = ["His friends measured a bridge using his body.", "They laid him down and marked his height.", "His name was Smoot.", "The bridge is still marked in Smoots."]
B_TEXTS = ["His friends measured a bridge using his body.", "His name was Smoot.", "The bridge is still marked in Smoots today.", "Police later used the marks to locate accidents."]


def _clip(path: Path, source: str, freq: int) -> Path:
    env = f"volume='0.2+0.8*mod(floor(t*4)*{3 if freq < 400 else 5},7)/6':eval=frame"
    subprocess.run([FFMPEG, "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"{source}=s=540x960:d=8:r=30", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=8",
                    "-af", env, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)], check=True, timeout=90)
    return path


@pytest.fixture(scope="module")
def clips(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    d = tmp_path_factory.mktemp("abc")
    return _clip(d / "a.mp4", "testsrc2", 330), _clip(d / "b.mp4", "rgbtestsrc", 520)


def _genome(path: Path, texts: list[str]) -> Any:
    g = make_genome(sha256_file(path), [BeatRole.HOOK, BeatRole.CONTEXT, BeatRole.PROOF, BeatRole.PAYOFF])
    return g.model_copy(update={"beats": [b.model_copy(update={"text": t}) for b, t in zip(g.beats, texts, strict=True)]})


def _transcript(texts: list[str]) -> Transcript:
    return Transcript(text=" ".join(texts), segments=[TranscriptSegment(start_ms=i * 2000 + 100, end_ms=i * 2000 + 1900, text=t) for i, t in enumerate(texts)])


class ScriptedPlanner:
    capability = ProviderCapability(name="fake", role="planner", model="scripted", modalities={"text"}, structured_output=True, docs_ref="", present=True)

    def __init__(self, preferences: list[str | None]) -> None:
        self.preferences = preferences
        self.prompts: list[str] = []

    def complete_json(self, system: str, user: str, schema: dict[str, Any], *, temperature: float = 0.2) -> CompletionResult:
        self.prompts.append(user)
        offered = set(re.findall(r"^- ([a-z0-9_]+):", user.split("Executable options")[-1], flags=re.M))
        pick = next((p for p in self.preferences if p is None or p in offered), None)
        return CompletionResult(data={"choice": pick, "target_dimensions": ["opening_interest", "not_a_dimension"], "expected_improvement": "clearer start",
                                      "protected_strengths": [{"source": "A", "item": "the measuring demonstration"}], "tradeoffs": ["loses a beat"],
                                      "why_smallest": "one part", "reason": "scripted" if pick else "nothing is supported", "needed_capability": "" if pick else "new footage"},
                                latency_ms=1, model="scripted")


def install(monkeypatch: pytest.MonkeyPatch, clips: tuple[Path, Path], comparisons: dict[str, str], incomplete: set[str] | None = None,
            unverified: set[str] | None = None, protected_status: str = "kept") -> dict[str, Any]:
    a, b = clips
    texts = {str(a): A_TEXTS, str(b): B_TEXTS}
    seen: dict[str, Any] = {"audits": [], "compares": [], "protected": []}
    monkeypatch.setattr(R, "extract_genome", lambda path, *args, **kw: _genome(Path(path), texts[str(path)]))
    monkeypatch.setattr(R, "transcribe", lambda path, *args, **kw: _transcript(texts.get(str(path), [])))

    def fake_audit(*, video_id: str, video_path: Path, version_id: str, provider: Any, data_dir: Path, on_stage: Any = None, closeups: bool = True) -> AuditReport:
        seen["audits"].append(version_id)
        status = "incomplete" if version_id in (incomplete or set()) else "complete"
        finding = AuditFinding(id=f"{version_id}_f1", version_id=version_id, start_ms=6000, end_ms=8000, weakness="the ending hangs on a static shot", severity="medium")
        return AuditReport(id=f"audit_{len(seen['audits']):x}_1", video_id=video_id, version_id=version_id, artifact_hash=sha256_file(video_path), artifact_path=str(video_path),
                           duration_ms=8000, created_at="t", coverage=CoverageRecord(mode="sampled_frames_plus_asr", duration_ms=8000), status=status,  # type: ignore[arg-type]
                           findings=[finding] if version_id in ("A", "B") else [], strengths=[Strength(id="s", start_ms=0, end_ms=2000, what="a clear demonstration", why="shows it")],
                           incomplete_reasons=["2 of 4 window reviews failed"] if status == "incomplete" else [])

    def fake_compare(provider: Any, first_label: str, first: Any, second_label: str, second: Any, context: Any, scope: str, region: Any = None) -> PairComparison:
        key = f"{first_label}{second_label}:{scope}"
        attempt = sum(1 for k in seen["compares"] if k.startswith("CA:whole") or k.startswith("CB:whole"))
        seen["compares"].append(key)
        verdict = comparisons.get(f"{key}:{attempt // 2}", comparisons.get(key, "same"))
        dims = {"opening_interest": verdict} if scope == "whole" else {}
        return pc(first_label, second_label, dims, verdict, "whole" if scope == "whole" else "region")

    def fake_protected(provider: Any, original: Any, candidate: Any, items: list[str]) -> list[dict[str, str]]:
        seen["protected"].append(items)
        return [{"item": i, "kind": "rule" if i.lower().startswith("do not") else "content", "status": "respected" if i.lower().startswith("do not") else protected_status,
                 "evidence": "scripted"} for i in items]

    monkeypatch.setattr(R, "run_audit", fake_audit)
    monkeypatch.setattr(R, "compare_pair", fake_compare)
    monkeypatch.setattr(R, "check_protected", fake_protected)
    import importlib

    T = importlib.import_module("directorloop.media.transcribe")

    real_verify = R.verify_c_render

    def verify(option: Any, plan: Any, c_path: Path, materials: dict[str, Any]) -> ChangeVerification:
        if option.key in (unverified or set()):
            return ChangeVerification(intended=option.description, verified=False, checks=["scripted: wrong picture"])
        from directorloop.compare.recombine import ASSET_ID, planned_words_multi

        words = planned_words_multi(plan, {ASSET_ID[k]: m.words for k, m in materials.items()})
        monkeypatch.setattr(T, "transcribe", lambda p, *args, **kw: Transcript(text=" ".join(w.text for w in words),
                                                                                  segments=[TranscriptSegment(start_ms=w.start_ms, end_ms=w.end_ms, text=w.text) for w in words]))
        return real_verify(option, plan, c_path, materials)

    monkeypatch.setattr(R, "verify_c_render", verify)
    return seen


def config(clips: tuple[Path, Path], **kw: Any) -> R.ABCConfig:
    a, b = clips
    return R.ABCConfig(a_video_id="edit-a", a_path=str(a), b_video_id="edit-b", b_path=str(b), context=CTX, constraints=["Do not add new claims or on-screen text"], **kw)


def bundle(planner: ScriptedPlanner) -> ProviderBundle:
    return ProviderBundle(probe=OrderJudge(), planner=planner)  # type: ignore[arg-type]


def test_regression_then_accepted_c(monkeypatch: pytest.MonkeyPatch, clips: tuple[Path, Path], tmp_path: Path) -> None:
    seen = install(monkeypatch, clips, {"AB:whole": "A", "CA:whole:0": "A", "CB:whole:0": "B", "CA:whole:1": "C", "CB:whole:1": "same", "CA:region": "C"})
    planner = ScriptedPlanner(["swap_a_u3", "remove_a_u1"])
    events: list[tuple[str, str]] = []
    run = R.run_abc(config(clips, iteration_budget=2), bundle(planner), tmp_path / "data", on_stage=lambda st, m, d: events.append((st, m)))

    assert run.status == "completed" and run.rubric["sha256"] and run.rubric["version"].startswith("abc-rubric")
    assert [u.kind for u in run.comparison.alignment] == ["shared", "only_a", "shared", "shared", "only_b"]  # type: ignore[union-attr]
    assert run.comparison.best_supported == "A"  # type: ignore[union-attr]
    assert seen["audits"][:2] in (["A", "B"], ["B", "A"]) and len(seen["audits"]) == 4, "A and B audited, then one fresh audit per rendered C"
    first, second = run.attempts
    assert first.proposal.option_key == "swap_a_u3" and first.evaluation.outcome == "regression" and first.decision == "reject_keep_inputs"  # type: ignore[union-attr]
    assert first.next_action == "try_another_c"
    assert first.evaluation.change_verification.verified, first.evaluation.change_verification.checks  # type: ignore[union-attr]
    assert any("matches B" in c and "ok" in c for c in first.evaluation.change_verification.checks), "the swapped part's pictures come from B"  # type: ignore[union-attr]
    assert "swap_a_u3" in planner.prompts[1] is False or "Earlier C attempts" in planner.prompts[1]
    assert "- swap_a_u3:" not in planner.prompts[1], "a failed option is not offered again"
    assert second.proposal.option_key == "remove_a_u1" and second.evaluation.outcome == "improvement" and second.decision == "accept"  # type: ignore[union-attr]
    assert second.proposal.target_dimensions == ["opening_interest"], "invalid dimensions from the selector are dropped"  # type: ignore[union-attr]
    assert run.final_version == "C" and run.final_decision.startswith("keep C")
    assert Path(second.render_path).exists() and run.versions["C2"].evaluated_hash == second.render_hash  # type: ignore[arg-type]
    assert run.versions["A"].evaluated_hash != run.versions["A"].original_hash, "A is re-rendered through the same pipeline as C"
    assert any("Do not add new claims" in " ".join(items) for items in seen["protected"])
    lessons = [json.loads(x) for x in (tmp_path / "data" / "abc" / "lessons.jsonl").read_text().splitlines()]
    assert [ln["outcome"] for ln in lessons] == ["regression", "improvement"] and all(ln["plain"] for ln in lessons)
    assert R.load_abc(tmp_path / "data", run.id) is not None and events[-1][0] == "DONE"
    assert run.usage["model_calls"] == 2, "only the two selector calls reached a provider in this scripted run"


def test_selector_declines_and_inputs_are_kept(monkeypatch: pytest.MonkeyPatch, clips: tuple[Path, Path], tmp_path: Path) -> None:
    install(monkeypatch, clips, {"AB:whole": "unstable"})
    run = R.run_abc(config(clips), bundle(ScriptedPlanner([None])), tmp_path / "data")
    assert run.attempts[0].decision == "stop" and "nothing is supported" in run.stop_reason and "new footage" in run.stop_reason
    assert run.final_version == "" and run.final_decision.startswith("keep both inputs")


def test_unverified_c_is_never_reviewed_and_lost_strength_is_a_regression(monkeypatch: pytest.MonkeyPatch, clips: tuple[Path, Path], tmp_path: Path) -> None:
    seen = install(monkeypatch, clips, {"AB:whole": "B", "CA:whole": "C", "CB:whole": "C", "CA:region": "C"}, unverified={"swap_a_u3"}, protected_status="lost")
    run = R.run_abc(config(clips, iteration_budget=2), bundle(ScriptedPlanner(["swap_a_u3", "remove_a_u1"])), tmp_path / "data")
    first, second = run.attempts
    assert first.decision == "incomplete" and first.evaluation.candidate_audit_id is None, _debug_attempts(run)  # type: ignore[union-attr]
    assert len([a for a in seen["audits"] if a.startswith("C")]) == 1, _debug_attempts(run)
    assert second.evaluation.outcome == "regression" and "protected" in second.evaluation.outcome_reason  # type: ignore[union-attr]
    assert run.final_version == "B" and run.stop_reason == "attempt budget of 2 used"


def test_incomplete_input_audit_and_refused_input(monkeypatch: pytest.MonkeyPatch, clips: tuple[Path, Path], tmp_path: Path) -> None:
    install(monkeypatch, clips, {}, incomplete={"B"})
    run = R.run_abc(config(clips), bundle(ScriptedPlanner(["swap_a_u3"])), tmp_path / "data")
    assert run.attempts == [] and "incomplete" in run.stop_reason and run.comparison is None
    missing = R.run_abc(R.ABCConfig(a_video_id="edit-a", a_path=str(tmp_path / "nope.mp4"), b_video_id="edit-b", b_path=str(clips[1]), context=CTX),
                        bundle(ScriptedPlanner([None])), tmp_path / "data2")
    assert missing.status == "failed" and missing.stop_reason.startswith("input A refused")


def test_run_limit_stops_explicitly(monkeypatch: pytest.MonkeyPatch, clips: tuple[Path, Path], tmp_path: Path) -> None:
    install(monkeypatch, clips, {"AB:whole": "A"})
    real_charge = CallBudget.charge

    def charge(self: CallBudget) -> None:
        if self.calls >= 1:
            raise BudgetExceeded("model-call limit of 1 reached")
        real_charge(self)

    monkeypatch.setattr(CallBudget, "charge", charge)
    run = R.run_abc(config(clips, iteration_budget=3), bundle(ScriptedPlanner(["swap_a_u3", "remove_a_u1"])), tmp_path / "data")
    assert run.stop_reason.startswith("stopped by a run limit") and run.status == "completed" and len(run.attempts) == 1


def _debug_attempts(run: Any) -> str:  # used only in assertion messages
    return " | ".join(f"{a.index}:{a.decision}:{a.reason}:{(a.evaluation.change_verification.checks if a.evaluation and a.evaluation.change_verification else None)}" for a in run.attempts)


def test_new_weakness_only_counts_where_the_edit_changed_something() -> None:
    from directorloop.compare.recombine import ASSET_ID
    from directorloop.domain.edit_plan import EditPlan, OutputProfile, Segment

    plan = EditPlan(output=OutputProfile(width=540, height=960), segments=[
        Segment(id="a_0", asset_id=ASSET_ID["A"], source_in_ms=0, source_out_ms=4000, audio_policy="keep"),
        Segment(id="c_b_1", asset_id=ASSET_ID["B"], source_in_ms=4000, source_out_ms=6000, audio_policy="keep"),
        Segment(id="a_2", asset_id=ASSET_ID["A"], source_in_ms=6000, source_out_ms=12000, audio_policy="keep")])

    def rep(vid: str, findings: list[tuple[int, int]]) -> AuditReport:
        return AuditReport(id=f"audit_{vid}", video_id="v", version_id=vid, artifact_hash="h", artifact_path="/x", duration_ms=12000, created_at="t",
                           coverage=CoverageRecord(mode="sampled_frames_plus_asr", duration_ms=12000),
                           findings=[AuditFinding(id=f"{vid}{i}", version_id=vid, start_ms=s, end_ms=e, weakness=f"w{i}", severity="medium") for i, (s, e) in enumerate(findings)])

    c = rep("C1", [(1000, 2000), (4100, 5000), (9000, 11000), (6500, 7000)])
    new, variance = R._new_weaknesses(c, plan, {"A": rep("A", [(6400, 7200)]), "B": rep("B", [])}, (4000, 6000))
    assert [n.split(" [")[0] for n in new] == ["4.1-5.0s"], new  # inside the swapped part
    assert len(variance) == 2 and all("unchanged material" in v for v in variance)  # 1.0-2.0s and 9.0-11.0s: untouched A footage
    # 6.5-7.0s is a known A weakness at the same source moment: neither new nor variance


def test_provider_failure_is_an_explicit_failed_record(monkeypatch: pytest.MonkeyPatch, clips: tuple[Path, Path], tmp_path: Path) -> None:
    install(monkeypatch, clips, {"AB:whole": "A"})

    def broken_audit(**kw: Any) -> AuditReport:
        raise ProviderError("openai request failed: Error code: 429 insufficient_quota")

    monkeypatch.setattr(R, "run_audit", broken_audit)
    run = R.run_abc(config(clips), bundle(ScriptedPlanner([None])), tmp_path / "data")
    assert run.status == "failed" and run.stop_reason.startswith("the model provider failed") and "429" in run.stop_reason
    stored = R.load_abc(tmp_path / "data", run.id)
    assert stored is not None and stored.status == "failed", "no record is left running"
