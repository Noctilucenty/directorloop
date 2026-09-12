"""DirectorLoop API (creative experiments). Contract: docs/API_CREATIVE.md.

Loopback by default. Optional bearer token (DL_LOCAL_AUTH_TOKEN) for /api. Media is served only from the
render store, the hook-clip cache and registered source videos, with strict name validation.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import re
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..config import REPO_ROOT, Settings, get_settings
from ..creative.design import design_experiment
from ..creative.experiment import classify_arm, run_creative_experiment
from ..creative.genome import extract_genome
from ..creative.investigate import investigate
from ..creative.mutate import experiment_brief, identity_plan, source_manifest
from ..creative.policy import load_policy, save_policy
from ..domain.creative import CreativeExperiment, ReferenceCorpus, RetentionSeries
from ..jobs import IdempotencyConflict, JobStore, JobWorker
from ..observability import init_weave, weave_status
from ..observability.weave_ops import flush
from ..providers import build_providers
from ..review.server import summarize as review_summary

SHA_RE = re.compile(r"^[0-9a-f]{64}$")
HOOK_RE = re.compile(r"^[0-9a-f]{40}_hook3000$")
VIDEO_ID_RE = re.compile(r"^[a-z0-9_\-]{2,80}$")

DEMO_VIDEOS = [
    {"video_id": "aptip", "role": "demo_a", "path": "/Users/leon/Desktop/dev/Curio-Automation/data/productions/AP-TIPPE-V4/aptip-custom-captioned.mp4", "title": "Tippe top climbs instead of falling"},
    {"video_id": "aplaze", "role": "demo_b", "path": "/Users/leon/Desktop/dev/Curio-Automation/data/productions/AP-LAZE-NARRATED-01/aplaze-custom-captioned.mp4", "title": "Laze: the white cloud that contains glass"},
]


class ExperimentRequest(BaseModel):
    video_id: str = Field(pattern=VIDEO_ID_RE.pattern)
    policy_mode: str = Field(default="learned", pattern="^(learned|none)$")
    max_arms: int = Field(default=3, ge=1, le=6)
    record_policy: bool = True
    idempotency_key: str | None = Field(default=None, max_length=120)


class Services:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.data = settings.data_dir
        self.creative = self.data / "creative"
        self.policy_path = self.creative / "policy.json"
        self.policy_lock = threading.Lock()
        self.providers = build_providers(settings)
        self.jobs = JobStore(self.data / "directorloop_jobs.db")
        self.worker = JobWorker(self.jobs, {"experiment": self.run_experiment_job})
        self._registry: dict[str, dict[str, Any]] | None = None

    # ---- registry -------------------------------------------------------------
    def corpus(self) -> ReferenceCorpus | None:
        for name in ("curio_corpus_with_patterns.json", "curio_references.json"):
            p = self.data / "corpus" / name
            if p.exists():
                return ReferenceCorpus.model_validate_json(p.read_text(encoding="utf-8"))
        return None

    def registry(self) -> dict[str, dict[str, Any]]:
        reg: dict[str, dict[str, Any]] = {}
        for v in DEMO_VIDEOS:
            if Path(v["path"]).exists():
                reg[v["video_id"]] = {**v, "category": "educational_short", "source": "owned_curio", "retention": None}
        corpus = self.corpus()
        if corpus is not None:
            for ref in corpus.references:
                vid = re.sub(r"[^a-z0-9_\-]", "-", ref.id.removeprefix("ref_").lower())[:80]
                if vid in reg or not ref.media_path or not Path(ref.media_path).exists():
                    continue
                retention = ref.performance
                curve = (ref.provenance or {}).get("facebook_retention_curve")
                if retention is None and curve:
                    retention = RetentionSeries.model_validate(curve)
                reg[vid] = {"video_id": vid, "role": "reference", "path": ref.media_path, "title": ref.title or vid, "category": ref.category,
                            "source": ref.source, "retention": retention.model_dump(mode="json") if retention else None}
        self._registry = reg
        return reg

    def video(self, video_id: str) -> dict[str, Any]:
        if not VIDEO_ID_RE.match(video_id):
            raise HTTPException(status_code=404, detail="unknown video")
        reg = self._registry or self.registry()
        if video_id not in reg:
            reg = self.registry()
        if video_id not in reg:
            raise HTTPException(status_code=404, detail="unknown video")
        return reg[video_id]

    def experiments(self) -> list[CreativeExperiment]:
        out = []
        d = self.creative / "experiments"
        if not d.exists():
            return out
        for f in d.glob("cexp_*.json"):
            if f.name.endswith(".detail.json"):
                continue
            try:
                out.append(CreativeExperiment.model_validate_json(f.read_text(encoding="utf-8")))
            except ValueError:
                continue
        return sorted(out, key=lambda e: e.created_at, reverse=True)

    # ---- jobs -----------------------------------------------------------------
    def run_experiment_job(self, job, on_stage) -> dict[str, Any]:  # noqa: ANN001
        params = job.params
        v = self.video(params["video_id"])
        with self.policy_lock:
            policy = load_policy(self.policy_path)
            exp = run_creative_experiment(
                video_id=v["video_id"], video_path=Path(v["path"]), providers=self.providers, data_dir=self.data, policy=policy, corpus=self.corpus(),
                retention=RetentionSeries.model_validate(v["retention"]) if v.get("retention") else None, category=v.get("category", "educational_short"),
                policy_mode=params.get("policy_mode", "learned"), max_arms=int(params.get("max_arms", 3)), record_policy=bool(params.get("record_policy", True)),
                on_stage=on_stage,
            )
            if params.get("record_policy", True):
                save_policy(policy, self.policy_path)
        flush()
        return {"experiment_id": exp.id, "outcome": exp.decision.outcome if exp.decision else None, "weave_url": exp.weave_url}


def _media_url(path: str | None) -> str | None:
    if not path:
        return None
    p = Path(path)
    if SHA_RE.match(p.stem):
        return f"/media/renders/{p.stem}.mp4"
    return None


def _hook_url(path: str | None, data: Path) -> str | None:
    if not path:
        return None
    stem = Path(path).stem[:40]
    f = data / "creative" / "cache" / "hooks" / f"{stem}_hook3000.mp4"
    return f"/media/hooks/{stem}_hook3000.mp4" if f.exists() else None


def create_app(settings: Settings | None = None, start_worker: bool = True) -> FastAPI:
    settings = settings or get_settings()
    services = Services(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        init_weave(settings)
        services.jobs.recover_stale(lease_seconds=180)
        if start_worker:
            services.worker.start()
        yield
        services.worker.stop()

    app = FastAPI(title="DirectorLoop", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.services = services

    def auth(request: Request) -> None:
        token = settings.dl_local_auth_token
        if token and request.headers.get("authorization") != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="missing or invalid bearer token")

    # ---- health ----------------------------------------------------------------
    @app.get("/api/health", dependencies=[Depends(auth)])
    def health() -> dict[str, Any]:
        w = weave_status()
        corpus = services.corpus()
        public = services.data / "reviews" / "public_url.txt"
        return {
            "status": "ok",
            "weave": {"connected": w.connected, "project": w.project, "traces_url": w.traces_url, "reason": w.reason},
            "providers": [{"name": c.name, "role": c.role, "model": c.model, "state": c.state} for c in services.providers.capabilities if c.present],
            "policy_version": load_policy(services.policy_path).version,
            "corpus": {"references": len(corpus.references) if corpus else 0, "with_metrics": sum(1 for r in corpus.references if r.performance) if corpus else 0,
                       "patterns": len(corpus.patterns) if corpus else 0},
            "review": {"public_url": public.read_text().strip() if public.exists() else None, "local_url": "http://127.0.0.1:8790"},
        }

    # ---- videos ----------------------------------------------------------------
    @app.get("/api/videos", dependencies=[Depends(auth)])
    def videos() -> list[dict[str, Any]]:
        exps = services.experiments()
        out = []
        for v in services.registry().values():
            latest = next((e.id for e in exps if e.video_id == v["video_id"] and e.status == "completed"), None)
            genome_cached = any((services.creative / "genomes").glob("genome_*.json")) if (services.creative / "genomes").exists() else False
            out.append({"video_id": v["video_id"], "title": v["title"], "duration_ms": None, "category": v["category"], "source": v["source"],
                        "media_url": f"/media/source/{v['video_id']}.mp4", "role": v["role"], "has_genome": genome_cached,
                        "retention_class": ({"historical_owned": "historical", "real_platform": "real", "simulated_demo": "simulated"}.get(v["retention"]["source_type"]) if v.get("retention") else None),
                        "latest_experiment_id": latest})
        order = {"demo_a": 0, "demo_b": 1, "dev": 2, "holdout": 3, "reference": 4}
        return sorted(out, key=lambda x: (order.get(x["role"], 9), x["title"]))

    def _genome(v: dict[str, Any]):  # noqa: ANN202
        if services.providers.probe is None:
            raise HTTPException(status_code=503, detail="no probe provider configured")
        return extract_genome(Path(v["path"]), services.providers.probe, services.creative / "genomes", category=v.get("category", "educational_short"))

    @app.get("/api/videos/{video_id}", dependencies=[Depends(auth)])
    def video_detail(video_id: str) -> dict[str, Any]:
        v = services.video(video_id)
        g = _genome(v)
        retention = RetentionSeries.model_validate(v["retention"]) if v.get("retention") else None
        inv = investigate(g, retention=retention, corpus=services.corpus(), scope=v.get("category", "educational_short"))
        gd = g.model_dump(mode="json")
        return {
            "video_id": v["video_id"], "title": v["title"], "duration_ms": g.duration_ms, "category": v["category"], "source": v["source"],
            "media_url": f"/media/source/{v['video_id']}.mp4", "role": v["role"], "has_genome": True,
            "retention_class": None if retention is None else retention.evidence_class.value,
            "latest_experiment_id": next((e.id for e in services.experiments() if e.video_id == video_id and e.status == "completed"), None),
            "genome": {k: gd[k] for k in ("artifact_hash", "duration_ms", "beats", "shots", "hook", "first_proof_ms", "first_payoff_ms", "context_before_proof_ms",
                                          "longest_static_span_ms", "longest_static_span_start_ms", "shots_per_10s", "speech_rate_wps", "open_loops", "motion_curve")},
            "investigation": {"weak_region": inv.weak_region.model_dump(mode="json") if inv.weak_region else None,
                              "hypotheses": [h.model_dump(mode="json") for h in inv.hypotheses], "retention_note": inv.retention_note},
            "retention": v.get("retention"),
        }

    @app.get("/api/videos/{video_id}/design", dependencies=[Depends(auth)])
    def design_preview(video_id: str, mode: str = "learned") -> dict[str, Any]:
        if mode not in ("learned", "none"):
            raise HTTPException(status_code=422, detail="mode must be learned or none")
        v = services.video(video_id)
        g = _genome(v)
        vp = Path(v["path"])
        project = f"proj_{video_id}"
        policy = load_policy(services.policy_path)
        corpus = services.corpus()
        inv = investigate(g, corpus=corpus, scope=v.get("category", "educational_short"))
        d = design_experiment(hypotheses=inv.hypotheses, genome=g, plan=identity_plan(g), manifest=source_manifest(project, vp, g.artifact_hash),
                              brief=experiment_brief(project, g, ""), video_path=vp, parent_version_id="v0", policy=policy, corpus=corpus,
                              policy_mode=mode, max_arms=3, category=v.get("category", "educational_short"))
        selected = {id(c) for c in d.selected}
        return {"video_id": video_id, "mode": mode, "policy_version": policy.version, "skipped": d.skipped, "rankings": [
            {"mutation": c.mutation.type.value, "hypothesis_id": c.hypothesis.id, "family": c.hypothesis.family.value, "score": c.ranking.score,
             "detection_confidence": c.ranking.detection_confidence, "reference_support": c.ranking.reference_support, "policy_mean": c.ranking.policy_mean,
             "policy_wins": c.ranking.policy_wins, "policy_losses": c.ranking.policy_losses, "policy_neutral": c.ranking.policy_neutral, "reason": c.ranking.reason,
             "description": c.mutation.description, "changed_variable": c.mutation.changed_variable, "protected_variables": c.mutation.protected_variables,
             "target_start_ms": c.mutation.target_start_ms, "target_end_ms": c.mutation.target_end_ms, "selected": id(c) in selected} for c in d.candidates]}

    # ---- experiments and jobs ---------------------------------------------------
    @app.post("/api/experiments", dependencies=[Depends(auth)])
    def start_experiment(body: ExperimentRequest) -> dict[str, Any]:
        services.video(body.video_id)
        params = body.model_dump(exclude={"idempotency_key"})
        try:
            job, created = services.jobs.create("experiment", params, idempotency_key=body.idempotency_key)
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"job_id": job.id, "created": created}

    @app.get("/api/jobs/{job_id}", dependencies=[Depends(auth)])
    def job_view(job_id: str) -> dict[str, Any]:
        job = services.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404)
        return job.view()

    @app.post("/api/jobs/{job_id}/cancel", dependencies=[Depends(auth)])
    def job_cancel(job_id: str) -> dict[str, Any]:
        job = services.jobs.request_cancel(job_id)
        if job is None:
            raise HTTPException(status_code=404)
        return job.view()

    @app.get("/api/jobs/{job_id}/events.json", dependencies=[Depends(auth)])
    def job_events_json(job_id: str, after: int = 0) -> list[dict[str, Any]]:
        if services.jobs.get(job_id) is None:
            raise HTTPException(status_code=404)
        return services.jobs.events(job_id, after=after)

    @app.get("/api/jobs/{job_id}/events", dependencies=[Depends(auth)])
    async def job_events_sse(job_id: str, request: Request, after: int = 0) -> StreamingResponse:
        if services.jobs.get(job_id) is None:
            raise HTTPException(status_code=404)

        async def stream():  # noqa: ANN202
            last = after
            idle = 0
            while True:
                if await request.is_disconnected():
                    return
                evs = services.jobs.events(job_id, after=last)
                for e in evs:
                    last = e["seq"]
                    yield f"id: {e['seq']}\ndata: {json.dumps(e, default=str)}\n\n"
                job = services.jobs.get(job_id)
                if job is not None and job.state in ("COMPLETED", "FAILED", "CANCELED") and not evs:
                    yield f"event: end\ndata: {json.dumps(job.view(), default=str)}\n\n"
                    return
                idle = 0 if evs else idle + 1
                if idle and idle % 30 == 0:
                    yield ": keepalive\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    def _summary(e: CreativeExperiment) -> dict[str, Any]:
        winner = next((a.label for a in e.arms if e.decision and a.id == e.decision.winner_arm_id), None)
        return {"id": e.id, "video_id": e.video_id, "created_at": e.created_at, "policy_mode": e.policy_mode, "generation": e.generation,
                "outcome": e.decision.outcome if e.decision else None, "winner_label": winner, "arms": len(e.arms), "status": e.status,
                "policy_version_before": e.policy_version_before, "policy_version_after": e.policy_version_after, "total_ms": e.timings_ms.get("total_ms"),
                "weave_url": e.weave_url, "recorded": e.policy_version_after is not None}

    @app.get("/api/experiments", dependencies=[Depends(auth)])
    def experiments() -> list[dict[str, Any]]:
        return [_summary(e) for e in services.experiments()]

    @app.get("/api/experiments/{exp_id}", dependencies=[Depends(auth)])
    def experiment_detail(exp_id: str) -> dict[str, Any]:
        if not re.match(r"^cexp_[0-9a-f]+_[0-9a-f]+$", exp_id):
            raise HTTPException(status_code=404)
        f = services.creative / "experiments" / f"{exp_id}.json"
        if not f.exists():
            raise HTTPException(status_code=404)
        e = CreativeExperiment.model_validate_json(f.read_text(encoding="utf-8"))
        detail_path = services.creative / "experiments" / f"{exp_id}.detail.json"
        detail = json.loads(detail_path.read_text(encoding="utf-8")) if detail_path.exists() else {}
        control = next((a for a in e.arms if a.label == "control"), None)
        arms = []
        for a in e.arms:
            outcome = reason = None
            if a.label != "control" and control and control.fitness and a.fitness:
                outcome, reason = classify_arm(control.fitness, a.fitness)
                if e.decision:
                    outcome = e.decision.per_arm.get(a.id, outcome)
            d = (detail.get("per_arm_detail") or {}).get(a.label, {})
            arms.append({
                "id": a.id, "label": a.label, "status": a.status, "media_url": _media_url(a.artifact_path), "hook_media_url": _hook_url(a.artifact_path, services.data),
                "duration_ms": a.duration_ms, "render_ms": a.render_ms, "outcome": outcome, "outcome_reason": reason,
                "mutation": None if a.mutation is None else {"type": a.mutation.type.value, "description": a.mutation.description, "changed_variable": a.mutation.changed_variable,
                                                             "protected_variables": a.mutation.protected_variables, "hypothesis_id": a.mutation.hypothesis_id,
                                                             "target_start_ms": a.mutation.target_start_ms, "target_end_ms": a.mutation.target_end_ms,
                                                             "expected_benefit": a.mutation.expected_benefit, "risk": a.mutation.risk},
                "fitness": None if a.fitness is None else {"hard_gates_passed": a.fitness.hard_gates_passed, "gate_notes": a.fitness.gate_notes,
                                                           "components": [c.model_dump(mode="json") for c in a.fitness.components]},
                "preference_reasons": {"full": d.get("full_preference_reasons", []), "hook": d.get("hook_preference_reasons", [])},
            })
        suite_path = next((services.creative / "cache").glob("suite_*_v1.json"), None) if (services.creative / "cache").exists() else None
        suite_q: list[dict[str, str]] = []
        for sp in (services.creative / "cache").glob("suite_*_v1.json") if (services.creative / "cache").exists() else []:
            data = json.loads(sp.read_text(encoding="utf-8"))
            if data.get("id") == e.suite_id:
                suite_q = [{"id": q["id"], "text": q["text"]} for q in data.get("questions", [])]
                break
        _ = suite_path
        return {**_summary(e), "question": e.question, "hypotheses": [h.model_dump(mode="json") for h in e.hypotheses],
                "ranking": [r.model_dump(mode="json") for r in e.ranking], "arms": arms,
                "decision": e.decision.model_dump(mode="json") if e.decision else None, "policy_updates": e.policy_updates,
                "reference_patterns": detail.get("reference_patterns", []), "suite": {"id": e.suite_id, "hash": e.suite_hash, "questions": suite_q},
                "timings_ms": e.timings_ms, "model_calls": e.model_calls, "notes": e.notes}

    # ---- policy, corpus, transfer, reviews --------------------------------------
    @app.get("/api/policy", dependencies=[Depends(auth)])
    def policy_view() -> dict[str, Any]:
        p = load_policy(services.policy_path)
        return {"version": p.version, "strategies": [
            {**row, "evidence": [{"experiment_id": ev.experiment_id, "video_id": ev.video_id, "outcome": ev.outcome, "note": ev.note, "weave_url": ev.weave_url}
                                 for ev in next(s for s in p.strategies if s.id == row["id"]).evidence]} for row in p.summary()],
            "changes": [c.model_dump(mode="json") for c in p.changes]}

    @app.get("/api/corpus", dependencies=[Depends(auth)])
    def corpus_view() -> dict[str, Any]:
        c = services.corpus()
        if c is None:
            return {"id": None, "version": 0, "references": 0, "with_metrics": 0, "patterns": [], "label": "REFERENCE CREATIVE (owned Curio shorts; descriptive counts)"}
        return {"id": c.id, "version": c.version, "references": len(c.references), "with_metrics": sum(1 for r in c.references if r.performance),
                "patterns": [p.model_dump(mode="json") for p in c.patterns], "label": "REFERENCE CREATIVE (owned Curio shorts; descriptive counts)"}

    @app.get("/api/transfer", dependencies=[Depends(auth)])
    def transfer_view() -> list[dict[str, Any]]:
        d = services.creative / "transfer"
        out = []
        for f in sorted(d.glob("*_transfer_report.json"), reverse=True) if d.exists() else []:
            rep = json.loads(f.read_text(encoding="utf-8"))
            rep["created_at"] = f.name.split("_")[0]
            out.append(rep)
        return out

    @app.get("/api/reviews/summary", dependencies=[Depends(auth)])
    def reviews_summary() -> dict[str, Any]:
        rows = review_summary(services.data)
        labels: dict[str, str] = {}
        for e in services.experiments():
            for a in e.arms:
                labels[a.id] = f"{e.video_id} {a.label}" + (f" ({a.mutation.type.value})" if a.mutation else " (original)")
        for r in rows:
            r["arm_labels"] = {a: labels.get(a, a) for a in r["arms"]}
        return {"pairs": rows, "total_responses": sum(r["n"] for r in rows)}

    @app.get("/api/reviews/qr", dependencies=[Depends(auth)])
    def reviews_qr() -> dict[str, Any]:
        public = services.data / "reviews" / "public_url.txt"
        if not public.exists():
            return {"url": None, "qr_png": None, "note": "no public review tunnel is running"}
        url = public.read_text().strip()
        import qrcode

        buf = io.BytesIO()
        qrcode.make(url, box_size=10, border=2).save(buf, format="PNG")
        return {"url": url, "qr_png": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(), "note": "blinded continue-watching test; answers are a small convenience sample"}

    # ---- media ------------------------------------------------------------------
    @app.get("/media/renders/{name}.mp4")
    def media_render(name: str) -> FileResponse:
        if not SHA_RE.match(name):
            raise HTTPException(status_code=404)
        f = services.data / "renders" / f"{name}.mp4"
        if not f.exists():
            raise HTTPException(status_code=404)
        return FileResponse(f, media_type="video/mp4")

    @app.get("/media/hooks/{name}.mp4")
    def media_hook(name: str) -> FileResponse:
        if not HOOK_RE.match(name):
            raise HTTPException(status_code=404)
        f = services.data / "creative" / "cache" / "hooks" / f"{name}.mp4"
        if not f.exists():
            raise HTTPException(status_code=404)
        return FileResponse(f, media_type="video/mp4")

    @app.get("/media/source/{video_id}.mp4")
    def media_source(video_id: str) -> FileResponse:
        v = services.video(video_id)
        return FileResponse(v["path"], media_type="video/mp4")

    # ---- web client ---------------------------------------------------------------
    dist = REPO_ROOT / "apps" / "web" / "dist"
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> FileResponse:
            if path.startswith(("api/", "media/")):
                raise HTTPException(status_code=404)
            candidate = (dist / path).resolve()
            if path and candidate.is_file() and dist.resolve() in candidate.parents:
                return FileResponse(candidate)
            if Path(path).suffix:  # a missing file is a 404, not the app shell
                raise HTTPException(status_code=404)
            return FileResponse(dist / "index.html")

    @app.exception_handler(ValueError)
    async def value_error(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)[:300]}, status_code=422)

    return app


def main() -> None:  # pragma: no cover
    import uvicorn

    s = get_settings()
    uvicorn.run(create_app(s), host=s.dl_bind_host, port=s.dl_port, log_level="info")


if __name__ == "__main__":  # pragma: no cover
    main()
