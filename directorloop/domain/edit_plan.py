"""Internal edit representation and the typed operations a planner may emit.

Nothing in this module may carry shell commands, filter strings, URLs or code.
The renderer translates a validated EditPlan into FFmpeg argument arrays.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .ids import sha256_json

SCHEMA_VERSION = "1"


class OutputProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width: int = 720
    height: int = 1280
    fps_num: int = 30
    fps_den: int = 1

    @property
    def fps(self) -> float:
        return self.fps_num / self.fps_den


class CropRect(BaseModel):
    """Crop rectangle in source pixels."""

    model_config = ConfigDict(extra="forbid")

    x: int = Field(ge=0)
    y: int = Field(ge=0)
    w: int = Field(gt=0)
    h: int = Field(gt=0)


class Segment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    asset_id: str
    source_in_ms: int = Field(ge=0)
    source_out_ms: int = Field(gt=0)
    fit: Literal["contain", "cover"] = "cover"
    crop: CropRect | None = None
    speed: float = 1.0
    audio_policy: Literal["keep", "mute"] = "mute"
    audio_fade_in_ms: int = Field(default=0, ge=0, le=500)  # softens a hard audio join at a non-contiguous cut
    audio_fade_out_ms: int = Field(default=0, ge=0, le=500)
    label: str = ""

    @model_validator(mode="after")
    def _check(self) -> Segment:
        if self.source_out_ms <= self.source_in_ms:
            raise ValueError(f"segment {self.id}: source_out_ms must exceed source_in_ms")
        return self

    @property
    def duration_ms(self) -> int:
        return int(round((self.source_out_ms - self.source_in_ms) / self.speed))


class Caption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    text: str = Field(max_length=200)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    position: Literal["top", "middle", "bottom"] = "bottom"
    style: str = "default"

    @model_validator(mode="after")
    def _check(self) -> Caption:
        if self.end_ms <= self.start_ms:
            raise ValueError(f"caption {self.id}: end_ms must exceed start_ms")
        return self

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


class NarrationTrack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str
    offset_ms: int = 0
    gain_db: float = 0.0


class MusicTrack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str
    gain_db: float = -14.0


class ProtectedInterval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    reason: str = ""


class EditPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    parent_plan_hash: str | None = None
    output: OutputProfile = Field(default_factory=OutputProfile)
    segments: list[Segment]
    captions: list[Caption] = Field(default_factory=list)
    narration: NarrationTrack | None = None
    music: MusicTrack | None = None
    protected_intervals: list[ProtectedInterval] = Field(default_factory=list)
    change_rationale: str = ""

    # --- timeline helpers -------------------------------------------------
    def timeline_duration_ms(self) -> int:
        return sum(s.duration_ms for s in self.segments)

    def segment_index(self, segment_id: str) -> int:
        for i, s in enumerate(self.segments):
            if s.id == segment_id:
                return i
        raise KeyError(segment_id)

    def segment(self, segment_id: str) -> Segment:
        return self.segments[self.segment_index(segment_id)]

    def segment_start_ms(self, segment_id: str) -> int:
        start = 0
        for s in self.segments:
            if s.id == segment_id:
                return start
            start += s.duration_ms
        raise KeyError(segment_id)

    def segment_windows(self) -> list[tuple[str, int, int]]:
        out = []
        t = 0
        for s in self.segments:
            out.append((s.id, t, t + s.duration_ms))
            t += s.duration_ms
        return out

    def caption(self, caption_id: str) -> Caption:
        for c in self.captions:
            if c.id == caption_id:
                return c
        raise KeyError(caption_id)

    def content_hash(self) -> str:
        data = self.model_dump(mode="json")
        data.pop("change_rationale", None)
        data.pop("parent_plan_hash", None)
        return sha256_json(data)

    def asset_ids(self) -> set[str]:
        ids = {s.asset_id for s in self.segments}
        if self.narration:
            ids.add(self.narration.asset_id)
        if self.music:
            ids.add(self.music.asset_id)
        return ids


# --- Typed operations -------------------------------------------------------


class TrimSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["trim_segment"] = "trim_segment"
    segment_id: str
    source_in_ms: int = Field(ge=0)
    source_out_ms: int = Field(gt=0)


class MoveSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["move_segment"] = "move_segment"
    segment_id: str
    after_segment_id: str | None = None  # None places it first


class ReplaceSegment(BaseModel):
    """Swap the footage of a segment for another authorized asset."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["replace_segment"] = "replace_segment"
    segment_id: str
    asset_id: str
    source_in_ms: int = Field(ge=0)
    source_out_ms: int = Field(gt=0)
    crop: CropRect | None = None
    fit: Literal["contain", "cover"] = "cover"


class InsertSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["insert_segment"] = "insert_segment"
    after_segment_id: str | None = None
    segment: Segment


class RemoveSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["remove_segment"] = "remove_segment"
    segment_id: str


class SetCrop(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["set_crop"] = "set_crop"
    segment_id: str
    crop: CropRect | None


class SetCaption(BaseModel):
    """Create (caption_id None) or rewrite a caption."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["set_caption"] = "set_caption"
    caption_id: str | None = None
    text: str = Field(max_length=200)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    position: Literal["top", "middle", "bottom"] = "bottom"


class AdjustCaptionTiming(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["adjust_caption_timing"] = "adjust_caption_timing"
    caption_id: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)


class RemoveCaption(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["remove_caption"] = "remove_caption"
    caption_id: str


class InsertGeneratedSegment(BaseModel):
    """Insert a completed generated asset. The asset record must already exist with origin=generated."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["insert_generated_segment"] = "insert_generated_segment"
    after_segment_id: str | None = None
    generated_asset_id: str
    source_in_ms: int = Field(ge=0)
    source_out_ms: int = Field(gt=0)
    replace_segment_id: str | None = None


EditOp = Annotated[
    TrimSegment
    | MoveSegment
    | ReplaceSegment
    | InsertSegment
    | RemoveSegment
    | SetCrop
    | SetCaption
    | AdjustCaptionTiming
    | RemoveCaption
    | InsertGeneratedSegment,
    Field(discriminator="type"),
]


class EditOpList(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ops: list[EditOp]


class EditPlanError(ValueError):
    pass


def apply_ops(plan: EditPlan, ops: list[EditOp]) -> EditPlan:
    """Pure function: returns a new plan with the ops applied in order."""
    new = plan.model_copy(deep=True)
    new.parent_plan_hash = plan.content_hash()
    for op in ops:
        _apply_one(new, op)
    return new


def _apply_one(plan: EditPlan, op: EditOp) -> None:
    if isinstance(op, TrimSegment):
        seg = plan.segment(op.segment_id)
        if op.source_out_ms <= op.source_in_ms:
            raise EditPlanError("trim: out must exceed in")
        seg.source_in_ms = op.source_in_ms
        seg.source_out_ms = op.source_out_ms
    elif isinstance(op, MoveSegment):
        idx = plan.segment_index(op.segment_id)
        seg = plan.segments.pop(idx)
        if op.after_segment_id is None:
            plan.segments.insert(0, seg)
        else:
            plan.segments.insert(plan.segment_index(op.after_segment_id) + 1, seg)
    elif isinstance(op, ReplaceSegment):
        seg = plan.segment(op.segment_id)
        if op.source_out_ms <= op.source_in_ms:
            raise EditPlanError("replace: out must exceed in")
        seg.asset_id = op.asset_id
        seg.source_in_ms = op.source_in_ms
        seg.source_out_ms = op.source_out_ms
        seg.crop = op.crop
        seg.fit = op.fit
    elif isinstance(op, InsertSegment):
        if any(s.id == op.segment.id for s in plan.segments):
            raise EditPlanError(f"insert: segment id {op.segment.id} already exists")
        if op.after_segment_id is None:
            plan.segments.insert(0, op.segment)
        else:
            plan.segments.insert(plan.segment_index(op.after_segment_id) + 1, op.segment)
    elif isinstance(op, RemoveSegment):
        plan.segments.pop(plan.segment_index(op.segment_id))
        if not plan.segments:
            raise EditPlanError("remove: a plan must keep at least one segment")
    elif isinstance(op, SetCrop):
        plan.segment(op.segment_id).crop = op.crop
    elif isinstance(op, SetCaption):
        if op.end_ms <= op.start_ms:
            raise EditPlanError("caption: end must exceed start")
        if op.caption_id is None:
            new_id = f"cap_{len(plan.captions) + 1:02d}"
            while any(c.id == new_id for c in plan.captions):
                new_id += "x"
            plan.captions.append(
                Caption(id=new_id, text=op.text, start_ms=op.start_ms, end_ms=op.end_ms, position=op.position)
            )
        else:
            cap = plan.caption(op.caption_id)
            cap.text, cap.start_ms, cap.end_ms, cap.position = op.text, op.start_ms, op.end_ms, op.position
    elif isinstance(op, AdjustCaptionTiming):
        cap = plan.caption(op.caption_id)
        if op.end_ms <= op.start_ms:
            raise EditPlanError("caption timing: end must exceed start")
        cap.start_ms, cap.end_ms = op.start_ms, op.end_ms
    elif isinstance(op, RemoveCaption):
        plan.captions = [c for c in plan.captions if c.id != op.caption_id]
    elif isinstance(op, InsertGeneratedSegment):
        seg = Segment(
            id=f"gen_{op.generated_asset_id[-8:]}",
            asset_id=op.generated_asset_id,
            source_in_ms=op.source_in_ms,
            source_out_ms=op.source_out_ms,
            label="generated shot",
        )
        if op.replace_segment_id is not None:
            idx = plan.segment_index(op.replace_segment_id)
            plan.segments[idx] = seg
        elif op.after_segment_id is None:
            plan.segments.insert(0, seg)
        else:
            plan.segments.insert(plan.segment_index(op.after_segment_id) + 1, seg)
    else:  # pragma: no cover
        raise EditPlanError(f"unsupported op {type(op).__name__}")


def describe_ops(ops: list[EditOp], before: EditPlan, asset_labels: dict[str, str] | None = None) -> list[str]:
    """Human-readable diff lines for judges and logs."""
    labels = asset_labels or {}

    def lab(asset_id: str) -> str:
        return labels.get(asset_id, asset_id)

    lines: list[str] = []
    for op in ops:
        if isinstance(op, TrimSegment):
            seg = before.segment(op.segment_id)
            lines.append(
                f"Trimmed {op.segment_id} ({lab(seg.asset_id)}) from {seg.source_in_ms}-{seg.source_out_ms} ms "
                f"to {op.source_in_ms}-{op.source_out_ms} ms."
            )
        elif isinstance(op, MoveSegment):
            where = "to the start" if op.after_segment_id is None else f"after {op.after_segment_id}"
            lines.append(f"Moved {op.segment_id} {where}.")
        elif isinstance(op, ReplaceSegment):
            seg = before.segment(op.segment_id)
            extra = " with a crop" if op.crop else ""
            lines.append(
                f"Replaced the footage of {op.segment_id} ({lab(seg.asset_id)}) with {lab(op.asset_id)} "
                f"{op.source_in_ms}-{op.source_out_ms} ms{extra}."
            )
        elif isinstance(op, InsertSegment):
            where = "at the start" if op.after_segment_id is None else f"after {op.after_segment_id}"
            lines.append(
                f"Inserted {lab(op.segment.asset_id)} {op.segment.source_in_ms}-{op.segment.source_out_ms} ms {where}."
            )
        elif isinstance(op, RemoveSegment):
            lines.append(f"Removed {op.segment_id}.")
        elif isinstance(op, SetCrop):
            if op.crop is None:
                lines.append(f"Removed the crop on {op.segment_id}.")
            else:
                lines.append(f"Cropped {op.segment_id} to a {op.crop.w}x{op.crop.h} region at ({op.crop.x},{op.crop.y}).")
        elif isinstance(op, SetCaption):
            verb = "Added" if op.caption_id is None else f"Rewrote {op.caption_id} as"
            lines.append(f'{verb} caption "{op.text}" at {op.start_ms}-{op.end_ms} ms.')
        elif isinstance(op, AdjustCaptionTiming):
            lines.append(f"Retimed caption {op.caption_id} to {op.start_ms}-{op.end_ms} ms.")
        elif isinstance(op, RemoveCaption):
            lines.append(f"Removed caption {op.caption_id}.")
        elif isinstance(op, InsertGeneratedSegment):
            target = f"replacing {op.replace_segment_id}" if op.replace_segment_id else (
                "at the start" if op.after_segment_id is None else f"after {op.after_segment_id}"
            )
            lines.append(f"Inserted generated shot {lab(op.generated_asset_id)} {target}.")
    if not lines:
        lines.append("No changes.")
    return lines


def plan_diff(before: EditPlan, after: EditPlan) -> dict[str, object]:
    """Structural diff for the UI: segment order, changed segments, changed captions."""
    b = {s.id: s for s in before.segments}
    a = {s.id: s for s in after.segments}
    changed = [sid for sid in a if sid in b and a[sid] != b[sid]]
    return {
        "segment_order_before": [s.id for s in before.segments],
        "segment_order_after": [s.id for s in after.segments],
        "segments_added": [sid for sid in a if sid not in b],
        "segments_removed": [sid for sid in b if sid not in a],
        "segments_changed": changed,
        "captions_before": [c.model_dump() for c in before.captions],
        "captions_after": [c.model_dump() for c in after.captions],
        "narration_changed": before.narration != after.narration,
        "music_changed": before.music != after.music,
        "duration_before_ms": before.timeline_duration_ms(),
        "duration_after_ms": after.timeline_duration_ms(),
    }
