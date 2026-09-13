"""Shared stage evidence for the product UI and nested Weave calls.

Stages describe work as it executes. They never run models, change decisions, or
manufacture remote links. Context variables carry the same parent through Weave's
context-preserving executor; sibling workers share a synchronized event journal.
"""

from __future__ import annotations

import contextlib
import inspect
import logging
import time
from asyncio import CancelledError
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache, wraps
from threading import RLock
from typing import Any, Literal
from uuid import uuid4

import weave
from pydantic import BaseModel, Field
from weave.trace.context import call_context

from ..jobs.worker import JobCanceled
from .weave_ops import redact_inputs, redact_output

log = logging.getLogger(__name__)


class WorkflowStage(BaseModel):
    id: str
    parent_id: str | None = None
    key: str
    title: str
    kind: str = "tool"
    iteration: int | None = None
    status: Literal["running", "completed", "failed", "skipped", "incomplete", "cancelled"] = "running"
    started_at: str
    ended_at: str | None = None
    duration_ms: int | None = None
    weave_call_id: str | None = None
    weave_url: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)

    def update(self, **summary: Any) -> None:
        self.summary.update(redact_output(summary))


@dataclass
class _Journal:
    records: list[WorkflowStage] = field(default_factory=list)
    callback: Callable[..., Any] | None = None
    lock: RLock = field(default_factory=RLock)
    terminal: tuple[str, str, Any] | None = None
    scopes: dict[str, str | None] = field(default_factory=dict)
    record_scopes: dict[str, str | None] = field(default_factory=dict)
    attached: dict[str | None, Any] = field(default_factory=dict)

    def emit(self, stage: WorkflowStage) -> None:
        if self.callback:
            # Serialize parallel stage events, including their sequence allocation.
            with self.lock:
                try:
                    self.callback("WORKFLOW_STAGE", stage.title, {"stage": stage.model_dump(mode="json")})
                except JobCanceled:
                    raise
                except Exception:
                    log.warning("Workflow event delivery failed", exc_info=False)


_journal: ContextVar[_Journal | None] = ContextVar("directorloop_workflow", default=None)
_parent: ContextVar[WorkflowStage | None] = ContextVar("directorloop_workflow_parent", default=None)
_scope: ContextVar[str | None] = ContextVar("directorloop_workflow_scope", default=None)


def snapshot_workflow() -> list[WorkflowStage]:
    journal = _journal.get()
    if journal is None:
        return []
    with journal.lock:
        return [record.model_copy(deep=True) for record in journal.records]


def attach_workflow(record: Any) -> None:
    """Let existing intermediate saves include stages without changing their timing."""
    journal = _journal.get()
    if journal is not None and hasattr(record, "workflow_stages"):
        record.workflow_stages = journal.records
        journal.attached[_scope.get()] = record


@lru_cache(maxsize=128)
def _stage_op(key: str, kind: str) -> Any:
    def boundary(**inputs: Any) -> None:
        """Execution boundary; actual work is performed inside workflow_stage."""

    return weave.op(name=f"directorloop.stage.{key}", kind=kind, enable_code_capture=False)(boundary)


