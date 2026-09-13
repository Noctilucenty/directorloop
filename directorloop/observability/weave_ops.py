"""Weave instrumentation helpers.

Ops degrade to plain functions when Weave is not initialized, so the loop runs the
same with or without a WANDB_API_KEY. Trace URLs are only reported when a call
actually exists; nothing here fabricates a remote link.
"""

from __future__ import annotations

import contextlib
import logging
import re
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import wraps
from typing import Any, TypeVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import weave

from ..config import REPO_ROOT, Settings, get_settings

log = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

_REDACT_KEYS = {"apikey", "authorization", "token", "secret", "password", "correctoptionid", "answerkey",
                "accesstoken", "refreshtoken", "clientsecret", "cookie", "setcookie", "sessiontoken",
                "xamzsignature", "xamzcredential", "xamzsecuritytoken", "signature", "sig"}


def _sensitive_key(key: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return normalized in _REDACT_KEYS or normalized.endswith(("apikey", "accesstoken", "refreshtoken", "clientsecret", "password"))


def _safe_text(value: str) -> str:
    """Scrub common credentials embedded in diagnostics and signed media URLs."""
    value = re.sub(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", "[redacted authorization]", value, flags=re.I)
    value = re.sub(r"\bAuthorization\s*[:=]\s*Basic\s+[A-Za-z0-9+/=]+", "[redacted authorization]", value, flags=re.I)
    value = re.sub(r"\b(?:sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{16,}|wandb_v1_[A-Za-z0-9_-]{16,}|apikey_[A-Za-z0-9_-]{16,})", "[redacted key]", value)

    def safe_url(match: re.Match) -> str:
        raw = match.group(0)
        try:
            parts = urlsplit(raw)
            query = parse_qsl(parts.query, keep_blank_values=True)
            sensitive = any(_sensitive_key(k) for k, _ in query)
            if not sensitive and parts.username is None:
                return raw
            host = parts.netloc.rsplit("@", 1)[-1]
            return urlunsplit((parts.scheme, host, parts.path,
                               urlencode([(k, "[redacted]" if _sensitive_key(k) else v) for k, v in query]), parts.fragment))
        except ValueError:
            return "[unparseable URL]"

    return re.sub(r"https?://[^\s\"<>]+", safe_url, value)


def _redact(obj: Any, depth: int = 0) -> Any:
    """JSON-safe, secret-free copy. Pydantic models and dataclasses become plain dicts at every depth:
    Weave treats any nested object with a `ref` attribute as its own reference type, and several of our
    evidence records have a `ref` field."""
    import dataclasses
    import enum
    from pathlib import PurePath

    if depth > 8:
        return "[depth limit]"
    if hasattr(obj, "model_dump") and not isinstance(obj, type):
        try:
            obj = obj.model_dump(mode="json")
        except Exception:  # noqa: BLE001
            return f"<{type(obj).__name__}>"
    elif dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        obj = {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): ("[redacted]" if _sensitive_key(k) else _redact(v, depth + 1)) for k, v in list(obj.items())[:200]}
    if isinstance(obj, list | tuple | set):
        return [_redact(v, depth + 1) for v in list(obj)[:200]]
    if isinstance(obj, bytes):
        return f"<{len(obj)} bytes>"
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, PurePath):
        return str(obj)
    if isinstance(obj, str):
        return _safe_text(obj)
    if obj is None or isinstance(obj, bool | int | float):
        return obj
    return f"<{type(obj).__name__}>"


def redact_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in inputs.items():
        if k in ("self", "provider", "providers", "planner", "on_stage", "is_cancelled") or str(k).startswith("_"):
            continue
        out[k] = "[redacted]" if _sensitive_key(k) else _redact(v)
    return out


def redact_output(output: Any) -> Any:
    return _redact(output)


@dataclass
class WeaveStatus:
    enabled: bool
    connected: bool
    project: str
    reason: str = ""
    traces_url: str | None = None


_status = WeaveStatus(enabled=False, connected=False, project="", reason="not initialized")


def git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=5, check=False
        ).stdout.strip() or "unknown"
    except OSError:
        return "unknown"


