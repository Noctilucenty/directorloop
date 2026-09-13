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
import tempfile
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from ..audit.models import OBJECTIVES, AuditReport, RepairRun
from ..audit.review import run_audit
from ..compare.models import ABCRun, DeclaredContext
from ..config import REPO_ROOT, Settings, get_settings
from ..creative.design import design_experiment
from ..creative.experiment import classify_arm, run_creative_experiment
from ..creative.genome import GENOME_VERSION
from ..creative.investigate import investigate
from ..creative.mutate import experiment_brief, identity_plan, source_manifest
from ..creative.policy import load_policy, save_policy
from ..domain.creative import CreativeExperiment, CreativeGenome, ReferenceCorpus, RetentionSeries
from ..domain.ids import sha256_file
from ..domain.ids import utc_now_iso as utc_now_iso_str
from ..jobs import IdempotencyConflict, JobStore, JobWorker
from ..jobs.recovery import reconcile_interrupted_job
from ..jobs.worker import safe_failure
from ..observability import init_weave, weave_status
from ..observability.weave_ops import flush
from ..providers import build_providers
from ..providers.openai_compat import WANDB_INFERENCE_BASE_URL, OpenAICompatProvider
from ..providers.registry import ProviderBundle
from ..providers.spend import SpendGuard, SpendGuardError, SpendLedger, price_card
from ..review.server import summarize as review_summary
from ..runtime.abc import ABCConfig, abc_dir, load_abc, run_abc
from ..runtime.causal import CausalConfig, CausalRun, causal_dir, load_causal, run_causal, save_causal
from ..runtime.director import EDIT_TYPES, DirectorRun, RunConfig, load_run, run_director, runs_dir
from .security import install_security, validate_exposure

SHA_RE = re.compile(r"^[0-9a-f]{64}$")
HOOK_RE = re.compile(r"^[0-9a-f]{40}_hook3000$")
VIDEO_ID_RE = re.compile(r"^[a-z0-9_\-]{2,80}$")
RUN_ID_RE = re.compile(r"^run_[0-9a-f]+_[0-9a-f]+$")
ABC_ID_RE = re.compile(r"^abc_[0-9a-f]+_[0-9a-f]+$")
CAUSAL_ID_RE = re.compile(r"^causal_[0-9a-f]+_[0-9a-f]+$")
AUDIT_ID_RE = re.compile(r"^audit_[0-9a-f]+_[0-9a-f]+$")
SCREEN_ID_RE = re.compile(r"^screen_[0-9a-f]+_[0-9a-f]+$")
SCREEN_FRAME_RE = re.compile(r"^[0-9]{6,9}_[0-9]{1,5}\.jpg$")
SCREENING_MODEL = "Qwen/Qwen3.8-27B"
SCREENING_OUTPUT_CAP = 2048
SCREENING_EVIDENCE_LABEL = "screen_model_judgment"
REPAIR_ID_RE = re.compile(r"^repair_[0-9a-f]+_[0-9a-f]+$")
UPLOAD_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm"}
FEATURES = {"abc": True, "url_ingest": True, "classify": False}  # switched on as each backend path is implemented and tested
URL_INGEST_UNAVAILABLE = "Link import is unavailable in production until restricted network egress is configured. Upload a local video file."

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


class RunRequest(BaseModel):
    video_id: str = Field(pattern=VIDEO_ID_RE.pattern)
    objective: str = Field(min_length=3, max_length=500)
    constraints: list[str] = Field(default_factory=list, max_length=12)
    focus: str | None = None
    allowed_edits: list[str] | None = None
    iteration_budget: int = Field(default=3, ge=1, le=8)
    owner_confirms_rights: bool = False
    idempotency_key: str | None = Field(default=None, max_length=120)


class IngestRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    idempotency_key: str | None = Field(default=None, max_length=120)


class AuditRequest(BaseModel):
    video_id: str = Field(pattern=VIDEO_ID_RE.pattern)
    idempotency_key: str | None = Field(default=None, max_length=120)


class ScreeningRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    coverage: Literal["quick", "full"] = "quick"

    video_id: str = Field(pattern=VIDEO_ID_RE.pattern)
    idempotency_key: str | None = Field(default=None, max_length=120)


class CausalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_id: str = Field(pattern=VIDEO_ID_RE.pattern)
    objective: str = Field(default="Keep an unfamiliar viewer watching and understanding the video without losing what already works.", min_length=3, max_length=500)
    constraints: list[str] = Field(default_factory=list, max_length=12)
    arms: int = Field(default=2, ge=0, le=3)
    plan_only: bool = False
    audit_id: str | None = Field(default=None, pattern=AUDIT_ID_RE.pattern)
    max_model_calls: int = Field(default=120, ge=10, le=1000)
    deadline_s: int = Field(default=1800, ge=60, le=7200)
    owner_confirms_rights: bool = False
    idempotency_key: str | None = Field(default=None, max_length=120)


EDIT_RIGHTS_DETAIL = ("This video came from a link. DirectorLoop can judge it, but creating an edited version requires confirming that you own it "
                      "or have the right to edit it (owner_confirms_rights: true), or uploading the original file.")


class ABCRequest(BaseModel):
    a_video_id: str = Field(pattern=VIDEO_ID_RE.pattern)
    b_video_id: str = Field(pattern=VIDEO_ID_RE.pattern)
    objective: str = Field(min_length=3, max_length=400)
    creative_type: str = Field(default="educational short", max_length=120)
    audience: str = Field(default="general viewers who do not know the topic", max_length=200)
    expected_payoff: str = Field(default="", max_length=300)
    encounter: str = Field(default="a cold scrolling feed on a phone with sound on", max_length=200)
    constraints: list[str] = Field(default_factory=list, max_length=12)
    iteration_budget: int = Field(default=2, ge=1, le=5)
    max_model_calls: int | None = Field(default=260, ge=20, le=2000)
    deadline_s: int | None = Field(default=1500, ge=60, le=7200)
    owner_confirms_rights: bool = False
    idempotency_key: str | None = Field(default=None, max_length=120)


