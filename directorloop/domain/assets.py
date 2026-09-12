"""Asset records, manifests and the claim-to-interval coverage map."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .ids import sha256_json


class AssetKind(StrEnum):
    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"
    GRAPHIC = "graphic"


class AssetOrigin(StrEnum):
    UPLOADED = "uploaded"
    RECORDED = "recorded"
    LICENSED = "licensed"
    PUBLIC_DOMAIN = "public_domain"
    GENERATED = "generated"
    GRAPHIC = "graphic"
    RENDERED_FIXTURE = "rendered_fixture"  # procedurally rendered, clearly labeled illustrative material


class StreamInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width: int | None = None
    height: int | None = None
    fps: float | None = None
    duration_ms: int | None = None
    video_codec: str | None = None
    has_audio: bool = False
    audio_codec: str | None = None
    audio_sample_rate: int | None = None
    audio_channels: int | None = None
    rotation: int = 0
    nb_frames: int | None = None
    container: str | None = None
    size_bytes: int | None = None


class GenerationProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model_revision: str
    seed: int | None = None
    prompt_hash: str
    conditioning_asset_ids: list[str] = Field(default_factory=list)
    settings: dict[str, object] = Field(default_factory=dict)
    wall_ms: int | None = None
    status: str = "completed"  # completed | failed
    note: str = "Seed does not guarantee bit-identical output across hardware or library versions."


class AssetRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    kind: AssetKind
    origin: AssetOrigin
    content_hash: str
    storage_key: str
    original_filename: str | None = None  # never exposed to probe inputs
    label: str = ""  # neutral display label, never an answer-leaking description for probes
    duration_ms: int | None = None
    stream: StreamInfo | None = None
    declared_claims: list[str] = Field(default_factory=list)  # creator hints; not visual evidence
    rights_note: str = ""
    restrictions: list[str] = Field(default_factory=list)
    generation: GenerationProvenance | None = None
    created_at: str | None = None

    @property
    def is_generated(self) -> bool:
        return self.origin == AssetOrigin.GENERATED


class AssetManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    assets: list[AssetRecord]
    rights_statement: str = ""

    def get(self, asset_id: str) -> AssetRecord:
        for a in self.assets:
            if a.id == asset_id:
                return a
        raise KeyError(asset_id)

    def has(self, asset_id: str) -> bool:
        return any(a.id == asset_id for a in self.assets)

    def checksum(self) -> str:
        return sha256_json(sorted(a.content_hash for a in self.assets))

    def videos(self) -> list[AssetRecord]:
        return [a for a in self.assets if a.kind == AssetKind.VIDEO]


class CoverageStatus(StrEnum):
    DECLARED = "declared"  # creator says so; unverified
    VERIFIED = "verified"  # a model looked at the pixels and agreed
    ABSENT = "absent"  # a model looked and did not find it
    UNCERTAIN = "uncertain"


class CoverageEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str
    asset_id: str
    start_ms: int | None = None
    end_ms: int | None = None
    status: CoverageStatus
    confidence: float = 0.0
    visibility: str = "unknown"  # unknown | clear | small | obscured
    verified_by: str | None = None  # model id
    note: str = ""


class AssetCoverageMap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[CoverageEntry] = Field(default_factory=list)

    def for_claim(self, claim_id: str) -> list[CoverageEntry]:
        return [e for e in self.entries if e.claim_id == claim_id]

    def best_for_claim(self, claim_id: str, exclude_asset_ids: set[str] | None = None) -> CoverageEntry | None:
        exclude = exclude_asset_ids or set()
        rank = {CoverageStatus.VERIFIED: 3, CoverageStatus.DECLARED: 2, CoverageStatus.UNCERTAIN: 1, CoverageStatus.ABSENT: 0}
        vis = {"clear": 3, "small": 1, "obscured": 0, "unknown": 1}
        candidates = [e for e in self.for_claim(claim_id) if e.asset_id not in exclude and e.status != CoverageStatus.ABSENT]
        if not candidates:
            return None
        candidates.sort(key=lambda e: (rank[e.status], vis.get(e.visibility, 1), e.confidence), reverse=True)
        return candidates[0]
