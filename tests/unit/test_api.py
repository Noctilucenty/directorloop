"""API contract checks against a temporary data directory (no provider keys, no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from directorloop.api.app import create_app
from directorloop.config import Settings
from directorloop.creative.policy import CreativePolicyStore, StrategyEvidence, save_policy
from directorloop.domain.creative import (
    CreativeExperiment,
    CreativeFitness,
    EvidenceClass,
    ExperimentArm,
    ExperimentDecision,
    FitnessComponent,
    MutationType,
)
from tests.unit.test_creative_mutations import make_genome  # noqa: F401  (keeps fixtures importable)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    data = tmp_path / "data"
    (data / "creative" / "experiments").mkdir(parents=True)
    (data / "renders").mkdir(parents=True)
    sha = "a" * 64
    (data / "renders" / f"{sha}.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    comps = [FitnessComponent(name="model_full_preference_vs_control", value=0.0, unit="share of 4 calls", evidence=EvidenceClass.MODEL_EVAL, valid=4),
             FitnessComponent(name="message_comprehension", value=3, evidence=EvidenceClass.MODEL_EVAL)]
    control_fit = CreativeFitness(arm_id="cexp_1a_2b_control", hard_gates_passed=True, components=[FitnessComponent(name="message_comprehension", value=3, evidence=EvidenceClass.MODEL_EVAL)])
    arms = [ExperimentArm(id="cexp_1a_2b_control", label="control", artifact_path=str(data / "renders" / f"{sha}.mp4"), fitness=control_fit, status="evaluated"),
            ExperimentArm(id="cexp_1a_2b_A", label="A", artifact_path=str(data / "renders" / f"{sha}.mp4"), fitness=CreativeFitness(arm_id="cexp_1a_2b_A", hard_gates_passed=True, components=comps), status="evaluated")]
    exp = CreativeExperiment(id="cexp_1a_2b", project_id="p", video_id="aptip", question="q", policy_mode="learned", policy_version_before=0, policy_version_after=1,
                             genome_id="g", hypotheses=[], ranking=[], arms=arms, suite_id="s", suite_hash="h", status="completed", created_at="2026-09-12T20:00:00Z",
                             decision=ExperimentDecision(outcome="no_clear_winner", reason="r", primary_metric="model_full_preference_vs_control", per_arm={"cexp_1a_2b_A": "loss"}))
    (data / "creative" / "experiments" / "cexp_1a_2b.json").write_text(exp.model_dump_json())
    store = CreativePolicyStore()
    store.record_outcome(MutationType.PAYOFF_EARLIER, "educational_short", StrategyEvidence(experiment_id="cexp_1a_2b", video_id="aptip", arm_id="A", outcome="loss", evidence_class="model_eval", primary_metric="m"))
    save_policy(store, data / "creative" / "policy.json")
    tdir = data / "creative" / "transfer"
    tdir.mkdir(parents=True)
    (tdir / "20260912-131738_aplaze_transfer_report.json").write_text(json.dumps({"video_id": "aplaze", "modes": {}, "first_choice_changed": True}))
    settings = Settings(_env_file=None, dl_data_dir=str(data), dl_weave_enabled=False)
    return TestClient(create_app(settings, start_worker=False))


def test_health_policy_experiments_transfer(client: TestClient) -> None:
    with client:
        h = client.get("/api/health").json()
        assert h["status"] == "ok" and h["policy_version"] == 1 and h["weave"]["connected"] is False
        pol = client.get("/api/policy").json()
        assert pol["version"] == 1 and pol["strategies"][0]["status"] == "CONTRADICTED" and pol["strategies"][0]["evidence"][0]["outcome"] == "loss"
        exps = client.get("/api/experiments").json()
        assert exps[0]["id"] == "cexp_1a_2b" and exps[0]["outcome"] == "no_clear_winner"
        detail = client.get("/api/experiments/cexp_1a_2b").json()
        arm = next(a for a in detail["arms"] if a["label"] == "A")
        assert arm["outcome"] == "loss" and arm["media_url"] == "/media/renders/" + "a" * 64 + ".mp4"
        assert "/x/" not in json.dumps(detail), "no local file paths leak to the client"
        assert client.get("/api/transfer").json()[0]["video_id"] == "aplaze"
        assert client.get("/api/experiments/not-an-id").status_code == 404


def test_media_paths_are_strictly_validated(client: TestClient) -> None:
    with client:
        assert client.get("/media/renders/" + "a" * 64 + ".mp4").status_code == 200
        assert client.get("/media/renders/..%2F..%2Fetc%2Fpasswd.mp4").status_code == 404
        assert client.get("/media/hooks/../../secret.mp4").status_code == 404
        assert client.get("/media/hooks/abc_hook3000.mp4").status_code == 404
        assert client.get("/media/renders/" + "b" * 64 + ".mp4").status_code == 404
        assert client.get("/media/source/UNKNOWN.mp4").status_code == 404


def test_experiment_jobs_are_idempotent(client: TestClient) -> None:
    with client:
        body = {"video_id": "aptip", "policy_mode": "learned", "max_arms": 3, "idempotency_key": "click-1"}
        r1 = client.post("/api/experiments", json=body)
        if r1.status_code == 404:
            pytest.skip("demo video not present on this machine")
        r2 = client.post("/api/experiments", json=body)
        assert r1.json()["job_id"] == r2.json()["job_id"] and r2.json()["created"] is False
        assert client.post("/api/experiments", json={**body, "max_arms": 2}).status_code == 409
        job = client.get(f"/api/jobs/{r1.json()['job_id']}").json()
        assert job["state"] == "QUEUED"
        assert client.post(f"/api/jobs/{job['id']}/cancel").json()["state"] == "CANCELED"
        assert client.post("/api/experiments", json={"video_id": "../../etc", "idempotency_key": "x"}).status_code == 422


def test_bearer_token_required_when_configured(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, dl_data_dir=str(tmp_path / "d"), dl_weave_enabled=False, dl_local_auth_token="t0ken")
    with TestClient(create_app(settings, start_worker=False)) as c:
        assert c.get("/api/health").status_code == 401
        assert c.get("/api/health", headers={"Authorization": "Bearer t0ken"}).status_code == 200