class Services:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.data = settings.data_dir
        self.creative = self.data / "creative"
        self.policy_path = self.creative / "policy.json"
        self.policy_lock = threading.Lock()
        self.upload_lock = threading.Lock()
        self.providers = build_providers(settings)
        self.screening_provider = None
        self.screening_ledger = None
        self.screening_problem = "Screening is disabled."
        if settings.dl_screening_enabled:
            self._configure_screening()
        self.jobs = JobStore(self.data / "directorloop_jobs.db")
        self.worker = JobWorker(self.jobs, {"experiment": self.run_experiment_job, "run": self.run_director_job, "abc": self.run_abc_job,
                                            "ingest": self.run_ingest_job, "audit": self.run_audit_job, "causal": self.run_causal_job,
                                            "screening": self.run_screening_job},
                                on_recovery=lambda job: reconcile_interrupted_job(self.data, job))
        self._registry: dict[str, dict[str, Any]] | None = None

    def _configure_screening(self) -> None:
        self.screening_provider = None
        self.screening_ledger = None
        if not self.settings.wandb_api_key:
            self.screening_problem = "Screening needs a configured W&B inference key."
            return
        try:
            price_card(WANDB_INFERENCE_BASE_URL, SCREENING_MODEL)
            guard = None
            if self.settings.dl_screening_spend_guard_enabled:
                path = Path(self.settings.dl_screening_spend_ledger_path) if self.settings.dl_screening_spend_ledger_path else self.data / "screening-spend.sqlite3"
                self.screening_ledger = SpendLedger(path, self.settings.dl_screening_spend_budget_id,
                                                    self.settings.dl_screening_spend_limit_usd,
                                                    max_attempts=self.settings.dl_screening_max_physical_attempts)
                guard = SpendGuard(self.screening_ledger, SCREENING_OUTPUT_CAP)
            self.screening_provider = OpenAICompatProvider(
                name="wandb_inference", api_key=self.settings.wandb_api_key, model=SCREENING_MODEL,
                base_url=WANDB_INFERENCE_BASE_URL, project=self.settings.weave_project_path(), vision=True,
                spend_guard=guard,
                max_output_tokens=SCREENING_OUTPUT_CAP, allow_compatibility_fallback=False, enable_thinking=False,
            )
            self.screening_problem = None
        except Exception as exc:
            self.screening_provider = None
            self.screening_problem = safe_failure(exc, "screening")

    def screening_status(self, required_calls: int = 3) -> dict[str, Any]:
        budget = None
        reason = self.screening_problem
        try:
            budget = self.screening_ledger.summary() if self.screening_ledger else None
            if self.screening_provider is not None:
                if self.settings.dl_screening_spend_guard_enabled:
                    card = price_card(WANDB_INFERENCE_BASE_URL, SCREENING_MODEL)
                    # Corrections run after the first pass settles; physical calls still
                    # reserve their full cost individually in the shared ledger.
                    required = card.cost_units(card.max_input_tokens, SCREENING_OUTPUT_CAP) * min(required_calls, 8) / 1_000_000_000
                    if budget is None:
                        reason = "Screening spending ledger is unavailable."
                    elif budget["halted"] or budget["available_usd"] < required or budget["physical_attempts"] + required_calls > budget["max_physical_attempts"]:
                        reason = f"Screening budget has insufficient headroom for {required_calls} requests."
                if reason is None and not weave_status().connected:
                    reason = "Screening needs connected Weave tracing."
        except SpendGuardError as exc:
            reason = safe_failure(exc, "screening")
        except Exception:
            reason = "Screening spending ledger is unavailable."
        return {"enabled": self.settings.dl_screening_enabled, "available": self.screening_provider is not None and reason is None,
                "reason": reason, "budget": budget, "model": SCREENING_MODEL, "evidence_label": SCREENING_EVIDENCE_LABEL,
                "spending_guard_enabled": self.settings.dl_screening_spend_guard_enabled,
                "usage_tracking": "local ledger and W&B" if self.settings.dl_screening_spend_guard_enabled else "W&B billing and Weave; previous local ledger retained as history",
                "no_automatic_edit": True, "review_required": True,
                "funding_status": "provider billing is not polled by this endpoint"}

    def require_screening(self, required_calls: int = 3) -> None:
        status = self.screening_status(required_calls)
        if not status["available"]:
            raise HTTPException(status_code=503, detail=status["reason"] or "Screening is unavailable.")

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
        for up in self.uploads():
            if Path(up["path"]).exists():
                reg[up["video_id"]] = {**up, "role": "link" if up.get("source") == "url" else "upload", "category": "educational_short",
                                       "source": up.get("source", "upload"), "edit_permission": up.get("edit_permission", "owner_upload"), "retention": None}
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

    def uploads(self) -> list[dict[str, Any]]:
        f = self.data / "uploads" / "registry.json"
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else []

    def register_upload(self, entry: dict[str, Any]) -> None:
        d = self.data / "uploads"
        d.mkdir(parents=True, exist_ok=True)
        with self.upload_lock:
            items = [u for u in self.uploads() if u["video_id"] != entry["video_id"]] + [entry]
            with tempfile.NamedTemporaryFile(mode="w", prefix=".registry_", suffix=".json", dir=d, delete=False) as out:
                tmp = Path(out.name)
                json.dump(items, out, indent=2)
            try:
                tmp.replace(d / "registry.json")
            finally:
                tmp.unlink(missing_ok=True)
            self._registry = None

    # ---- jobs -----------------------------------------------------------------
    def create_review_job(self, kind: str, params: dict[str, Any], idempotency_key: str | None):
        existing = self.jobs.idempotent_result(kind, params, idempotency_key)
        if existing is not None:
            return existing, False
        status = self.providers.spend_status()
        if status["enabled"] and not status["available"]:
            raise HTTPException(status_code=503, detail=status["reason"])
        return self.jobs.create(kind, params, idempotency_key=idempotency_key)

    def review_providers(self, job) -> ProviderBundle:  # noqa: ANN001
        status = self.providers.spend_status()
        if status["enabled"] and not status["available"]:
            raise SpendGuardError(status["reason"])
        return self.providers.for_run(job.id)

    def run_screening_job(self, job, on_stage) -> dict[str, Any]:  # noqa: ANN001
        from ..jobs.worker import JobCanceled
        from ..screening.runner import load_screening, run_screening, save_screening

        video = self.video(job.params["video_id"])
        coverage = job.params.get("coverage", "quick")
        from ..screening.runner import screening_windows
        self.require_screening(len(screening_windows(video["duration_ms"], coverage)) * (2 if coverage == "full" else 1))
        screen_id = "screen_" + job.id.removeprefix("job_")
        try:
            report = run_screening(video_id=video["video_id"], path=Path(video["path"]),
                                   provider=self.screening_provider, data_dir=self.data, screening_id=screen_id,
                                   on_stage=on_stage, is_cancelled=lambda: self.jobs.cancel_requested(job.id),
                                   **({"coverage": coverage, "repair_incomplete": True} if coverage == "full" else {}))
            if report.status == "failed":
                raise RuntimeError(report.error or "screening failed")
            if report.status == "canceled":
                raise JobCanceled("cancellation requested by the operator")
        except BaseException as exc:
            try:
                report = load_screening(self.data, screen_id)
            except (OSError, ValueError):
                report = None
            if report is not None and report.status == "running":
                report.status = "canceled" if isinstance(exc, JobCanceled) else "failed"
                report.ended_at = utc_now_iso_str()
                report.error = safe_failure(exc, job.kind)
                save_screening(self.data, report)
            raise
        finally:
            flush()
        return {"screen_id": report.id, "status": report.status, "weave_url": report.weave_url,
                "evidence_label": SCREENING_EVIDENCE_LABEL, "review_required": True, "no_automatic_edit": True}

    def run_causal_job(self, job, on_stage) -> dict[str, Any]:  # noqa: ANN001
        from ..jobs.worker import JobCanceled

        params = job.params
        v = self.video(params["video_id"])
        causal_id = "causal_" + job.id.removeprefix("job_")
        config = CausalConfig(**{k: v for k, v in params.items() if k != "owner_confirms_rights"},
                              video_path=v["path"], category=v.get("category", "educational_short"))
        try:
            with self.policy_lock:
                run = run_causal(config, self.review_providers(job), self.data, on_stage=on_stage, run_id=causal_id, launched_via="api")
            # run_causal records expected runtime failures and returns them. Do not report a failed run as a completed job.
            if run.status == "failed":
                raise RuntimeError(run.error or run.stop_reason or "causal run failed")
            if run.status == "cancelled":
                raise JobCanceled(run.stop_reason or "causal run cancelled")
        except BaseException as exc:
            stored = load_causal(self.data, causal_id)
            if stored is not None and stored.status == "running":
                # Covers cancellation during STARTED, before the runtime enters its own exception handler.
                stored.status = "cancelled" if isinstance(exc, JobCanceled) else "failed"
                stored.ended_at = utc_now_iso_str()
                stored.error = safe_failure(exc, job.kind)
                stored.stop_reason = "the run was cancelled by the operator" if isinstance(exc, JobCanceled) else "the run stopped with an error"
                save_causal(stored, self.data)
            raise
        finally:
            flush()
        return {"causal_id": run.id, "status": run.status, "stop_reason": run.stop_reason, "weave_url": run.weave_url}

    def run_director_job(self, job, on_stage) -> dict[str, Any]:  # noqa: ANN001
        params = job.params
        v = self.video(params["video_id"])
        run_id = "run_" + job.id.removeprefix("job_")
        config = RunConfig(video_id=v["video_id"], video_path=v["path"], objective=params["objective"], constraints=params.get("constraints") or [],
                           focus=params.get("focus"), allowed_edits=params.get("allowed_edits"), iteration_budget=int(params.get("iteration_budget", 3)),
                           category=v.get("category", "educational_short"))
        try:
            with self.policy_lock:
                run = run_director(config, self.review_providers(job), self.data, on_stage=on_stage, run_id=run_id, launched_via="api")
        except BaseException as exc:
            stored = load_run(self.data, run_id)
            if stored is not None and stored.status == "running":
                stored.status, stored.error = "failed", safe_failure(exc, job.kind)
                stored.stop_reason = "the run was canceled" if type(exc).__name__ == "JobCanceled" else "the run stopped with an error"
                from ..runtime.director import save_run

                save_run(stored, self.data)
            raise
        finally:
            flush()
        return {"run_id": run.id, "final_version_id": run.final_version_id, "stop_reason": run.stop_reason, "weave_url": run.weave_url, "status": run.status}

    def run_ingest_job(self, job, on_stage) -> dict[str, Any]:  # noqa: ANN001
        if self.settings.dl_mode == "production":
            raise ValueError(URL_INGEST_UNAVAILABLE)
        import shutil

        from ..ingest.url import IngestError, acquire, classify_url
        from ..media.probe import inspect_media

        url = job.params["url"]
        info = classify_url(url)
        on_stage("RESOLVING", f"{info['kind']} link on {info['host']}" + (f" ({info['platform']})" if info["platform"] else ""), {"kind": info["kind"], "platform": info["platform"]})
        on_stage("DOWNLOADING", "retrieving the accessible media", None)
        incoming = self.data / "uploads" / "incoming"
        try:
            got = acquire(url, incoming, allow_private=self.settings.dl_ingest_allow_private_hosts)
        except IngestError as exc:
            raise ValueError(str(exc)) from exc
        on_stage("PROBING", f"received {got.bytes / 1e6:.1f} MB in {got.elapsed_ms / 1000:.1f}s; checking the streams", None)
        try:
            media = inspect_media(got.path, untrusted=True)
        except Exception as exc:  # noqa: BLE001
            got.path.unlink(missing_ok=True)
            raise ValueError("the downloaded file is not a supported readable video") from exc
        if not media.width or not media.height or not media.duration_ms:
            got.path.unlink(missing_ok=True)
            raise ValueError("the downloaded file has no video stream")
        final = self.data / "uploads" / f"{got.sha256}{got.path.suffix.lower() or '.mp4'}"
        if final.exists():
            got.path.unlink(missing_ok=True)
        else:
            final.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(got.path), final)
        entry = {"video_id": f"url-{got.sha256[:12]}", "path": str(final), "title": got.title, "sha256": got.sha256, "duration_ms": media.duration_ms,
                 "width": media.width, "height": media.height, "has_audio": bool(media.has_audio), "source": "url", "source_url": got.source_url,
                 "platform": got.platform, "uploader": got.uploader, "edit_permission": "requires_owner_confirmation", "uploaded_at": utc_now_iso_str()}
        self.register_upload(entry)
        on_stage("REGISTERED", f"ready to judge: {entry['video_id']}", {"video_id": entry["video_id"]})
        return {"video_id": entry["video_id"], "title": got.title, "platform": got.platform, "edit_permission": entry["edit_permission"],
                "media_url": f"/media/source/{entry['video_id']}.mp4"}

    def run_audit_job(self, job, on_stage) -> dict[str, Any]:  # noqa: ANN001
        v = self.video(job.params["video_id"])
        try:
            rep = run_audit(video_id=v["video_id"], video_path=Path(v["path"]), version_id="v0", provider=self.review_providers(job).probe, data_dir=self.data, on_stage=on_stage)
        finally:
            flush()
        return {"audit_id": rep.id, "status": rep.status, "findings": len(rep.findings), "strengths": len(rep.strengths), "weave_url": rep.weave_url}

    def require_edit_rights(self, video: dict[str, Any], confirmed: bool) -> None:
        if video.get("edit_permission") == "requires_owner_confirmation" and not confirmed:
            raise HTTPException(status_code=403, detail=EDIT_RIGHTS_DETAIL)

    def run_abc_job(self, job, on_stage) -> dict[str, Any]:  # noqa: ANN001
        params = job.params
        a, b = self.video(params["a_video_id"]), self.video(params["b_video_id"])
        abc_id = "abc_" + job.id.removeprefix("job_")
        ctx = DeclaredContext(objective=params["objective"], creative_type=params.get("creative_type") or "educational short",
                              audience=params.get("audience") or "general viewers who do not know the topic", expected_payoff=params.get("expected_payoff") or "",
                              encounter=params.get("encounter") or "a cold scrolling feed on a phone with sound on")
        config = ABCConfig(a_video_id=a["video_id"], a_path=a["path"], b_video_id=b["video_id"], b_path=b["path"], context=ctx, constraints=params.get("constraints") or [],
                           iteration_budget=int(params.get("iteration_budget", 2)), max_model_calls=params.get("max_model_calls"), deadline_s=params.get("deadline_s"))
        try:
            with self.policy_lock:
                run = run_abc(config, self.review_providers(job), self.data, on_stage=on_stage, abc_id=abc_id, launched_via="api")
        except BaseException as exc:
            stored = load_abc(self.data, abc_id)
            if stored is not None and stored.status == "running":
                from ..runtime.abc import save_abc

                stored.status, stored.error = "failed", safe_failure(exc, job.kind)
                stored.stop_reason = "the run was canceled" if type(exc).__name__ == "JobCanceled" else "the run stopped with an error"
                save_abc(stored, self.data)
            raise
        finally:
            flush()
        return {"abc_id": run.id, "final_version": run.final_version, "stop_reason": run.stop_reason, "weave_url": run.weave_url, "status": run.status}

    def run_experiment_job(self, job, on_stage) -> dict[str, Any]:  # noqa: ANN001
        params = job.params
        v = self.video(params["video_id"])
        with self.policy_lock:
            policy = load_policy(self.policy_path)
            exp = run_creative_experiment(
                video_id=v["video_id"], video_path=Path(v["path"]), providers=self.review_providers(job), data_dir=self.data, policy=policy, corpus=self.corpus(),
                retention=RetentionSeries.model_validate(v["retention"]) if v.get("retention") else None, category=v.get("category", "educational_short"),
                policy_mode=params.get("policy_mode", "learned"), max_arms=int(params.get("max_arms", 3)), record_policy=bool(params.get("record_policy", True)),
                on_stage=on_stage,
            )
            if params.get("record_policy", True):
                save_policy(policy, self.policy_path)
        flush()
        return {"experiment_id": exp.id, "outcome": exp.decision.outcome if exp.decision else None, "weave_url": exp.weave_url}