def init_weave(settings: Settings | None = None) -> WeaveStatus:
    """Initialize Weave once. Safe to call repeatedly."""
    global _status
    settings = settings or get_settings()
    project = settings.weave_project_path()
    if not settings.dl_weave_enabled:
        _status = WeaveStatus(enabled=False, connected=False, project=project, reason="DL_WEAVE_ENABLED=false")
        return _status
    if not settings.wandb_api_key_present():
        _status = WeaveStatus(enabled=True, connected=False, project=project, reason="WANDB_API_KEY missing")
        return _status
    if _status.connected:
        return _status
    try:
        import os

        if settings.wandb_api_key:
            os.environ.setdefault("WANDB_API_KEY", settings.wandb_api_key)
        os.environ.setdefault("WEAVE_PRINT_CALL_LINK", "false")
        weave.init(
            project,
            global_postprocess_inputs=redact_inputs,
            global_postprocess_output=redact_output,
            global_attributes={"git_commit": git_commit(), "app": "directorloop", "mode": settings.dl_mode},
        )
        _status = WeaveStatus(
            enabled=True,
            connected=True,
            project=project,
            reason="",
            traces_url=f"https://wandb.ai/{project}/weave/traces",
        )
    except Exception as exc:  # noqa: BLE001
        _status = WeaveStatus(enabled=True, connected=False, project=project, reason=f"weave.init failed: {str(exc)[:200]}")
        log.warning("weave init failed: %s", exc)
    return _status


def weave_status() -> WeaveStatus:
    return _status


def _display_func(display: str | Callable[[dict[str, Any]], str], fallback: str) -> str | Callable[[Any], str]:
    if not callable(display):
        return display

    def call_display_name(call: Any) -> str:  # Weave requires exactly one parameter (the Call)
        try:
            return str(display(dict(call.inputs or {})))[:200] or fallback
        except Exception:  # noqa: BLE001 - a naming failure must never break the traced function
            return fallback

    return call_display_name


def _output_func(summarize: Callable[[Any], Any] | None) -> Callable[[Any], Any]:
    if summarize is None:
        return redact_output

    def postprocess_output(output: Any) -> Any:
        try:
            return redact_output(summarize(output))
        except Exception:  # noqa: BLE001
            return redact_output(output)

    return postprocess_output


def traced(name: str, kind: str | None = None, display: str | Callable[[dict[str, Any]], str] | None = None,
           summarize: Callable[[Any], Any] | None = None) -> Callable[[F], F]:
    """Decorate a loop boundary as a Weave op with a stable name.

    display: the span label in the trace tree, a string or a function of the call's (redacted) inputs, computed when the call is
    created. summarize: what the trace shows as the span's output (the caller still receives the real return value)."""

    def deco(fn: F) -> F:
        op = weave.op(
            name=name, kind=kind, call_display_name=_display_func(display, name) if display is not None else None, postprocess_inputs=redact_inputs,
            postprocess_output=_output_func(summarize), enable_code_capture=False
        )(fn)

        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return op(*args, **kwargs)

        wrapper.__weave_op__ = op  # type: ignore[attr-defined]
        return wrapper  # type: ignore[return-value]

    return deco


@contextlib.contextmanager
def attributes(**attrs: Any) -> Iterator[None]:
    with weave.attributes(attrs):
        yield


def current_call_ref() -> tuple[str | None, str | None]:
    """(call_id, ui_url) of the current call, or (None, None) when not tracing."""
    try:
        call = weave.get_current_call()
    except Exception:  # noqa: BLE001
        return None, None
    if call is None:
        return None, None
    try:
        return call.id, call.ui_url
    except Exception:  # noqa: BLE001
        return getattr(call, "id", None), None


def set_display_name(name: str) -> None:
    try:
        call = weave.get_current_call()
        if call is not None:
            call.set_display_name(name)
    except Exception:  # noqa: BLE001
        pass


def flush() -> None:
    try:
        client = weave.get_client()
        if client is not None:
            client.flush()
    except Exception:  # noqa: BLE001
        pass
