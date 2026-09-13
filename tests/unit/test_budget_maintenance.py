"""Offline cap migration preserves billed attempts and runtime enforcement."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from pydantic import ValidationError

from directorloop.config import Settings
from directorloop.providers import budget_maintenance as M
from directorloop.providers.spend import SpendGuardError, SpendLedger, SpendLimitExceeded
from tests.unit.test_provider_spend import CARD


def setup_ledger(tmp_path, *, limit=2, attempts=30, run_policy=False):
    path = tmp_path / "spend.db"
    kwargs = {"run_limit_usd": 1, "max_run_attempts": 30} if run_policy else {}
    ledger = SpendLedger(path, M.BUDGET_ID, limit, max_attempts=attempts, **kwargs)
    for _ in range(3):
        attempt = ledger.reserve(CARD, 5, run_id="before" if run_policy else None)
        ledger.settle(attempt, 10, 2)
    return path, ledger


def upgrade(path, **kwargs):
    return M.upgrade_screening_attempt_ceiling(
        path, **({"expected_attempts": 3, "expected_held_units": 36000,
                  "reason": "Authorized demo ceiling extension within the same $2 budget."} | kwargs),
    )


def rows(path):
    with sqlite3.connect(path) as db:
        return {table: db.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
                for table in ("spend_budgets", "spend_attempts", "spend_run_policies", "spend_attempt_runs")}


def test_exact_upgrade_preserves_attempts_and_run_policies_and_is_idempotent(tmp_path):
    path, _ = setup_ledger(tmp_path, run_policy=True)
    before = rows(path)
    receipt = upgrade(path)
    assert receipt["held_units"] == 36000 and receipt["physical_attempts"] == 3
    assert receipt["limit_units"] == 2_000_000_000
    assert receipt["old_max_physical_attempts"] == 30
    assert receipt["new_max_physical_attempts"] == 64
    after = rows(path)
    old_budget, new_budget = before.pop("spend_budgets")[0], after.pop("spend_budgets")[0]
    assert old_budget[:3] == new_budget[:3]  # ID, dollar cap, halt
    assert old_budget[3] == 30 and new_budget[3] == 64 and old_budget[4:] == new_budget[4:]
    assert before == after
    assert upgrade(path) == receipt
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM spend_maintenance_audit").fetchone()[0] == 1
    SpendLedger(path, M.BUDGET_ID, 2, max_attempts=64, run_limit_usd=1, max_run_attempts=30)
    with pytest.raises(SpendGuardError, match="fixed limit"):
        SpendLedger(path, M.BUDGET_ID, 2, max_attempts=30, run_limit_usd=1, max_run_attempts=30)


@pytest.mark.parametrize("change", ["limit", "ceiling", "count", "held", "halted", "reserved", "unknown", "budget_id"])
def test_wrong_or_uncertain_prior_state_rolls_back_everything(tmp_path, change):
    path, ledger = setup_ledger(tmp_path)
    kwargs = {}
    with sqlite3.connect(path) as db:
        if change == "limit":
            db.execute("UPDATE spend_budgets SET limit_units=1000000000")
        elif change == "ceiling":
            db.execute("UPDATE spend_budgets SET max_attempts=31")
        elif change == "halted":
            db.execute("UPDATE spend_budgets SET halted=1")
        elif change == "budget_id":
            db.execute("UPDATE spend_budgets SET budget_id='unrelated'")
        elif change in {"reserved", "unknown"}:
            db.execute("UPDATE spend_attempts SET status=? WHERE rowid=1", (change,))
        elif change == "count":
            kwargs["expected_attempts"] = 2
        elif change == "held":
            kwargs["expected_held_units"] = 1
    before = rows(path)
    with pytest.raises(SpendGuardError):
        upgrade(path, **kwargs)
    assert before == rows(path)
    with sqlite3.connect(path) as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='spend_maintenance_audit'").fetchall()


def test_interrupted_update_rolls_back_cap_and_audit(tmp_path, monkeypatch):
    path, _ = setup_ledger(tmp_path)
    before = rows(path)
    original = M._protected_hash
    calls = 0

    def fail_after_update(db):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("Injected interruption after UPDATE")
        return original(db)

    monkeypatch.setattr(M, "_protected_hash", fail_after_update)
    with pytest.raises(RuntimeError, match="Injected interruption"):
        upgrade(path)
    assert before == rows(path)
    with sqlite3.connect(path) as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='spend_maintenance_audit'").fetchall()


def test_conflicting_repeated_upgrade_fails_without_changing_rows(tmp_path):
    path, _ = setup_ledger(tmp_path)
    original = upgrade(path)
    before = rows(path)
    with pytest.raises(SpendGuardError, match="receipt conflicts"):
        upgrade(path, reason="A different request")
    assert rows(path) == before
    assert upgrade(path) == original


def test_missing_file_is_not_created(tmp_path):
    path = tmp_path / "absent.db"
    with pytest.raises(SpendGuardError):
        upgrade(path)
    assert not path.exists()


@pytest.mark.parametrize("dollar_bound", [False, True])
def test_parallel_reservations_still_enforce_both_limits(tmp_path, dollar_bound):
    path, _ = setup_ledger(tmp_path)
    upgrade(path)
    ledger = SpendLedger(path, M.BUDGET_ID, 2, max_attempts=64)
    card = replace(CARD, input_usd_per_million="20000") if dollar_bound else CARD

    def reserve(_):
        try:
            return ledger.reserve(card, 5)
        except SpendLimitExceeded:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(80)))
    summary = ledger.summary()
    assert summary["held_usd"] <= 2
    assert summary["physical_attempts"] <= 64
    assert sum(item is not None for item in results) == (0 if dollar_bound else 61)


def test_configuration_keeps_default_and_dollar_ceiling(monkeypatch):
    monkeypatch.delenv("DL_SCREENING_MAX_PHYSICAL_ATTEMPTS", raising=False)
    assert Settings(_env_file=None).dl_screening_max_physical_attempts == 30
    assert Settings(_env_file=None, dl_screening_max_physical_attempts=64).dl_screening_max_physical_attempts == 64
    assert Settings(_env_file=None, dl_screening_max_physical_attempts=96).dl_screening_max_physical_attempts == 96
    assert Settings(_env_file=None, dl_screening_max_physical_attempts=160).dl_screening_max_physical_attempts == 160
    with pytest.raises(ValidationError):
        Settings(_env_file=None, dl_screening_max_physical_attempts=161)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, dl_screening_spend_limit_usd=2.01)


def extend(path, **kwargs):
    return M.extend_screening_attempt_ceiling(
        path, **({"expected_attempts": 3, "expected_held_units": 36000,
                  "reason": "Authorized 64-to-96 demo extension within the same $2 budget."} | kwargs),
    )


def audit_rows(path):
    with sqlite3.connect(path) as db:
        return db.execute("SELECT * FROM spend_maintenance_audit ORDER BY rowid").fetchall()


def test_second_extension_preserves_original_receipt_and_all_other_ledger_rows(tmp_path):
    path, _ = setup_ledger(tmp_path, run_policy=True)
    upgrade(path)
    unrelated = SpendLedger(path, "unrelated", 1, max_attempts=5)
    attempt = unrelated.reserve(CARD, 5)
    unrelated.settle(attempt, 10, 2)
    before = rows(path)
    old_audit = audit_rows(path)
    receipt = extend(path)
    assert receipt["migration_id"] == M.EXTENSION_MIGRATION_ID
    assert receipt["old_max_physical_attempts"] == 64
    assert receipt["new_max_physical_attempts"] == 96
    assert receipt["limit_units"] == 2_000_000_000
    assert receipt["physical_attempts"] == 3 and receipt["held_units"] == 36000
    after = rows(path)
    budget_before = before.pop("spend_budgets")
    budget_after = after.pop("spend_budgets")
    assert before == after
    assert budget_before[1:] == budget_after[1:]
    assert (*budget_before[0][:3], 96, *budget_before[0][4:]) == budget_after[0]
    assert audit_rows(path)[:1] == old_audit
    assert len(audit_rows(path)) == 2
    assert extend(path) == receipt
    SpendLedger(path, M.BUDGET_ID, 2, max_attempts=96, run_limit_usd=1, max_run_attempts=30)
    with pytest.raises(SpendGuardError, match="fixed limit"):
        SpendLedger(path, M.BUDGET_ID, 2, max_attempts=64)


@pytest.mark.parametrize("replayed", [False, True])
@pytest.mark.parametrize("change", ["limit", "ceiling", "count", "held", "halted", "reserved", "unknown", "budget_id"])
def test_extension_uncertain_or_stale_state_fails_closed_including_replay(tmp_path, replayed, change):
    path, _ = setup_ledger(tmp_path)
    upgrade(path)
    if replayed:
        extend(path)
    kwargs = {}
    with sqlite3.connect(path) as db:
        if change == "limit":
            db.execute("UPDATE spend_budgets SET limit_units=1000000000")
        elif change == "ceiling":
            db.execute("UPDATE spend_budgets SET max_attempts=65")
        elif change == "halted":
            db.execute("UPDATE spend_budgets SET halted=1")
        elif change == "budget_id":
            db.execute("UPDATE spend_budgets SET budget_id='unrelated'")
        elif change in {"reserved", "unknown"}:
            db.execute("UPDATE spend_attempts SET status=? WHERE rowid=1", (change,))
        elif change == "count":
            kwargs["expected_attempts"] = 2
        elif change == "held":
            kwargs["expected_held_units"] = 1
    before, previous_audit = rows(path), audit_rows(path)
    with pytest.raises(SpendGuardError):
        extend(path, **kwargs)
    assert rows(path) == before and audit_rows(path) == previous_audit


def test_extension_replay_rejects_usage_or_protected_evidence_changed_since_receipt(tmp_path):
    path, _ = setup_ledger(tmp_path)
    upgrade(path)
    extend(path)
    ledger = SpendLedger(path, M.BUDGET_ID, 2, max_attempts=96)
    attempt = ledger.reserve(CARD, 5)
    ledger.settle(attempt, 10, 2)
    with pytest.raises(SpendGuardError, match="usage changed"):
        extend(path)
    with pytest.raises(SpendGuardError, match="receipt conflicts"):
        extend(path, expected_attempts=4, expected_held_units=48000)


@pytest.mark.parametrize("trigger", ["budget", "attempt", "old_receipt", "new_receipt"])
def test_extension_rolls_back_if_update_or_audit_trigger_changes_protected_rows(tmp_path, trigger):
    path, _ = setup_ledger(tmp_path)
    upgrade(path)
    before, previous_audit = rows(path), audit_rows(path)
    actions = {
        "budget": "AFTER UPDATE ON spend_budgets BEGIN UPDATE spend_budgets SET created_at='changed'; END",
        "attempt": "AFTER UPDATE ON spend_budgets BEGIN UPDATE spend_attempts SET input_tokens=999; END",
        "old_receipt": "AFTER INSERT ON spend_maintenance_audit BEGIN DELETE FROM spend_maintenance_audit WHERE migration_id='"
                       + M.MIGRATION_ID + "'; END",
        "new_receipt": "AFTER INSERT ON spend_maintenance_audit BEGIN UPDATE spend_maintenance_audit SET receipt_json='{}' "
                       "WHERE migration_id=NEW.migration_id; END",
    }
    with sqlite3.connect(path) as db:
        db.execute("CREATE TRIGGER corrupt_maintenance " + actions[trigger])
    with pytest.raises(SpendGuardError, match="did not preserve"):
        extend(path)
    assert rows(path) == before and audit_rows(path) == previous_audit


def test_extension_interrupted_transaction_leaves_original_ceiling_and_receipt(tmp_path, monkeypatch):
    path, _ = setup_ledger(tmp_path)
    upgrade(path)
    before, previous_audit = rows(path), audit_rows(path)
    original, calls = M._protected_hash, 0

    def interrupt(db):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("Interrupted after receipt insertion")
        return original(db)

    monkeypatch.setattr(M, "_protected_hash", interrupt)
    with pytest.raises(RuntimeError, match="Interrupted"):
        extend(path)
    assert rows(path) == before and audit_rows(path) == previous_audit


def test_extension_refuses_missing_file_or_unaudited_existing_96_ceiling(tmp_path):
    missing = tmp_path / "missing.db"
    with pytest.raises(SpendGuardError):
        extend(missing)
    assert not missing.exists()
    path, _ = setup_ledger(tmp_path, attempts=96)
    before = rows(path)
    with pytest.raises(SpendGuardError, match="absent extension receipt"):
        extend(path)
    assert rows(path) == before


@pytest.mark.parametrize("kwargs", [
    {"expected_attempts": True}, {"expected_attempts": 65}, {"expected_attempts": -1},
    {"expected_held_units": True}, {"expected_held_units": -1}, {"expected_held_units": 2000000001},
    {"reason": " "}, {"reason": "x" * 501},
])
def test_extension_validates_operator_inputs_before_touching_database(tmp_path, kwargs):
    path = tmp_path / "absent.db"
    with pytest.raises(SpendGuardError):
        extend(path, **kwargs)
    assert not path.exists()


@pytest.mark.parametrize("dollar_bound", [False, True])
def test_extended_ledger_parallel_reservations_still_enforce_same_dollar_and_attempt_limits(tmp_path, dollar_bound):
    path, _ = setup_ledger(tmp_path)
    upgrade(path)
    extend(path)
    ledger = SpendLedger(path, M.BUDGET_ID, 2, max_attempts=96)
    card = replace(CARD, input_usd_per_million="20000") if dollar_bound else CARD

    def reserve(_):
        try:
            return ledger.reserve(card, 5)
        except SpendLimitExceeded:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(105)))
    summary = ledger.summary()
    assert summary["held_usd"] <= 2
    assert summary["physical_attempts"] <= 96
    assert sum(item is not None for item in results) == (0 if dollar_bound else 93)


def extend_to_160(path, **kwargs):
    return M.extend_screening_attempt_ceiling_to_160(
        path, **({"expected_attempts": 3, "expected_held_units": 36000,
                  "reason": "Authorized finite demo allowance including bounded evidence re-reviews; same $2 cap."} | kwargs),
    )


def test_demo_extension_preserves_both_previous_receipts_and_same_dollar_limit(tmp_path):
    path, _ = setup_ledger(tmp_path, run_policy=True)
    upgrade(path)
    extend(path)
    before, previous_audit = rows(path), audit_rows(path)
    receipt = extend_to_160(path)
    assert receipt["migration_id"] == M.DEMO_EXTENSION_MIGRATION_ID
    assert receipt["old_max_physical_attempts"] == 96
    assert receipt["new_max_physical_attempts"] == 160
    assert receipt["limit_units"] == 2_000_000_000
    assert receipt["physical_attempts"] == 3 and receipt["held_units"] == 36000
    after = rows(path)
    old_budget, new_budget = before.pop("spend_budgets")[0], after.pop("spend_budgets")[0]
    assert before == after
    assert (*old_budget[:3], 160, *old_budget[4:]) == new_budget
    assert audit_rows(path)[:2] == previous_audit
    assert len(audit_rows(path)) == 3
    assert extend_to_160(path) == receipt
    SpendLedger(path, M.BUDGET_ID, 2, max_attempts=160, run_limit_usd=1, max_run_attempts=30)
    with pytest.raises(SpendGuardError, match="fixed limit"):
        SpendLedger(path, M.BUDGET_ID, 2, max_attempts=96)


@pytest.mark.parametrize("replayed", [False, True])
@pytest.mark.parametrize("change", ["limit", "ceiling", "halted", "unsettled", "count", "held", "rationale", "hash"])
def test_demo_extension_fails_closed_on_uncertain_state_and_conflicting_replay(tmp_path, replayed, change):
    path, _ = setup_ledger(tmp_path)
    upgrade(path)
    extend(path)
    if replayed:
        extend_to_160(path)
    kwargs = {}
    with sqlite3.connect(path) as db:
        if change == "limit":
            db.execute("UPDATE spend_budgets SET limit_units=3000000000")
        elif change == "ceiling":
            db.execute("UPDATE spend_budgets SET max_attempts=97")
        elif change == "halted":
            db.execute("UPDATE spend_budgets SET halted=1")
        elif change == "unsettled":
            db.execute("UPDATE spend_attempts SET status='reserved' WHERE rowid=1")
        elif change == "count":
            kwargs["expected_attempts"] = 4
        elif change == "held":
            kwargs["expected_held_units"] = 1
        elif change == "rationale":
            kwargs["reason"] = "Different reason" if replayed else " "
        elif change == "hash":
            if replayed:
                db.execute("UPDATE spend_attempts SET input_tokens=11 WHERE rowid=1")
            else:
                db.execute("CREATE TRIGGER tamper AFTER UPDATE ON spend_budgets BEGIN "
                           "UPDATE spend_attempts SET input_tokens=11 WHERE rowid=1; END")
    before, previous_audit = rows(path), audit_rows(path)
    with pytest.raises(SpendGuardError):
        extend_to_160(path, **kwargs)
    assert rows(path) == before and audit_rows(path) == previous_audit


def test_demo_extension_cannot_skip_64_to_96_or_create_new_file(tmp_path):
    path, _ = setup_ledger(tmp_path)
    upgrade(path)
    before, previous_audit = rows(path), audit_rows(path)
    with pytest.raises(SpendGuardError, match="96-attempt"):
        extend_to_160(path)
    assert rows(path) == before and audit_rows(path) == previous_audit
    missing = tmp_path / "missing.db"
    with pytest.raises(SpendGuardError):
        extend_to_160(missing)
    assert not missing.exists()


@pytest.mark.parametrize("dollar_bound", [False, True])
def test_demo_extension_retains_finite_parallel_runtime_admission_limits(tmp_path, dollar_bound):
    path, _ = setup_ledger(tmp_path)
    upgrade(path)
    extend(path)
    extend_to_160(path)
    ledger = SpendLedger(path, M.BUDGET_ID, 2, max_attempts=160)
    card = replace(CARD, input_usd_per_million="20000") if dollar_bound else CARD

    def reserve(_):
        try:
            return ledger.reserve(card, 5)
        except SpendLimitExceeded:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(165)))
    summary = ledger.summary()
    assert summary["held_usd"] <= 2
    assert summary["physical_attempts"] <= 160
    assert sum(item is not None for item in results) == (0 if dollar_bound else 157)