@contextlib.contextmanager
def workflow_stage(key: str, title: str, *, kind: str = "tool", iteration: int | None = None,
                   inputs: dict[str, Any] | None = None) -> Iterator[WorkflowStage]:
    journal = _journal.get()
    parent = _parent.get()
    stage = WorkflowStage(
        id=f"stage_{uuid4().hex}", parent_id=parent.id if parent else None,
        key=key, title=title, kind=kind,
        iteration=iteration if iteration is not None else parent.iteration if parent else None,
        started_at=datetime.now(UTC).isoformat(), inputs=redact_inputs(inputs or {}),
    )
    started = time.monotonic()
    stack_scope = call_context.set_call_stack(call_context.get_call_stack())
    stack_scope.__enter__()
    client = call = None
    try:
        client = weave.get_client()
        if client is not None and weave.get_current_call() is not None:
            call = client.create_call(
                _stage_op(key, kind), inputs=stage.inputs, display_name=title,
                attributes={"workflow_stage": key, "iteration": stage.iteration,
                            "workflow_stage_id": stage.id, "evidence_source": "execution"},
                use_stack=True,
            )
            stage.weave_call_id = call.id
            stage.weave_url = call.ui_url
    except Exception:
        # Instrumentation availability must not control product behavior.
        log.warning("Weave stage creation unavailable", exc_info=False)
    if journal is not None:
        with journal.lock:
            journal.records.append(stage)
            journal.record_scopes[stage.id] = _scope.get()
    token = _parent.set(stage)
    failure: BaseException | None = None
    try:
        if journal is not None:
            journal.emit(stage)
        yield stage
    except BaseException as exc:
        stage.status = "cancelled" if isinstance(exc, (KeyboardInterrupt, CancelledError, JobCanceled)) else "failed"
        stage.update(error_type=type(exc).__name__)
        # Preserve the caller's original exception; avoid copying provider secrets
        # from exception messages to a second trace payload.
        failure = RuntimeError(type(exc).__name__)
        raise
    finally:
        _parent.reset(token)
        if stage.status == "running":
            stage.status = "completed"
        stage.ended_at = datetime.now(UTC).isoformat()
        stage.duration_ms = max(0, int((time.monotonic() - started) * 1000))
        if client is not None and call is not None:
            try:
                client.finish_call(call, output={"status": stage.status, **stage.summary}, exception=failure)
            except Exception:
                log.warning("Weave stage completion unavailable", exc_info=False)
        # Also restore the stack if a remote finish failed before the SDK popped
        # its call. The next stage must never inherit a stale completed parent.
        stack_scope.__exit__(None, None, None)
        if journal is not None:
            journal.emit(stage)


def workflow_session(fn: Callable[..., Any] | None = None, *, persist: Callable[..., Any] | None = None) -> Any:
    """Apply underneath @traced: one journal per session, reused by nested audits.

persist(result, data_dir) updates the existing record after scopes close. Terminal
job events are delivered last, so consumers cannot finish before stage evidence.
"""
    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        signature = inspect.signature(func)

        @wraps(func)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            bound = signature.bind(*args, **kwargs)
            journal = _journal.get()
            owner = journal is None
            if owner:
                journal = _Journal(callback=bound.arguments.get("on_stage"))
            assert journal is not None
            scope_id = uuid4().hex
            with journal.lock:
                journal.scopes[scope_id] = _scope.get()
            scope_token = _scope.set(scope_id)
            journal_token = _journal.set(journal)
            parent_token = _parent.set(None) if owner else None
            original_callback = bound.arguments.get("on_stage")
            if owner and original_callback:
                def ordered_callback(stage: str, message: str, data: Any = None) -> None:
                    if stage in {"DONE", "FAILED", "CANCELED"}:
                        journal.terminal = stage, message, data
                    else:
                        with journal.lock:
                            original_callback(stage, message, data)
                bound.arguments["on_stage"] = ordered_callback

            def finish_record(result: Any) -> None:
                if hasattr(result, "workflow_stages"):
                    # Nested parallel audits can interleave in the journal. Keep
                    # only descendants of their starting parent, not their peers.
                    records = snapshot_workflow()
                    if not owner:
                        def belongs(record: WorkflowStage) -> bool:
                            scope = journal.record_scopes.get(record.id)
                            while scope is not None:
                                if scope == scope_id:
                                    return True
                                scope = journal.scopes.get(scope)
                            return False
                        records = [record for record in records if belongs(record)]
                    result.workflow_stages = records
                if persist is not None:
                    try:
                        persist(result, bound.arguments["data_dir"])
                    except Exception:
                        log.warning("Final workflow evidence persistence failed", exc_info=False)
                if owner and journal.terminal and original_callback:
                    original_callback(*journal.terminal)

            try:
                result = func(*bound.args, **bound.kwargs)
                finish_record(result)
                return result
            except BaseException:
                # The API/CLI still owns the run's failure status. Persist only
                # the closed stage evidence before its handler reloads that run.
                if scope_id in journal.attached:
                    finish_record(journal.attached[scope_id])
                raise
            finally:
                if parent_token is not None:
                    _parent.reset(parent_token)
                _scope.reset(scope_token)
                _journal.reset(journal_token)

        return wrapped

    return decorate(fn) if fn is not None else decorate