def _media_url(path: str | None, data_dir: Path) -> str | None:
    if not path:
        return None
    p = Path(path)
    # Uploads also have SHA filenames; a filename alone does not put a file in the render store.
    if SHA_RE.fullmatch(p.stem) and p.suffix.lower() == ".mp4" and p.resolve().parent == (data_dir / "renders").resolve():
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
    validate_exposure(settings)
    services = Services(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        init_weave(settings)
        services.worker.maintain_once()
        if start_worker:
            services.worker.start()
        yield
        services.worker.stop()

    app = FastAPI(title="DirectorLoop", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.services = services
    session_auth = install_security(app, settings)

    def known_media_url(path: str | None, artifact_hash: str | None = None) -> str | None:
        """Expose only the actual render store or the exact file registered as a source video."""
        from ..domain.ids import sha256_file

        if not path:
            return None
        p = Path(path)
        rendered = _media_url(path, services.data)
        if rendered:
            return rendered if not artifact_hash or p.stem == artifact_hash else None
        if not p.is_file():
            return None
        resolved = p.resolve()
        for video in services.registry().values():
            if Path(video["path"]).resolve() != resolved:
                continue
            expected = artifact_hash or video.get("sha256")
            if expected and sha256_file(p) != expected:
                return None
            return f"/media/source/{video['video_id']}.mp4"
        return None

    def auth(request: Request) -> None:
        session_auth.require(request)

    # ---- health ----------------------------------------------------------------
    @app.get("/api/health", dependencies=[Depends(auth)])
    def health() -> dict[str, Any]:
        w = weave_status()
        corpus = services.corpus()
        public = services.data / "reviews" / "public_url.txt"
        return {
            "status": "ok",
            "features": {"runs": True, "causal": True, "abc": FEATURES["abc"], "screening": settings.dl_screening_enabled,
                         "url_ingest": FEATURES["url_ingest"] and settings.dl_mode != "production", "classify": FEATURES["classify"]},
            "screening": services.screening_status(),
            "full_screening": services.screening_status(16),
            "full_review_spending": services.providers.spend_status(),
            "capability_notes": {"url_ingest": URL_INGEST_UNAVAILABLE} if settings.dl_mode == "production" else {},
            "limits": {"upload_max_bytes": settings.dl_max_upload_mb * 1024 * 1024, "upload_max_mb": settings.dl_max_upload_mb},
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
            out.append({"video_id": v["video_id"], "title": v["title"], "duration_ms": v.get("duration_ms"), "category": v["category"], "source": v["source"],
                        "edit_permission": v.get("edit_permission", "owned"), "platform": v.get("platform"),
                        "media_url": f"/media/source/{v['video_id']}.mp4", "role": v["role"], "has_genome": genome_cached,
                        "retention_class": ({"historical_owned": "historical", "real_platform": "real", "simulated_demo": "simulated"}.get(v["retention"]["source_type"]) if v.get("retention") else None),
                        "latest_experiment_id": latest})
        order = {"demo_a": 0, "demo_b": 1, "dev": 2, "holdout": 3, "reference": 4}
        return sorted(out, key=lambda x: (order.get(x["role"], 9), x["title"]))

    def _cached_genome(v: dict[str, Any]) -> CreativeGenome | None:
        """Browsing source metadata never dispatches paid model work."""
        artifact_hash = sha256_file(Path(v["path"]))
        model = services.providers.probe.capability.model if services.providers.probe else settings.dl_probe_model
        cached = services.creative / "genomes" / f"genome_{artifact_hash[:24]}_{GENOME_VERSION}_{model.replace('/', '_')}.json"
        if not cached.is_file():
            return None
        try:
            genome = CreativeGenome.model_validate_json(cached.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return genome if genome.artifact_hash == artifact_hash else None

    @app.get("/api/videos/{video_id}", dependencies=[Depends(auth)])
    def video_detail(video_id: str) -> dict[str, Any]:
        v = services.video(video_id)
        g = _cached_genome(v)
        retention = RetentionSeries.model_validate(v["retention"]) if v.get("retention") else None
        if g is None:
            return {"video_id": v["video_id"], "title": v["title"], "duration_ms": v.get("duration_ms"),
                    "category": v["category"], "source": v["source"], "role": v["role"],
                    "media_url": f"/media/source/{v['video_id']}.mp4", "has_genome": False,
                    "edit_permission": v.get("edit_permission", "owned"), "platform": v.get("platform"),
                    "retention_class": None if retention is None else retention.evidence_class.value,
                    "latest_experiment_id": next((e.id for e in services.experiments() if e.video_id == video_id and e.status == "completed"), None),
                    "genome": None, "investigation": None, "retention": v.get("retention")}
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
        g = _cached_genome(v)
        if g is None:
            raise HTTPException(status_code=409, detail="No saved experiment analysis. Choose Test explanations to prepare one.")
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
            job, created = services.create_review_job("experiment", params, body.idempotency_key)
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
                "id": a.id, "label": a.label, "status": a.status, "media_url": known_media_url(a.artifact_path, a.artifact_hash), "hook_media_url": _hook_url(a.artifact_path, services.data),
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

    # ---- uploads and director runs ------------------------------------------------
    @app.post("/api/uploads", dependencies=[Depends(auth)])
    async def upload_video(file: UploadFile = File(...)) -> dict[str, Any]:  # noqa: B008
        import hashlib

        from ..media.probe import inspect_media

        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in UPLOAD_SUFFIXES:
            raise HTTPException(status_code=415, detail=f"unsupported file type; use one of {sorted(UPLOAD_SUFFIXES)}")
        d = services.data / "uploads"
        d.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".incoming_", suffix=suffix, dir=d, delete=False) as incoming:
            tmp = Path(incoming.name)
        h, size = hashlib.sha256(), 0
        try:
            with tmp.open("wb") as out:
                while chunk := await file.read(1 << 20):
                    size += len(chunk)
                    if size > settings.dl_max_upload_mb * 1024 * 1024:
                        raise HTTPException(status_code=413, detail=f"file is larger than {settings.dl_max_upload_mb} MB")
                    h.update(chunk)
                    out.write(chunk)
            sha = h.hexdigest()
            try:
                info = await asyncio.to_thread(inspect_media, tmp, untrusted=True)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=422, detail="not a supported readable video") from exc
            if not info.width or not info.height or not info.duration_ms:
                raise HTTPException(status_code=422, detail="the file has no video stream")
            final = d / f"{sha}{suffix}"
            # Same hash means identical content; publication is atomic on this filesystem.
            if not final.exists():
                tmp.replace(final)
        finally:
            tmp.unlink(missing_ok=True)
            await file.close()
        title = re.sub(r"[^A-Za-z0-9 ._\-]", "", Path(file.filename or "upload").stem)[:80] or "upload"
        entry = {"video_id": f"upl-{sha[:12]}", "path": str(final), "title": title, "sha256": sha, "duration_ms": info.duration_ms,
                 "width": info.width, "height": info.height, "has_audio": bool(info.has_audio), "source": "upload", "edit_permission": "owner_upload",
                 "uploaded_at": utc_now_iso_str()}
        services.register_upload(entry)
        return {k: entry[k] for k in ("video_id", "title", "sha256", "duration_ms", "width", "height", "has_audio")} | {"media_url": f"/media/source/{entry['video_id']}.mp4"}

    @app.post("/api/ingest/url", dependencies=[Depends(auth)])
    def ingest_url(body: IngestRequest) -> dict[str, Any]:
        if settings.dl_mode == "production":
            raise HTTPException(status_code=503, detail={"code": "url_ingest_unavailable", "message": URL_INGEST_UNAVAILABLE})
        from urllib.parse import urlparse

        from ..ingest.url import IngestError, check_host, classify_url

        try:
            info = classify_url(body.url)
            if info["kind"] == "unknown":
                raise IngestError("the link is neither a supported platform page nor a direct video file (.mp4, .mov, .m4v, .webm)")
            u = urlparse(body.url.strip())
            check_host(info["host"], u.port, allow_private=settings.dl_ingest_allow_private_hosts)
        except IngestError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            job, created = services.jobs.create("ingest", {"url": body.url.strip()}, idempotency_key=body.idempotency_key)
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"job_id": job.id, "created": created, "kind": info["kind"], "platform": info["platform"],
                "edit_permission": "requires_owner_confirmation"}

    # ---- sponsor screening: separate from final audits and edit experiments ----
    @app.post("/api/screenings", dependencies=[Depends(auth)])
    def start_screening(body: ScreeningRequest) -> dict[str, Any]:
        params = {"video_id": body.video_id}
        if body.coverage == "full":
            params["coverage"] = "full"
        try:
            existing = services.jobs.idempotent_result("screening", params, body.idempotency_key)
            if existing is not None:
                return {"job_id": existing.id, "screen_id": "screen_" + existing.id.removeprefix("job_"), "created": False}
            video = services.video(body.video_id)
            from ..screening.runner import screening_windows
            services.require_screening(len(screening_windows(video["duration_ms"], body.coverage)) * (2 if body.coverage == "full" else 1))
            job, created = services.jobs.create("screening", params, idempotency_key=body.idempotency_key)
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"job_id": job.id, "screen_id": "screen_" + job.id.removeprefix("job_"), "created": created}

    def _screening_summary(report) -> dict[str, Any]:  # noqa: ANN001
        return {"id": report.id, "screen_id": report.id, "video_id": report.video_id, "status": report.status,
                "created_at": report.created_at, "ended_at": report.ended_at, "duration_ms": report.duration_ms,
                "model_calls": report.model_calls, "weave_url": report.weave_url,
                "evidence_label": SCREENING_EVIDENCE_LABEL, "review_required": True, "no_automatic_edit": True}

    @app.get("/api/screenings", dependencies=[Depends(auth)])
    def screenings(video_id: str | None = None) -> list[dict[str, Any]]:
        from ..screening.runner import list_screenings

        return [_screening_summary(report) for report in list_screenings(services.data)
                if SCREEN_ID_RE.fullmatch(report.id) and (video_id is None or report.video_id == video_id)]

    def _get_screening(screen_id: str):  # noqa: ANN202
        from ..screening.runner import load_screening

        if not SCREEN_ID_RE.fullmatch(screen_id):
            raise HTTPException(status_code=404, detail="unknown screening")
        try:
            report = load_screening(services.data, screen_id)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="unknown screening") from exc
        if report.id != screen_id:
            raise HTTPException(status_code=404, detail="unknown screening")
        return report

    def _screening_source(report) -> Path:  # noqa: ANN001
        video = services.video(report.video_id)
        source = Path(video["path"])
        try:
            matches = report.artifact_hash and source.resolve() == Path(report.artifact_path).resolve() and sha256_file(source) == report.artifact_hash
        except OSError as exc:
            raise HTTPException(status_code=404, detail="The screening source file is unavailable.") from exc
        if not matches:
            raise HTTPException(status_code=409, detail="The source file no longer matches this screening.")
        return source

    @app.get("/api/screenings/{screen_id}", dependencies=[Depends(auth)])
    def screening_detail(screen_id: str) -> dict[str, Any]:
        from ..screening.boundary import BOUNDARY_VALIDATION_PROTOCOL, boundary_validation_issues
        from ..screening.scoring import build_scorecard
        from ..screening.attention_admission import assess_attention

        report = _get_screening(screen_id)
        score_view = report.model_copy(deep=True)
        body = report.model_dump(mode="json", exclude={"artifact_path"})
        body["recorded_status"] = body["status"]
        body["view_validation"] = BOUNDARY_VALIDATION_PROTOCOL
        body.update(screen_id=report.id, evidence_label=SCREENING_EVIDENCE_LABEL,
                    evidence_title=report.evidence_label, no_automatic_edit=True,
                    media_url=f"/media/screening/{report.id}/original.mp4" if report.artifact_hash else None)
        for index, window in enumerate(body["windows"]):
            # Additional read-time flags preserve the exact historical model record.
            issues = boundary_validation_issues(window.get("judgment") or {},
                is_last_prefix=window["end_ms"] >= report.duration_ms,
                coverage=report.protocol.get("coverage", "quick"))
            if issues:
                window["recorded_status"] = window["status"]
                window["validation_issues"] = list(dict.fromkeys([*window["validation_issues"], *issues]))
                if window["status"] == "complete":
                    window["status"] = "needs_review"
                if body["status"] == "complete":
                    body["status"] = "needs_review"
                window["display_reason"] = "The video continues past this checkpoint. This finding needs review."
            score_view.windows[index].validation_issues = window["validation_issues"]
            score_view.windows[index].status = window["status"]
            window["attention_assessment"] = assess_attention(score_view.windows[index])
            for frame in window["evidence_frames"]:
                name = Path(frame.pop("path")).name
                frame["media_url"] = f"/media/screening/{report.id}/{name}" if SCREEN_FRAME_RE.fullmatch(name) else None
        score_view.status = body["status"]
        body["scorecard"] = build_scorecard(score_view)
        return body

    @app.get("/media/screening/{screen_id}/original.mp4")
    def screening_original(screen_id: str) -> FileResponse:
        return FileResponse(_screening_source(_get_screening(screen_id)), media_type="video/mp4")

    @app.get("/media/screening/{screen_id}/{filename}")
    def screening_frame(screen_id: str, filename: str) -> FileResponse:
        report = _get_screening(screen_id)
        if not SCREEN_FRAME_RE.fullmatch(filename):
            raise HTTPException(status_code=404)
        root = (services.data / "screenings" / report.id / "frames").resolve()
        if not root.is_relative_to((services.data / "screenings").resolve()):
            raise HTTPException(status_code=404)
        candidates = [frame for window in report.windows for frame in window.evidence_frames if Path(frame.path).name == filename]
        for frame in candidates:
            path = Path(frame.path)
            try:
                if path.resolve().parent == root and path.is_file() and sha256_file(path) == frame.sha256:
                    return FileResponse(path, media_type="image/jpeg")
            except OSError:
                continue
        raise HTTPException(status_code=404, detail="No matching saved evidence frame.")

    @app.post("/api/audits", dependencies=[Depends(auth)])
    def start_audit(body: AuditRequest) -> dict[str, Any]:
        services.video(body.video_id)
        try:
            job, created = services.create_review_job("audit", {"video_id": body.video_id}, body.idempotency_key)
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"job_id": job.id, "created": created}

    @app.get("/api/audits", dependencies=[Depends(auth)])
    def list_audits(video_id: str | None = None) -> list[dict[str, Any]]:
        d = services.data / "audit"
        out = []
        for f in sorted(d.glob("audit_*/audit.json"), reverse=True) if d.exists() else []:
            try:
                a = AuditReport.model_validate_json(f.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if video_id and a.video_id != video_id:
                continue
            out.append({"id": a.id, "video_id": a.video_id, "version_id": a.version_id, "status": a.status, "created_at": a.created_at, "findings": len(a.findings),
                        "strengths": len(a.strengths), "duration_ms": a.duration_ms, "weave_url": a.weave_url})
        return out

    @app.post("/api/runs", dependencies=[Depends(auth)])
    def start_run(body: RunRequest) -> dict[str, Any]:
        services.require_edit_rights(services.video(body.video_id), body.owner_confirms_rights)
        if body.focus is not None and body.focus not in OBJECTIVES:
            raise HTTPException(status_code=422, detail=f"focus must be one of {OBJECTIVES}")
        if body.allowed_edits is not None and any(e not in EDIT_TYPES for e in body.allowed_edits):
            raise HTTPException(status_code=422, detail=f"allowed_edits must be a subset of {list(EDIT_TYPES)}")
        params = body.model_dump(exclude={"idempotency_key"})
        try:
            job, created = services.create_review_job("run", params, body.idempotency_key)
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"job_id": job.id, "run_id": "run_" + job.id.removeprefix("job_"), "created": created}

    def _run_summary(r: DirectorRun) -> dict[str, Any]:
        return {"id": r.id, "video_id": r.config.video_id, "status": r.status, "created_at": r.created_at, "ended_at": r.ended_at, "objective": r.config.objective,
                "iteration_budget": r.config.iteration_budget, "iterations": len([i for i in r.iterations if i.finding_id]),
                "decisions": [i.decision for i in r.iterations], "final_version_id": r.final_version_id, "final_decision": r.final_decision,
                "stop_reason": r.stop_reason, "weave_url": r.weave_url, "launched_via": r.launched_via, "total_ms": r.timings_ms.get("total_ms")}

    @app.get("/api/runs", dependencies=[Depends(auth)])
    def list_runs() -> list[dict[str, Any]]:
        d = runs_dir(services.data)
        out = []
        for f in sorted(d.glob("run_*.json"), reverse=True) if d.exists() else []:
            try:
                out.append(_run_summary(DirectorRun.model_validate_json(f.read_text(encoding="utf-8"))))
            except ValueError:
                continue
        return out

    @app.get("/api/runs/{run_id}", dependencies=[Depends(auth)])
    def run_detail(run_id: str) -> dict[str, Any]:
        if not RUN_ID_RE.match(run_id):
            raise HTTPException(status_code=404)
        r = load_run(services.data, run_id)
        if r is None:
            raise HTTPException(status_code=404)
        body = r.model_dump(mode="json")
        body["original_media_url"] = known_media_url(r.original_path, r.original_artifact_hash)
        body["final_media_url"] = known_media_url(r.final_path, r.final_artifact_hash)
        if not r.final_path and r.final_version_id == "v0":
            body["final_media_url"] = body["original_media_url"]
        for it in body["iterations"]:
            it["candidate_media_url"] = known_media_url(it.get("candidate_path"), it.get("candidate_artifact_hash"))
        return body

    def _audit_view(a: AuditReport) -> dict[str, Any]:
        body = a.model_dump(mode="json")

        def frame_url(path: str) -> str | None:
            p = Path(path)
            return f"/media/audit/{a.id}/{p.stem}.jpg" if p.parent.name == "frames" and p.stem.isdigit() else None

        for group in ("findings", "strengths"):
            for item in body[group]:
                for ef in item.get("evidence_frames", []):
                    ef["url"] = frame_url(ef.pop("path"))
        body["media_url"] = known_media_url(a.artifact_path, a.artifact_hash)
        body.pop("artifact_path", None)
        return body

    @app.get("/api/audits/{audit_id}", dependencies=[Depends(auth)])
    def audit_detail(audit_id: str) -> dict[str, Any]:
        if not AUDIT_ID_RE.match(audit_id):
            raise HTTPException(status_code=404)
        f = services.data / "audit" / audit_id / "audit.json"
        if not f.exists():
            raise HTTPException(status_code=404)
        return _audit_view(AuditReport.model_validate_json(f.read_text(encoding="utf-8")))

    @app.get("/api/repairs/{repair_id}", dependencies=[Depends(auth)])
    def repair_detail(repair_id: str) -> dict[str, Any]:
        if not REPAIR_ID_RE.match(repair_id):
            raise HTTPException(status_code=404)
        f = services.data / "repairs" / f"{repair_id}.json"
        if not f.exists():
            raise HTTPException(status_code=404)
        rr = RepairRun.model_validate_json(f.read_text(encoding="utf-8"))
        body = rr.model_dump(mode="json")
        body["candidate_media_url"] = known_media_url(rr.candidate_path, rr.candidate_artifact_hash)
        body.pop("original_path", None)
        body.pop("candidate_path", None)
        return body

    @app.get("/media/audit/{audit_id}/{t_ms}.jpg")
    def media_audit_frame(audit_id: str, t_ms: str) -> FileResponse:
        if not AUDIT_ID_RE.match(audit_id) or not t_ms.isdigit() or len(t_ms) > 7:
            raise HTTPException(status_code=404)
        f = services.data / "audit" / audit_id / "frames" / f"{t_ms}.jpg"
        if not f.exists():
            raise HTTPException(status_code=404)
        return FileResponse(f, media_type="image/jpeg")

    # ---- causal experiments --------------------------------------------------------
    @app.post("/api/causal", dependencies=[Depends(auth)])
    def start_causal(body: CausalRequest) -> dict[str, Any]:
        video = services.video(body.video_id)
        if not body.plan_only and body.arms:
            services.require_edit_rights(video, body.owner_confirms_rights)
        if body.audit_id and not (services.data / "audit" / body.audit_id / "audit.json").is_file():
            raise HTTPException(status_code=404, detail="unknown audit")
        params = body.model_dump(exclude={"idempotency_key"})
        try:
            job, created = services.create_review_job("causal", params, body.idempotency_key)
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"job_id": job.id, "causal_id": "causal_" + job.id.removeprefix("job_"), "created": created}

    def _causal_summary(r: CausalRun) -> dict[str, Any]:
        return {"id": r.id, "video_id": r.config.video_id, "status": r.status, "created_at": r.created_at, "ended_at": r.ended_at,
                "objective": r.config.objective, "plan_only": r.config.plan_only, "arms": len(r.arms), "audit_id": r.audit_id,
                "audit_source": r.audit_source, "decision": r.plan.decision if r.plan else None, "verdicts": [a.verdict for a in r.arms],
                "stop_reason": r.stop_reason, "weave_url": r.weave_url, "launched_via": r.launched_via, "total_ms": r.timings_ms.get("total_ms")}

    @app.get("/api/causal", dependencies=[Depends(auth)])
    def list_causal(video_id: str | None = None) -> list[dict[str, Any]]:
        out = []
        for f in sorted(causal_dir(services.data).glob("causal_*.json"), reverse=True):
            try:
                run = CausalRun.model_validate_json(f.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if video_id is None or run.config.video_id == video_id:
                out.append(_causal_summary(run))
        return out

    def _get_causal(causal_id: str) -> CausalRun:
        if not CAUSAL_ID_RE.fullmatch(causal_id):
            raise HTTPException(status_code=404)
        run = load_causal(services.data, causal_id)
        if run is None:
            raise HTTPException(status_code=404)
        return run

    @app.get("/api/causal/{causal_id}", dependencies=[Depends(auth)])
    def causal_detail(causal_id: str) -> dict[str, Any]:
        run = _get_causal(causal_id)
        body = run.model_dump(mode="json")
        body["config"].pop("video_path", None)
        body["original_media_url"] = f"/media/causal/{run.id}/original.mp4"
        for arm in body["arms"]:
            arm["render_media_url"] = known_media_url(arm.pop("render_path", None), arm.get("render_hash"))
        body["runtime"].pop("state", None)
        return body

    @app.get("/media/causal/{causal_id}/original.mp4")
    def causal_original(causal_id: str) -> FileResponse:
        from ..domain.ids import sha256_file

        run = _get_causal(causal_id)
        source = Path(run.config.video_path)
        if not source.is_file() or source.suffix.lower() not in UPLOAD_SUFFIXES:
            raise HTTPException(status_code=404, detail="source video unavailable")
        if not SHA_RE.fullmatch(run.artifact_hash) or sha256_file(source) != run.artifact_hash:
            raise HTTPException(status_code=409, detail="source video no longer matches the analyzed artifact")
        return FileResponse(source, media_type="video/mp4")

    # ---- A/B-to-C -------------------------------------------------------------------
    @app.post("/api/abc", dependencies=[Depends(auth)])
    def start_abc(body: ABCRequest) -> dict[str, Any]:
        if body.a_video_id == body.b_video_id:
            raise HTTPException(status_code=422, detail="A and B must be two different edits")
        services.require_edit_rights(services.video(body.a_video_id), body.owner_confirms_rights)
        services.require_edit_rights(services.video(body.b_video_id), body.owner_confirms_rights)
        params = body.model_dump(exclude={"idempotency_key"})
        try:
            job, created = services.create_review_job("abc", params, body.idempotency_key)
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"job_id": job.id, "abc_id": "abc_" + job.id.removeprefix("job_"), "created": created}

    def _abc_summary(r: ABCRun) -> dict[str, Any]:
        comp = r.comparison
        return {"id": r.id, "status": r.status, "created_at": r.created_at, "ended_at": r.ended_at, "objective": r.context.objective,
                "a_video_id": r.versions["A"].video_id if "A" in r.versions else None, "b_video_id": r.versions["B"].video_id if "B" in r.versions else None,
                "ab_overall": comp.whole.overall.verdict if comp and comp.whole else None, "attempts": len([a for a in r.attempts if a.proposal]),
                "decisions": [a.decision for a in r.attempts], "final_version": r.final_version, "final_decision": r.final_decision, "stop_reason": r.stop_reason,
                "weave_url": r.weave_url, "launched_via": r.launched_via, "total_ms": r.timings_ms.get("total_ms"), "rubric_version": r.rubric.get("version")}

    @app.get("/api/abc", dependencies=[Depends(auth)])
    def list_abc() -> list[dict[str, Any]]:
        d = abc_dir(services.data)
        out = []
        for f in sorted(d.glob("abc_*.json"), reverse=True) if d.exists() else []:
            try:
                out.append(_abc_summary(ABCRun.model_validate_json(f.read_text(encoding="utf-8"))))
            except ValueError:
                continue
        return out

    @app.get("/api/abc/{abc_id}", dependencies=[Depends(auth)])
    def abc_detail(abc_id: str) -> dict[str, Any]:
        if not ABC_ID_RE.match(abc_id):
            raise HTTPException(status_code=404)
        r = load_abc(services.data, abc_id)
        if r is None:
            raise HTTPException(status_code=404)
        body = r.model_dump(mode="json")
        for v in body["versions"].values():
            v["media_url"] = known_media_url(v.get("evaluated_path"), v.get("evaluated_hash"))
            v.pop("evaluated_path", None)
            v.pop("original_path", None)
        for att in body["attempts"]:
            att["render_media_url"] = known_media_url(att.get("render_path"), att.get("render_hash"))
            att.pop("render_path", None)
        body["runtime"] = {k: v for k, v in body.get("runtime", {}).items() if k != "state"}
        return body

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
        return JSONResponse({"detail": "the request could not be processed with the supplied data"}, status_code=422)

    return app


def main() -> None:  # pragma: no cover
    import uvicorn

    s = get_settings()
    uvicorn.run(create_app(s), host=s.dl_bind_host, port=s.dl_port, log_level="info")


if __name__ == "__main__":  # pragma: no cover
    main()
