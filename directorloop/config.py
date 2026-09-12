"""Application configuration.

Secrets are read from the environment (or a local .env) and are never printed.
`presence_report()` exposes only whether a value is configured and its length.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.environ.get("DL_ENV_FILE", str(REPO_ROOT / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Application
    dl_mode: Literal["demo", "production"] = "demo"
    dl_bind_host: str = "127.0.0.1"
    dl_port: int = 8787
    dl_database_url: str = "sqlite:///./data/directorloop.db"
    dl_data_dir: str = "./data"
    dl_local_auth_token: str = ""
    dl_max_upload_mb: int = 200
    dl_max_iterations: int = 1
    dl_job_deadline_seconds: int = 45
    dl_approved_spend_usd: float = 2.0
    dl_gpu_concurrency: int = 1
    dl_external_media_consent: Literal["demo_assets_only", "all_project_media"] = "demo_assets_only"

    # Weights & Biases
    wandb_api_key: str = ""
    wandb_entity: str = "leondragon3798-curio"
    wandb_project: str = "directorloop"
    dl_weave_enabled: bool = True

    # Model selection
    dl_planner_provider: Literal["gemini", "openai", "wandb_inference", "typesafe", "rules"] = "openai"
    dl_planner_model: str = "gpt-5.6-sol"
    dl_probe_provider: Literal["gemini", "openai", "wandb_inference", "typesafe"] = "openai"
    dl_probe_model: str = "gpt-5.6-terra"
    dl_probe_trials: int = 3
    dl_probe_frames: int = 8
    dl_probe_prompt_version: str = "v1"
    dl_wandb_inference_probe_model: str = "Qwen/Qwen3.8-27B"
    dl_wandb_inference_planner_model: str = "moonshotai/Kimi-K2.6"

    # Provider secrets
    gemini_api_key: str = ""
    openai_api_key: str = ""
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""
    typesafe_api_key: str = ""
    typesafe_base_url: str = ""
    typesafe_model: str = ""

    # Generation
    dl_generation_provider: Literal["none", "ltx_local", "fal"] = "none"
    dl_ltx_checkpoint: str = "Lightricks/LTX-Video-0.9.8-distilled"
    dl_live_generation_enabled: bool = False

    # Derived helpers -------------------------------------------------
    @property
    def data_dir(self) -> Path:
        p = Path(self.dl_data_dir)
        if not p.is_absolute():
            p = REPO_ROOT / p
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def database_url(self) -> str:
        url = self.dl_database_url
        if url.startswith("sqlite:///./"):
            rel = url[len("sqlite:///./") :]
            return f"sqlite:///{(REPO_ROOT / rel).as_posix()}"
        return url

    def wandb_api_key_present(self) -> bool:
        if self.wandb_api_key:
            return True
        netrc = Path.home() / ".netrc"
        try:
            return "api.wandb.ai" in netrc.read_text()
        except OSError:
            return False

    def weave_project_path(self) -> str:
        return f"{self.wandb_entity}/{self.wandb_project}" if self.wandb_entity else self.wandb_project

    def presence_report(self) -> dict[str, dict[str, object]]:
        """Names, presence and lengths only. Never values."""
        secrets = {
            "WANDB_API_KEY": self.wandb_api_key or ("netrc" if self.wandb_api_key_present() else ""),
            "GEMINI_API_KEY": self.gemini_api_key,
            "OPENAI_API_KEY": self.openai_api_key,
            "ELEVENLABS_API_KEY": self.elevenlabs_api_key,
            "TYPESAFE_API_KEY": self.typesafe_api_key,
        }
        report: dict[str, dict[str, object]] = {}
        for name, value in secrets.items():
            report[name] = {
                "present": bool(value),
                "length": 0 if value in ("", "netrc") else len(value),
                "source": "netrc" if value == "netrc" else ("env" if value else "missing"),
            }
        return report


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
