"""Freeze unattempted source identities for future review; no previews or model calls."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from directorloop.domain.ids import sha256_bytes, sha256_file, sha256_json, utc_now_iso
from directorloop.screening.runner import SCREENING_INSTRUCTION, SCREENING_SCHEMA, SCREENING_VERSION


def prior_identities(manifests: list[dict]) -> tuple[set[str], set[str]]:
    hashes, creators = set(), set()

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"source_sha256", "artifact_sha256", "artifact_hash", "sha256"} and isinstance(item, str) and re.fullmatch(r"[0-9a-f]{64}", item):
                    hashes.add(item)
                if key in {"source_path", "media_path", "staged_path"} and isinstance(item, str):
                    name = Path(item).stem.rsplit("_", 1)[0]
                    if name.startswith("tiktok_"):
                        creators.add(name)
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    for manifest in manifests:
        walk(manifest)
    return hashes, creators


def select_unattempted(media: list[dict], batch_indices: list[dict], staging: Path, prior_manifests: list[dict], count: int) -> tuple[list[dict], dict]:
    if count < 1:
        raise ValueError("Positive cohort size required")
    excluded_hashes, excluded_creators = prior_identities(prior_manifests)
    pending_sets = [{row["media_id"] for row in index["videos"] if row.get("status") == "pending" and not row.get("started_at")}
                    for index in batch_indices]
    if not pending_sets:
        raise ValueError("At least one batch index is required to establish unattempted status")
    eligible_ids = set.intersection(*pending_sets)
    # Filenames identify only source/creator; sidecars and selection labels remain unopened.
    staged = {sha256_file(path): path for path in sorted(Path(staging).glob("*.mp4"))}
    selected, seen = [], set(excluded_creators)
    for row in sorted(media, key=lambda item: item["sha256"]):
        digest = row["sha256"]
        if row["media_id"] not in eligible_ids or digest in excluded_hashes or not 4000 < row.get("duration_ms", 0) <= 180000:
            continue
        source = staged.get(digest)
        if source is None:
            continue
        creator = source.stem.rsplit("_", 1)[0]
        if creator in seen:
            continue
        selected.append({"case_id": f"future-{len(selected) + 1}", "source_path": str(source.resolve()), "source_sha256": digest,
                         "creator_identity": creator, "duration_ms": row["duration_ms"], "source_media_id": row["media_id"],
                         "prefix_end_ms": [2000, 4000, row["duration_ms"]]})
        seen.add(creator)
        if len(selected) == count:
            break
    if len(selected) < count:
        raise ValueError(f"Only {len(selected)} distinct-creator unattempted sources qualify")
    return selected, {"pending_in_every_index": len(eligible_ids), "excluded_source_hashes": len(excluded_hashes),
                      "excluded_development_creators": sorted(excluded_creators)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frozen-media", type=Path, required=True)
    parser.add_argument("--batch-index", type=Path, action="append", required=True)
    parser.add_argument("--exclude-manifest", type=Path, action="append", default=[])
    parser.add_argument("--staging", type=Path, required=True)
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    selection, diagnostics = select_unattempted(json.loads(args.frozen_media.read_text())["videos"],
        [json.loads(p.read_text()) for p in args.batch_index], args.staging,
        [json.loads(p.read_text()) for p in args.exclude_manifest], args.count)
    source_root = Path(__file__).resolve().parents[1]
    protocol = {"version": SCREENING_VERSION, "instruction_sha256": sha256_bytes(SCREENING_INSTRUCTION.encode()),
                "schema_sha256": sha256_json(SCREENING_SCHEMA), "provider": "not selected; freeze before execution",
                "planned_max_model_calls": 3 * args.count, "code_sha256": {str(p.relative_to(source_root)): sha256_file(p)
                    for p in sorted((source_root / "directorloop/screening").glob("*.py"))}}
    manifest = {"schema": "future-screening-validation-cohort-v1", "frozen_at": utc_now_iso(), "sources": selection,
                "selection_rule": "ascending SHA256; pending without started_at in every supplied batch index; exclude development source hashes and creators; one new creator per source",
                "selection_diagnostics": diagnostics, "protocol": protocol,
                "prior_development_manifests": [{"path": str(p), "sha256": sha256_file(p)} for p in args.exclude_manifest],
                "batch_index_receipts": [{"path": str(p), "sha256": sha256_file(p)} for p in args.batch_index],
                "frozen_media_receipt": {"path": str(args.frozen_media), "sha256": sha256_file(args.frozen_media)},
                "provider_calls": 0, "media_previewed": False, "asr_opened": False, "model_outputs_opened": False,
                "platform_outcomes_opened": False, "status": "frozen_pending_validation",
                "limitations": ["Untouched means no attempts in supplied local batch indices and no match to listed development sources/creators; external history is unknown.",
                                "Downloader cohort was assembled previously; this is not a representative random sample of platform videos.",
                                "Content, language, near duplicates and grounding quality are unreviewed. Freeze provider policy and spending separately before execution.",
                                "Previous sponsor and three-screen integration cases are development evidence, not fresh heldout evaluation."]}
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (args.output / "manifest.sha256").write_text(sha256_file(args.output / "manifest.json") + "\n")
    print(json.dumps({"output": str(args.output), "sources": len(selection), "provider_calls": 0, "status": manifest["status"]}))


if __name__ == "__main__":
    main()
