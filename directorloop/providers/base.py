"""Provider interfaces and the shared, answer-free probe prompt builder."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..domain.evaluation import ProbeAnswer
from ..domain.truth import NOT_SHOWN_OPTION_ID, EvidenceModality, ProbeQuestionView
from ..media.frames import SampledFrame


class ProviderError(RuntimeError):
    pass


@dataclass
class ProviderCapability:
    name: str
    role: str  # probe | planner | speech | generation
    model: str
    modalities: set[str] = field(default_factory=lambda: {"text"})
    structured_output: bool = False
    state: str = "unknown"  # unknown | verified_supported | verified_unsupported
    smoke_result: str | None = None
    checked_at: str | None = None
    docs_ref: str = ""
    present: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "model": self.model,
            "modalities": sorted(self.modalities),
            "structured_output": self.structured_output,
            "state": self.state,
            "smoke_result": self.smoke_result,
            "checked_at": self.checked_at,
            "docs_ref": self.docs_ref,
            "present": self.present,
        }


@dataclass
class ProbeMedia:
    """Exactly what a viewer receives. Built only from the rendered artifact."""

    kind: str  # video | frames | none
    duration_ms: int = 0
    video_bytes: bytes | None = None
    mime_type: str = "video/mp4"
    frames: list[SampledFrame] = field(default_factory=list)
    transcript: str | None = None  # ASR of the rendered audio; never the planned script

    @property
    def modality_label(self) -> str:
        if self.kind == "video":
            return "video_native"
        if self.kind == "frames":
            return "frames_and_transcript" if self.transcript else "frames_only"
        return "none"


@dataclass
class CompletionResult:
    data: dict[str, Any]
    latency_ms: int
    model: str
    raw_text: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None


class MediaProbeProvider(Protocol):
    capability: ProviderCapability

    def answer_questions(self, media: ProbeMedia, questions: list[ProbeQuestionView], *, seed: int) -> list[ProbeAnswer]: ...

    def judge_json(self, media: ProbeMedia, instruction: str, schema: dict[str, Any]) -> CompletionResult: ...


class TextPlannerProvider(Protocol):
    capability: ProviderCapability

    def complete_json(self, system: str, user: str, schema: dict[str, Any], *, temperature: float = 0.2) -> CompletionResult: ...


VIEWER_SYSTEM_PROMPT = (
    "You are watching a short video and answering questions about it. "
    "Use only what this video shows or says. Do not use outside knowledge and do not guess. "
    "Questions marked VISUAL must be answered from what is visibly shown in the pictures; if the pictures do not "
    "show it, choose the 'not_shown' option even if the narration or on-screen text mentions it. "
    "Questions marked AUDIO may be answered from narration or on-screen text. "
    "Questions marked ANY may use anything in the video. "
    "For every question return the option id you choose and one short sentence of evidence describing what you saw or heard."
)

NO_MEDIA_SYSTEM_PROMPT = (
    "You are answering questions without any video, image, audio or transcript. "
    "If you cannot know the answer from the question alone, choose the 'not_shown' option. "
    "Return the option id you choose and one short sentence explaining your basis."
)


def answers_schema(question_ids: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "answers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "question_id": {"type": "string", "enum": question_ids},
                        "option_id": {"type": "string"},
                        "evidence": {"type": "string"},
                    },
                    "required": ["question_id", "option_id", "evidence"],
                },
            }
        },
        "required": ["answers"],
    }


def render_questions_text(questions: list[ProbeQuestionView]) -> str:
    label = {EvidenceModality.VISUAL: "VISUAL", EvidenceModality.AUDIO: "AUDIO", EvidenceModality.EITHER: "ANY"}
    lines: list[str] = []
    for i, q in enumerate(questions, 1):
        lines.append(f"Q{i} [{label[q.modality]}] (id: {q.id}) {q.text}")
        for o in q.options:
            lines.append(f"   - {o.id}: {o.text}")
    lines.append("")
    lines.append("Return JSON: {\"answers\": [{\"question_id\": ..., \"option_id\": ..., \"evidence\": ...}, ...]} with one entry per question.")
    return "\n".join(lines)


def parse_answers(data: dict[str, Any], questions: list[ProbeQuestionView], trial: int, latency_ms: int, raw: str = "") -> list[ProbeAnswer]:
    valid_options = {q.id: {o.id for o in q.options} for q in questions}
    got: dict[str, tuple[str | None, str]] = {}
    for item in data.get("answers", []) if isinstance(data, dict) else []:
        if not isinstance(item, dict):
            continue
        qid = str(item.get("question_id", ""))
        oid = str(item.get("option_id", "")).strip()
        if qid in valid_options:
            got[qid] = (oid if oid in valid_options[qid] else None, str(item.get("evidence", ""))[:300])
    answers: list[ProbeAnswer] = []
    per_q_latency = latency_ms // max(1, len(questions))
    for q in questions:
        if q.id in got:
            oid, evidence = got[q.id]
            answers.append(
                ProbeAnswer(
                    question_id=q.id,
                    trial=trial,
                    chosen_option_id=oid,
                    raw_text=evidence,
                    latency_ms=per_q_latency,
                    error=None if oid else "option id not among the offered options",
                )
            )
        else:
            answers.append(ProbeAnswer(question_id=q.id, trial=trial, chosen_option_id=None, raw_text=raw[:200], latency_ms=per_q_latency, error="no answer returned"))
    return answers


def extract_json(text: str) -> dict[str, Any]:
    """Parse JSON from a model reply, tolerating code fences and leading prose."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise ProviderError(f"model returned non-JSON output: {text[:120]!r}")


def not_shown_id() -> str:
    return NOT_SHOWN_OPTION_ID
