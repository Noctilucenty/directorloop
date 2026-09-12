"""Demo packs on disk.

A pack directory contains:
  brief.json            CreativeBrief
  source_truth.json     SourceTruth
  suite.json            EvaluationSuite (hidden answers; never sent to probes)
  assets/manifest.json  AssetManifest; media files live next to it, named by content hash
  baseline_plan.json    EditPlan for the ordinary one-shot baseline
  checksums.json        content hashes of every file above (written by `write_checksums`)
  PACK.md               provenance, rights and labeling
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .assets import AssetManifest, AssetRecord
from .brief import CreativeBrief
from .edit_plan import EditPlan
from .ids import sha256_file
from .truth import EvaluationSuite, SourceTruth


class DemoPack(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    root: Path
    brief: CreativeBrief
    truth: SourceTruth
    suite: EvaluationSuite
    manifest: AssetManifest
    baseline_plan: EditPlan
    checksums: dict[str, str]

    @property
    def project_id(self) -> str:
        return self.brief.project_id

    def asset_path(self, asset: AssetRecord | str) -> Path:
        rec = asset if isinstance(asset, AssetRecord) else self.manifest.get(asset)
        return self.root / "assets" / rec.storage_key


def _read_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_pack(root: str | Path, verify_checksums: bool = True) -> DemoPack:
    root = Path(root)
    brief = CreativeBrief.model_validate(_read_json(root / "brief.json"))
    truth = SourceTruth.model_validate(_read_json(root / "source_truth.json"))
    suite = EvaluationSuite.model_validate(_read_json(root / "suite.json"))
    manifest = AssetManifest.model_validate(_read_json(root / "assets" / "manifest.json"))
    plan = EditPlan.model_validate(_read_json(root / "baseline_plan.json"))
    checks_path = root / "checksums.json"
    checksums: dict[str, str] = _read_json(checks_path) if checks_path.exists() else {}
    if verify_checksums and checksums:
        for rel, expected in checksums.items():
            actual = sha256_file(root / rel)
            if actual != expected:
                raise ValueError(f"checksum mismatch for {rel}: expected {expected[:12]}, got {actual[:12]}")
    for asset in manifest.assets:
        path = root / "assets" / asset.storage_key
        if not path.exists():
            raise FileNotFoundError(f"asset {asset.id} missing at {path}")
        if verify_checksums and sha256_file(path) != asset.content_hash:
            raise ValueError(f"asset {asset.id} content hash does not match its file")
    for asset_id in plan.asset_ids():
        if not manifest.has(asset_id):
            raise ValueError(f"baseline plan references unknown asset {asset_id}")
    return DemoPack(
        root=root, brief=brief, truth=truth, suite=suite, manifest=manifest, baseline_plan=plan, checksums=checksums
    )


def write_checksums(root: str | Path) -> dict[str, str]:
    root = Path(root)
    files = ["brief.json", "source_truth.json", "suite.json", "assets/manifest.json", "baseline_plan.json"]
    manifest = AssetManifest.model_validate(_read_json(root / "assets" / "manifest.json"))
    files += [f"assets/{a.storage_key}" for a in manifest.assets]
    checks = {rel: sha256_file(root / rel) for rel in files if (root / rel).exists()}
    with open(root / "checksums.json", "w", encoding="utf-8") as fh:
        json.dump(checks, fh, indent=2, sort_keys=True)
    return checks
