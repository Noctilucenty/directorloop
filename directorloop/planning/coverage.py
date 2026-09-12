"""Asset coverage: which claims each clip visibly shows, verified from pixels.

Creator-declared claims are hints (status=declared). A probe provider looks at the
actual clip and reports what is shown; those become verified/absent entries with a
visibility grade. Cached per asset hash + model.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..domain.assets import AssetCoverageMap, AssetKind, AssetManifest, CoverageEntry, CoverageStatus
from ..domain.truth import SourceTruth
from ..media.frames import sample_frames
from ..observability.weave_ops import traced
from ..providers.base import MediaProbeProvider, ProbeMedia, ProviderError

COVERAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim_id": {"type": "string"},
                    "shown": {"type": "string", "enum": ["clearly", "small_or_partial", "not_shown"]},
                    "start_s": {"type": "number"},
                    "end_s": {"type": "number"},
                    "evidence": {"type": "string"},
                },
                "required": ["claim_id", "shown", "evidence"],
            },
        }
    },
    "required": ["claims"],
}


def declared_coverage(manifest: AssetManifest, truth: SourceTruth) -> AssetCoverageMap:
    entries = [
        CoverageEntry(claim_id=cid, asset_id=a.id, status=CoverageStatus.DECLARED, confidence=0.3, visibility="unknown", note="creator declared")
        for a in manifest.videos()
        for cid in a.declared_claims
        if any(c.id == cid for c in truth.claims)
    ]
    return AssetCoverageMap(entries=entries)


@traced("analyze_asset_coverage", kind="tool")
def analyze_asset_coverage(
    manifest: AssetManifest,
    asset_paths: dict[str, Path],
    truth: SourceTruth,
    provider: MediaProbeProvider,
    cache_dir: Path,
    frame_count: int = 6,
) -> AssetCoverageMap:
    """Verify declared coverage against pixels. Falls back to declared entries on provider failure."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = declared_coverage(manifest, truth)
    claims_text = "\n".join(f"- {c.id}: {c.text}" for c in truth.claims)
    for asset in manifest.videos():
        cache_file = cache_dir / f"coverage_{asset.content_hash[:16]}_{provider.capability.model.replace('/', '_')}.json"
        if cache_file.exists():
            data = json.loads(cache_file.read_text(encoding="utf-8"))
        else:
            path = asset_paths[asset.id]
            duration = asset.duration_ms or 0
            try:
                if "video" in provider.capability.modalities and path.stat().st_size < 19 * 1024 * 1024:
                    media = ProbeMedia(kind="video", duration_ms=duration, video_bytes=path.read_bytes())
                else:
                    media = ProbeMedia(kind="frames", duration_ms=duration, frames=sample_frames(path, duration, count=frame_count), transcript="")
                res = provider.judge_json(
                    media,
                    "For each statement below, say whether this clip VISIBLY shows it: 'clearly' (large in frame, "
                    "unambiguous), 'small_or_partial' (present but small, brief or partly hidden), or 'not_shown'. "
                    "Give start/end seconds when shown and one sentence of visual evidence. Judge only the pictures.\n"
                    f"Statements:\n{claims_text}",
                    COVERAGE_SCHEMA,
                )
                data = res.data
                cache_file.write_text(json.dumps(data), encoding="utf-8")
            except (ProviderError, OSError) as exc:
                out.entries.append(
                    CoverageEntry(claim_id="*", asset_id=asset.id, status=CoverageStatus.UNCERTAIN, confidence=0.0, note=f"coverage analysis failed: {str(exc)[:120]}")
                )
                continue
        for item in data.get("claims", []):
            cid = str(item.get("claim_id", ""))
            if not any(c.id == cid for c in truth.claims):
                continue
            shown = item.get("shown", "not_shown")
            status = CoverageStatus.VERIFIED if shown in ("clearly", "small_or_partial") else CoverageStatus.ABSENT
            visibility = {"clearly": "clear", "small_or_partial": "small", "not_shown": "unknown"}[shown]
            start = item.get("start_s")
            end = item.get("end_s")
            out.entries = [e for e in out.entries if not (e.claim_id == cid and e.asset_id == asset.id)]
            out.entries.append(
                CoverageEntry(
                    claim_id=cid,
                    asset_id=asset.id,
                    start_ms=int(float(start) * 1000) if isinstance(start, int | float) else None,
                    end_ms=int(float(end) * 1000) if isinstance(end, int | float) else None,
                    status=status,
                    confidence=0.8 if status == CoverageStatus.VERIFIED else 0.6,
                    visibility=visibility,
                    verified_by=provider.capability.model,
                    note=str(item.get("evidence", ""))[:200],
                )
            )
    return out


def coverage_summary(cov: AssetCoverageMap, manifest: AssetManifest) -> list[dict[str, object]]:
    labels = {a.id: a.label for a in manifest.assets}
    return [
        {
            "claim_id": e.claim_id,
            "asset": labels.get(e.asset_id, e.asset_id),
            "asset_id": e.asset_id,
            "status": str(e.status),
            "visibility": e.visibility,
            "interval_ms": [e.start_ms, e.end_ms] if e.start_ms is not None else None,
            "note": e.note,
        }
        for e in cov.entries
    ]


def video_asset_ids(manifest: AssetManifest) -> list[str]:
    return [a.id for a in manifest.assets if a.kind == AssetKind.VIDEO]
