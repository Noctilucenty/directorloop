"""Isolated browser authentication harness. No worker, model calls, or Weave.

The credential and synthetic media exist only in a temporary directory. Verify
its removal after a signal stops the server. This is not a DirectorLoop model evaluation.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

import uvicorn  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from directorloop.api.app import create_app  # noqa: E402
from directorloop.config import Settings  # noqa: E402
from directorloop.runtime.causal import CausalConfig, CausalRun, save_causal  # noqa: E402


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="directorloop-auth-ui-") as directory:
        data = Path(directory)
        token_file = data / "temporary-token"
        token_file.write_text(secrets.token_urlsafe(36))
        token_file.chmod(0o600)
        media = data / "synthetic-auth-test.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=180x320:rate=15", "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(media)], check=True)
        digest = hashlib.sha256(media.read_bytes()).hexdigest()
        now = datetime.now(UTC).isoformat()
        record = CausalRun(
            id="causal_1a099999999_00000000",
            config=CausalConfig(video_id="auth-test-only", video_path=str(media),
                                objective="Authentication test only. No model evaluation performed.", arms=0, plan_only=True),
            status="completed", created_at=now, ended_at=now,
            artifact_hash=digest, mocked_stages=["authentication_test_only"],
            stop_reason="Authentication test only; no audit, render experiment, comparison, or learning.",
            budget={"model_calls": 0, "cost_usd": None},
        )
        save_causal(record, data)
        settings = Settings(_env_file=None, dl_mode="demo", dl_bind_host="127.0.0.1", dl_data_dir=str(data),
                            dl_local_auth_token=token_file.read_text(), dl_weave_enabled=False,
                            openai_api_key="", gemini_api_key="", typesafe_api_key="", wandb_api_key="")
        app = create_app(settings, start_worker=False)
        # Restrict this UI fixture to its synthetic source, with no access to demo media.
        app.state.services.registry = lambda: {"auth-test-only": {"video_id": "auth-test-only", "path": str(media), "title": "Authentication test only · synthetic clip", "role": "upload", "category": "test", "source": "upload", "edit_permission": "owner_upload", "retention": None}}

        @app.middleware("http")
        async def no_jobs(request, call_next):
            if request.method == "POST" and request.url.path != "/api/session":
                return JSONResponse({"detail": "Authentication test server: model jobs and uploads are disabled."}, status_code=403)
            return await call_next(request)

        print(json.dumps({"test_only": True, "url": "http://127.0.0.1:8797/causal/causal_1a099999999_00000000", "temporary_token_file": str(token_file)}), flush=True)
        uvicorn.run(app, host="127.0.0.1", port=8797, log_level="warning")


if __name__ == "__main__":
    main()
