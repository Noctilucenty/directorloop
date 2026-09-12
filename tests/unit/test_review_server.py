"""Blinded review server: no labels leak, sides are randomized, answers are stored once per session."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from directorloop.domain.creative import (
    CreativeExperiment,
    CreativeFitness,
    EvidenceClass,
    ExperimentArm,
    FitnessComponent,
)
from directorloop.media.probe import FFMPEG
from directorloop.review.server import create_app, summarize


def _clip(path: Path, color: str) -> None:
    subprocess.run(
        [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=90x160:d=3:r=15",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        check=True, timeout=60,
    )


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    renders = tmp_path / "renders"
    hooks = tmp_path / "creative" / "cache" / "hooks"
    renders.mkdir(parents=True)
    hooks.mkdir(parents=True)
    arms = []
    for label, color in (("control", "navy"), ("A", "olive")):
        full = renders / f"{label}_{'a' * 20}.mp4"
        _clip(full, color)
        _clip(hooks / f"{full.stem[:40]}_hook3000.mp4", color)
        fitness = CreativeFitness(arm_id=f"exp1_{label}", hard_gates_passed=True, components=[
            FitnessComponent(name="model_hook_preference_vs_control", value=None if label == "control" else 0.75, unit="share of 4 calls", evidence=EvidenceClass.MODEL_EVAL)])
        arms.append(ExperimentArm(id=f"exp1_{label}", label=label, artifact_path=str(full), artifact_hash=label * 10, fitness=fitness, status="evaluated"))
    exp = CreativeExperiment(id="cexp_test1", project_id="p", video_id="v", question="q", policy_mode="learned", policy_version_before=0,
                             genome_id="g", hypotheses=[], ranking=[], arms=arms, suite_id="s", suite_hash="h", status="completed")
    out = tmp_path / "creative" / "experiments"
    out.mkdir(parents=True)
    (out / "cexp_test1.json").write_text(exp.model_dump_json(), encoding="utf-8")
    return tmp_path


def _assign(client: TestClient) -> str:
    r = client.get("/next", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("t/")
    return r.headers["location"].removeprefix("t/")


def test_pages_never_reveal_arms_or_roles(data_dir: Path) -> None:
    client = TestClient(create_app(data_dir=data_dir, secret="s" * 32))
    assert client.get("/").status_code == 200
    token = _assign(client)
    page = client.get(f"/t/{token}").text
    for leak in ("exp1_control", "exp1_a", "control", "variant", "baseline", "original", "improved", "cexp_test1"):
        assert not re.search(rf"\b{leak}\b", page.lower()), leak
    import base64

    payload = token.split(".")[0]
    decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode(errors="ignore")
    for leak in ("exp1_control", "exp1_A", "cexp_test1"):
        assert leak not in decoded, "the URL token must not carry arm or experiment ids"
    assert client.get(f"/m/{token}/left.mp4").status_code == 200
    assert client.get(f"/m/{token}/right.mp4").status_code == 200
    assert client.get(f"/m/{token}/other.mp4").status_code == 404
    assert client.get("/t/forged-token").status_code == 404


def test_sides_are_randomized_across_assignments(data_dir: Path) -> None:
    app = create_app(data_dir=data_dir, secret="s" * 32)
    import json

    for _ in range(30):
        client = TestClient(app)
        token = _assign(client)
        assert client.get(f"/m/{token}/left.mp4").status_code == 200
    rows = [json.loads(line) for line in (data_dir / "reviews" / "assignments.jsonl").read_text().splitlines()]
    lefts = {r["l"] for r in rows}
    assert len(rows) == 30 and lefts == {"exp1_control", "exp1_A"}, "both arms must appear on the left across assignments"
    left_counts = sum(1 for r in rows if r["l"] == "exp1_control")
    assert 5 <= left_counts <= 25, left_counts


def test_answers_stored_once_per_session_and_summarized(data_dir: Path) -> None:
    app = create_app(data_dir=data_dir, secret="s" * 32)
    client = TestClient(app)
    token = _assign(client)
    r = client.post(f"/t/{token}/answer", json={"choice": "left", "about": "a spinning top"})
    assert r.status_code == 200 and r.json()["recorded"] is True
    assert client.post(f"/t/{token}/answer", json={"choice": "right"}).status_code == 409
    assert client.post(f"/t/{token}/answer", json={"choice": "bogus"}).status_code == 422
    other = TestClient(app)
    token2 = _assign(other)
    assert other.post(f"/t/{token2}/answer", json={"choice": "none"}).status_code == 200
    rows = summarize(data_dir)
    assert len(rows) == 1 and rows[0]["n"] == 2 and rows[0]["no_preference"] == 1
    assert sum(rows[0]["preferred"].values()) == 1
    assert "not retention" in rows[0]["label"]
    after = client.get("/next", follow_redirects=False)
    assert after.headers["location"].startswith("done"), "a session sees each pair once"
    assert re.fullmatch(r"[0-9a-f]{24}", (data_dir / "reviews" / "reviews.jsonl").read_text().split('"session_hash":"')[1][:24])
