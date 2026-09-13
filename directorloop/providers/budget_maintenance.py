"""Explicit offline maintenance for the canonical screening attempt ceiling.

Stop the gateway and engine before calling this operator-only function. It is
not imported by startup, request handling or retries. Dollar limits and existing
reservations are never changed; SpendLedger construction remains fail-closed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .spend import SpendGuardError

BUDGET_ID = "screening-app-20260912-v1"
MIGRATION_ID = "screening-attempt-ceiling-30-to-64-v1"
LIMIT_UNITS = 2_000_000_000
OLD_ATTEMPTS = 30
NEW_ATTEMPTS = 64
EXTENSION_MIGRATION_ID = "screening-attempt-ceiling-64-to-96-v1"
EXTENDED_ATTEMPTS = 96
DEMO_EXTENSION_MIGRATION_ID = "screening-attempt-ceiling-96-to-160-v1"
DEMO_EXTENDED_ATTEMPTS = 160


def _protected_hash(db: sqlite3.Connection) -> str:
    """Include every budget's attempts and run policies, not just this budget."""
    records = {
        table: db.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
        for table in ("spend_attempts", "spend_run_policies", "spend_attempt_runs")
    }
    return hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def upgrade_screening_attempt_ceiling(
    path: str | Path, *, expected_attempts: int, expected_held_units: int, reason: str,
) -> dict[str, Any]:
    """Audit the one-time 30-to-64 upgrade without resetting the same $2 budget.

    Both usage expectations must come from an operator's fresh read. A mismatch,
    unsettled charge, halted budget or partial write fails closed. A repeated
    call with the same expectations and reason returns its original receipt.
    No database is created when the supplied path is missing.
    """
    if type(expected_attempts) is not int or not 0 <= expected_attempts <= OLD_ATTEMPTS:
        raise SpendGuardError("maintenance needs the exact previous attempt count")
    if type(expected_held_units) is not int or not 0 <= expected_held_units <= LIMIT_UNITS:
        raise SpendGuardError("maintenance needs the exact previous held amount")
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
        raise SpendGuardError("maintenance needs a short operator rationale without secrets")
    reason = reason.strip()
    db = None
    try:
        db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=rw", uri=True, timeout=30)
        db.execute("PRAGMA busy_timeout = 30000")
        with db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS spend_maintenance_audit (
                migration_id TEXT PRIMARY KEY, receipt_json TEXT NOT NULL
            )""")
            budget = db.execute(
                "SELECT limit_units, max_attempts, halted FROM spend_budgets WHERE budget_id=?", (BUDGET_ID,),
            ).fetchone()
            previous = db.execute(
                "SELECT receipt_json FROM spend_maintenance_audit WHERE migration_id=?", (MIGRATION_ID,),
            ).fetchone()
            if previous:
                receipt = json.loads(previous[0])
                if (budget is None or budget[:2] != (LIMIT_UNITS, NEW_ATTEMPTS)
                        or receipt.get("physical_attempts") != expected_attempts
                        or receipt.get("held_units") != expected_held_units
                        or receipt.get("reason") != reason):
                    raise SpendGuardError("maintenance receipt conflicts with the requested upgrade")
                return receipt
            if budget != (LIMIT_UNITS, OLD_ATTEMPTS, 0):
                raise SpendGuardError("maintenance requires the unchanged active $2 / 30-attempt budget")
            usage = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(held_units),0) FROM spend_attempts WHERE budget_id=?", (BUDGET_ID,),
            ).fetchone()
            if usage != (expected_attempts, expected_held_units):
                raise SpendGuardError("maintenance usage changed since the operator check")
            if db.execute(
                "SELECT 1 FROM spend_attempts WHERE budget_id=? AND status!='settled' LIMIT 1", (BUDGET_ID,),
            ).fetchone():
                raise SpendGuardError("maintenance requires every prior attempt to be settled")
            before_hash = _protected_hash(db)
            changed = db.execute(
                """UPDATE spend_budgets SET max_attempts=?
                   WHERE budget_id=? AND limit_units=? AND max_attempts=? AND halted=0""",
                (NEW_ATTEMPTS, BUDGET_ID, LIMIT_UNITS, OLD_ATTEMPTS),
            ).rowcount
            if changed != 1 or _protected_hash(db) != before_hash:
                raise SpendGuardError("maintenance did not preserve all reservations and run policies")
            receipt = {
                "migration_id": MIGRATION_ID,
                "applied_at": datetime.now(UTC).isoformat(),
                "budget_id": BUDGET_ID,
                "limit_units": LIMIT_UNITS,
                "old_max_physical_attempts": OLD_ATTEMPTS,
                "new_max_physical_attempts": NEW_ATTEMPTS,
                "physical_attempts": expected_attempts,
                "held_units": expected_held_units,
                "protected_rows_sha256": before_hash,
                "reason": reason,
            }
            db.execute("INSERT INTO spend_maintenance_audit VALUES (?, ?)",
                       (MIGRATION_ID, json.dumps(receipt, sort_keys=True)))
            return receipt
    except sqlite3.Error as exc:
        raise SpendGuardError("offline budget maintenance failed; transaction rolled back") from exc
    finally:
        if db is not None:
            db.close()


def extend_screening_attempt_ceiling(
    path: str | Path, *, expected_attempts: int, expected_held_units: int, reason: str,
) -> dict[str, Any]:
    """Explicitly extend the same $2 budget from 64 to 96 physical attempts.

    The operator MUST stop the gateway and engine, confirm all jobs are idle,
    and read fresh usage before calling. SQLite's write transaction prevents
    concurrent ledger writes; it cannot establish application job idleness.
    This is never called by startup, requests, or retry handling. It does not
    replace or modify the earlier 30-to-64 maintenance receipt.

    Halted budgets, unsettled reservations, wrong ceilings, and stale usage
    expectations fail closed, including on a repeated call. Identical replay
    returns the original receipt only while the protected ledger is unchanged.
    Missing files are not created. No dollar limit or prior charge is changed.
    """
    return _extend_attempt_ceiling(
        path, expected_attempts=expected_attempts, expected_held_units=expected_held_units,
        reason=reason, old_attempts=NEW_ATTEMPTS, new_attempts=EXTENDED_ATTEMPTS,
        migration_id=EXTENSION_MIGRATION_ID,
    )


def extend_screening_attempt_ceiling_to_160(
    path: str | Path, *, expected_attempts: int, expected_held_units: int, reason: str,
) -> dict[str, Any]:
    """Operator-only 96-to-160 extension; the same $2 cap and charges survive.

    Stop both gateway and engine and confirm idle jobs before taking the fresh
    usage snapshot and calling. All previous maintenance receipts are retained.
    No startup, request, or retry code may call this function. The same strict
    state, protected-row, and idempotency guards as the 64-to-96 extension apply.
    """
    return _extend_attempt_ceiling(
        path, expected_attempts=expected_attempts, expected_held_units=expected_held_units,
        reason=reason, old_attempts=EXTENDED_ATTEMPTS, new_attempts=DEMO_EXTENDED_ATTEMPTS,
        migration_id=DEMO_EXTENSION_MIGRATION_ID,
    )


def _extend_attempt_ceiling(
    path: str | Path, *, expected_attempts: int, expected_held_units: int, reason: str,
    old_attempts: int, new_attempts: int, migration_id: str,
) -> dict[str, Any]:
    """Transaction shared by the two explicit, fixed operator extensions."""
    if type(expected_attempts) is not int or not 0 <= expected_attempts <= old_attempts:
        raise SpendGuardError("maintenance needs the exact previous attempt count")
    if type(expected_held_units) is not int or not 0 <= expected_held_units <= LIMIT_UNITS:
        raise SpendGuardError("maintenance needs the exact previous held amount")
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
        raise SpendGuardError("maintenance needs a short operator rationale without secrets")
    reason = reason.strip()
    db = None
    try:
        db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=rw", uri=True, timeout=30)
        db.execute("PRAGMA busy_timeout = 30000")
        with db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS spend_maintenance_audit (
                migration_id TEXT PRIMARY KEY, receipt_json TEXT NOT NULL
            )""")
            budget = db.execute(
                "SELECT limit_units, max_attempts, halted FROM spend_budgets WHERE budget_id=?", (BUDGET_ID,),
            ).fetchone()
            if budget not in ((LIMIT_UNITS, old_attempts, 0), (LIMIT_UNITS, new_attempts, 0)):
                raise SpendGuardError(f"maintenance requires the unchanged active $2 / {old_attempts}-attempt budget")
            usage = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(held_units),0) FROM spend_attempts WHERE budget_id=?", (BUDGET_ID,),
            ).fetchone()
            if usage != (expected_attempts, expected_held_units):
                raise SpendGuardError("maintenance usage changed since the operator check")
            if db.execute(
                "SELECT 1 FROM spend_attempts WHERE budget_id=? AND status!='settled' LIMIT 1", (BUDGET_ID,),
            ).fetchone():
                raise SpendGuardError("maintenance requires every prior attempt to be settled")
            protected_hash = _protected_hash(db)
            audit_rows = db.execute("SELECT * FROM spend_maintenance_audit ORDER BY rowid").fetchall()
            previous = next((row[1] for row in audit_rows if row[0] == migration_id), None)
            receipt_fields = {
                "migration_id": migration_id,
                "budget_id": BUDGET_ID,
                "limit_units": LIMIT_UNITS,
                "old_max_physical_attempts": old_attempts,
                "new_max_physical_attempts": new_attempts,
                "physical_attempts": expected_attempts,
                "held_units": expected_held_units,
                "protected_rows_sha256": protected_hash,
                "reason": reason,
            }
            if previous is not None:
                receipt = json.loads(previous)
                if (budget != (LIMIT_UNITS, new_attempts, 0)
                        or not isinstance(receipt, dict)
                        or any(receipt.get(key) != value for key, value in receipt_fields.items())):
                    raise SpendGuardError("maintenance receipt conflicts with the requested extension")
                return receipt
            if budget != (LIMIT_UNITS, old_attempts, 0):
                raise SpendGuardError("maintenance requires the previous ceiling and an absent extension receipt")
            # Preserve all other budget columns and every unrelated budget too.
            budgets_before = db.execute("SELECT * FROM spend_budgets ORDER BY rowid").fetchall()
            expected_budgets = [
                (*row[:3], new_attempts, *row[4:]) if row[0] == BUDGET_ID else row
                for row in budgets_before
            ]
            changed = db.execute(
                """UPDATE spend_budgets SET max_attempts=?
                   WHERE budget_id=? AND limit_units=? AND max_attempts=? AND halted=0""",
                (new_attempts, BUDGET_ID, LIMIT_UNITS, old_attempts),
            ).rowcount
            if (changed != 1 or _protected_hash(db) != protected_hash
                    or db.execute("SELECT * FROM spend_budgets ORDER BY rowid").fetchall() != expected_budgets
                    or db.execute("SELECT * FROM spend_maintenance_audit ORDER BY rowid").fetchall() != audit_rows):
                raise SpendGuardError("maintenance did not preserve the ledger and prior maintenance receipts")
            receipt = {**receipt_fields, "applied_at": datetime.now(UTC).isoformat()}
            receipt_json = json.dumps(receipt, sort_keys=True)
            db.execute("INSERT INTO spend_maintenance_audit VALUES (?, ?)",
                       (migration_id, receipt_json))
            if (_protected_hash(db) != protected_hash
                    or db.execute("SELECT * FROM spend_budgets ORDER BY rowid").fetchall() != expected_budgets
                    or db.execute("SELECT * FROM spend_maintenance_audit ORDER BY rowid").fetchall()
                    != [*audit_rows, (migration_id, receipt_json)]):
                raise SpendGuardError("maintenance did not preserve the ledger while writing its receipt")
            return receipt
    except (sqlite3.Error, json.JSONDecodeError) as exc:
        raise SpendGuardError("offline budget maintenance failed; transaction rolled back") from exc
    finally:
        if db is not None:
            db.close()
