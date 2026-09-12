"""Project state: immutable version records, evaluation runs, coverage. JSON-persisted.

The jobs/API layer indexes this; the loop itself stays database-agnostic.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..domain.assets import AssetCoverageMap
from ..domain.decision import PromotionDecision
from ..domain.edit_plan import EditPlan
from ..domain.evaluation import EvaluationRun, LeakageControlResult
from ..domain.ids import utc_now_iso


class VersionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    index: int
    parent_version_id: str | None
    role: str  # baseline | candidate
    status: str = "unevaluated"  # baseline | promoted | rejected | needs_review | no_gain | insufficient_evidence | unevaluated
    plan: EditPlan
    plan_hash: str
    artifact_hash: str
    artifact_path: str
    duration_ms: int
    width: int
    height: int
    created_at: str = Field(default_factory=utc_now_iso)
    evaluation_run_id: str | None = None
    job_id: str | None = None
    diff_lines: list[str] = Field(default_factory=list)
    decision: PromotionDecision | None = None
    weave_url: str | None = None
    render_ms: int | None = None
    policy_mode: str | None = None  # learned | baseline

    @property
    def label(self) -> str:
        return f"V{self.index}"


class ProjectState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    name: str
    pack_dir: str
    versions: list[VersionRecord] = Field(default_factory=list)
    evaluations: dict[str, EvaluationRun] = Field(default_factory=dict)
    coverage: AssetCoverageMap | None = None
    coverage_model: str | None = None
    coverage_key: str | None = None  # probe model + manifest checksum the coverage was computed for
    leakage: LeakageControlResult | None = None
    best_version_id: str | None = None
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)

    def version(self, version_id: str) -> VersionRecord:
        for v in self.versions:
            if v.id == version_id:
                return v
        raise KeyError(version_id)

    def baseline(self) -> VersionRecord | None:
        return next((v for v in self.versions if v.role == "baseline"), None)

    def best(self) -> VersionRecord | None:
        if self.best_version_id:
            try:
                return self.version(self.best_version_id)
            except KeyError:
                return None
        return self.baseline()

    def next_index(self) -> int:
        return max((v.index for v in self.versions), default=-1) + 1

    def evaluation_for(self, version: VersionRecord) -> EvaluationRun | None:
        return self.evaluations.get(version.evaluation_run_id or "")

    def save(self, path: Path) -> None:
        self.updated_at = utc_now_iso()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.model_dump(mode="json")), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> ProjectState:
        return cls.model_validate(json.loads(path.read_text(encoding="utf-8")))


def project_dir(data_dir: Path, project_id: str) -> Path:
    p = data_dir / "projects" / project_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def state_path(data_dir: Path, project_id: str) -> Path:
    return project_dir(data_dir, project_id) / "state.json"


def load_or_create_state(data_dir: Path, project_id: str, name: str, pack_dir: str) -> ProjectState:
    path = state_path(data_dir, project_id)
    if path.exists():
        return ProjectState.load(path)
    state = ProjectState(project_id=project_id, name=name, pack_dir=pack_dir)
    state.save(path)
    return state
