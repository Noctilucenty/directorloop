"""Durable job store on SQLite (stdlib): jobs, ordered events, idempotency, heartbeats, crash recovery.

States: QUEUED -> RUNNING -> COMPLETED | FAILED | CANCELED. A cancel request on a running job sets
cancel_requested; the worker checks it between stages. On startup, RUNNING jobs whose heartbeat is older
than the lease are marked FAILED ("worker stopped"), never silently resumed as if nothing happened.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..domain.ids import new_id, sha256_json, utc_now_iso

TERMINAL = {"COMPLETED", "FAILED", "CANCELED"}


class IdempotencyConflict(ValueError):
    pass


@dataclass
class Job:
    id: str
    kind: str
    state: str
    stage: str
    params: dict[str, Any]
    idempotency_key: str | None
    created_at: str
    started_at: str | None
    ended_at: str | None
    heartbeat_at: str | None
    error: str | None
    result: dict[str, Any] | None
    cancel_requested: bool

    def elapsed_ms(self) -> int:
        if not self.started_at:
            return 0
        start = datetime.fromisoformat(self.started_at)
        end = datetime.fromisoformat(self.ended_at) if self.ended_at else datetime.now(UTC)
        return int((end - start).total_seconds() * 1000)

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "state": self.state, "stage": self.stage, "created_at": self.created_at,
            "started_at": self.started_at, "ended_at": self.ended_at, "elapsed_ms": self.elapsed_ms(), "error": self.error,
            "experiment_id": (self.result or {}).get("experiment_id"), "run_id": (self.result or {}).get("run_id") or ("run_" + self.id.removeprefix("job_") if self.kind == "run" else None),
            "abc_id": (self.result or {}).get("abc_id") or ("abc_" + self.id.removeprefix("job_") if self.kind == "abc" else None),
            "params": self.params, "cancel_requested": self.cancel_requested,
        }


class JobStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL, stage TEXT NOT NULL DEFAULT '',
              params TEXT NOT NULL, params_hash TEXT NOT NULL, idempotency_key TEXT UNIQUE,
              created_at TEXT NOT NULL, started_at TEXT, ended_at TEXT, heartbeat_at TEXT,
              error TEXT, result TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS job_events (
              job_id TEXT NOT NULL, seq INTEGER NOT NULL, ts TEXT NOT NULL, stage TEXT NOT NULL, message TEXT NOT NULL, data TEXT,
              PRIMARY KEY (job_id, seq)
            );
            CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, created_at);
            """
        )

    def _row(self, row: tuple) -> Job:
        (jid, kind, state, stage, params, _ph, idem, created, started, ended, hb, error, result, cancel) = row
        return Job(jid, kind, state, stage, json.loads(params), idem, created, started, ended, hb, error, json.loads(result) if result else None, bool(cancel))

    def get(self, job_id: str) -> Job | None:
        cur = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        row = cur.fetchone()
        return self._row(row) if row else None

    def list(self, limit: int = 50) -> list[Job]:
        cur = self._conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
        return [self._row(r) for r in cur.fetchall()]

    def create(self, kind: str, params: dict[str, Any], idempotency_key: str | None = None) -> tuple[Job, bool]:
        """Returns (job, created). The same key with the same body returns the original job; a different body is refused."""
        params_hash = sha256_json({"kind": kind, "params": params})
        with self._lock:
            if idempotency_key:
                cur = self._conn.execute("SELECT * FROM jobs WHERE idempotency_key = ?", (idempotency_key,))
                row = cur.fetchone()
                if row:
                    if row[5] != params_hash:
                        raise IdempotencyConflict("idempotency key was already used for a different request")
                    return self._row(row), False
            jid = new_id("job")
            self._conn.execute(
                "INSERT INTO jobs (id, kind, state, stage, params, params_hash, idempotency_key, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (jid, kind, "QUEUED", "QUEUED", json.dumps(params), params_hash, idempotency_key, utc_now_iso()),
            )
        job = self.get(jid)
        assert job is not None
        return job, True

    def claim_next(self, kind: str | None = None) -> Job | None:
        with self._lock:
            q = "SELECT id FROM jobs WHERE state = 'QUEUED'" + (" AND kind = ?" if kind else "") + " ORDER BY created_at LIMIT 1"
            row = self._conn.execute(q, (kind,) if kind else ()).fetchone()
            if not row:
                return None
            now = utc_now_iso()
            updated = self._conn.execute(
                "UPDATE jobs SET state='RUNNING', stage='STARTING', started_at=?, heartbeat_at=? WHERE id=? AND state='QUEUED'", (now, now, row[0])
            ).rowcount
            if updated != 1:
                return None
        return self.get(row[0])

    def add_event(self, job_id: str, stage: str, message: str, data: dict[str, Any] | None = None) -> int:
        with self._lock:
            seq = (self._conn.execute("SELECT COALESCE(MAX(seq), 0) FROM job_events WHERE job_id = ?", (job_id,)).fetchone()[0]) + 1
            now = utc_now_iso()
            self._conn.execute(
                "INSERT INTO job_events (job_id, seq, ts, stage, message, data) VALUES (?,?,?,?,?,?)",
                (job_id, seq, now, stage, message, json.dumps(data, default=str) if data is not None else None),
            )
            self._conn.execute("UPDATE jobs SET stage=?, heartbeat_at=? WHERE id=?", (stage, now, job_id))
        return seq

    def events(self, job_id: str, after: int = 0) -> list[dict[str, Any]]:
        cur = self._conn.execute("SELECT seq, ts, stage, message, data FROM job_events WHERE job_id=? AND seq>? ORDER BY seq", (job_id, after))
        return [{"seq": s, "ts": ts, "stage": st, "message": m, "data": json.loads(d) if d else None} for s, ts, st, m, d in cur.fetchall()]

    def finish(self, job_id: str, state: str, result: dict[str, Any] | None = None, error: str | None = None) -> None:
        assert state in TERMINAL
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET state=?, stage=?, ended_at=?, result=?, error=? WHERE id=? AND state NOT IN ('COMPLETED','FAILED','CANCELED')",
                (state, state, utc_now_iso(), json.dumps(result, default=str) if result is not None else None, error, job_id),
            )

    def request_cancel(self, job_id: str) -> Job | None:
        with self._lock:
            job = self.get(job_id)
            if job is None or job.state in TERMINAL:
                return job
            if job.state == "QUEUED":
                self._conn.execute("UPDATE jobs SET state='CANCELED', stage='CANCELED', ended_at=?, cancel_requested=1 WHERE id=?", (utc_now_iso(), job_id))
            else:
                self._conn.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (job_id,))
        return self.get(job_id)

    def cancel_requested(self, job_id: str) -> bool:
        row = self._conn.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
        return bool(row and row[0])

    def recover_stale(self, lease_seconds: int = 120) -> list[str]:
        """Mark RUNNING jobs whose heartbeat is older than the lease as FAILED. Returns their ids."""
        now = datetime.now(UTC)
        stale: list[str] = []
        for job in [j for j in self.list(limit=1000) if j.state == "RUNNING"]:
            hb = datetime.fromisoformat(job.heartbeat_at) if job.heartbeat_at else None
            if hb is None or (now - hb).total_seconds() > lease_seconds:
                stale.append(job.id)
        for jid in stale:
            self.add_event(jid, "FAILED", "worker stopped while this job was running; marked failed on restart")
            self.finish(jid, "FAILED", error="worker stopped (lease expired)")
        return stale
