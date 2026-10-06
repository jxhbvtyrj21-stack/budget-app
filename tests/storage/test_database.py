import sqlite3

import pytest

from budget.errors import DatabaseCorruptedError, StorageError
from budget.storage.backup import backup_database
from budget.storage.database import open_database
from budget.storage.integrity import integrity_check, quick_check
from budget.storage.migrations import LATEST_VERSION, migrate, schema_version
from budget.storage.recovery import quarantine_database, restore_from_backup
from budget.storage.transaction import transaction

EXPECTED_TABLES = {
    "setup_state",
    "general_remainder",
    "incomes",
    "accumulations",
    "expenses",
    "replenishments",
    "replenishment_parts",
    "debts",
    "debt_repayments",
    "base_minimums",
}
REJECTED_ENTITIES = (
    "period",
    "month_state",
    "account",
    "wallet",
    "card",
    "currenc",
    "goal",
    "cancel",
    "correction",
    "restore",
    "transfer",
)


def table_names(connection):
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    return {name for (name,) in rows}


def test_wal_and_foreign_keys(connection):
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL


def test_migration_on_clean_database(connection):
    assert schema_version(connection) == LATEST_VERSION
    assert table_names(connection) == EXPECTED_TABLES
    assert migrate(connection) == []


def test_all_tables_are_strict(connection):
    for name in EXPECTED_TABLES:
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()[0]
        assert sql.rstrip().endswith("STRICT"), name


def test_no_rejected_entities(connection):
    for name in table_names(connection):
        assert not any(word in name for word in REJECTED_ENTITIES), name


def test_seed_rows(connection):
    assert connection.execute("SELECT status FROM setup_state").fetchall() == [("not_started",)]
    assert connection.execute("SELECT balance FROM general_remainder").fetchall() == [(0,)]


def test_constraints_reject_invalid_rows(connection):
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO incomes (month, name, amount) VALUES ('2026-13', 'Дохід', 100)"
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO incomes (month, name, amount) VALUES ('2026-10', ' ', 100)")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO accumulations (name, status, archived) VALUES ('Подорож', 'active', 1)"
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO expenses (month, name, amount, source_kind, source_income_id)"
            " VALUES ('2026-10', 'Кава', 100, 'income', 999)"
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO debts (origin, month, name, amount) VALUES ('initial', '2026-10', 'X', 1)"
        )


def test_transaction_rolls_back_on_error(connection):
    with pytest.raises(RuntimeError), transaction(connection):
        connection.execute("UPDATE general_remainder SET balance = 500")
        raise RuntimeError
    assert connection.execute("SELECT balance FROM general_remainder").fetchone()[0] == 0
    with transaction(connection):
        connection.execute("UPDATE general_remainder SET balance = 500")
    assert connection.execute("SELECT balance FROM general_remainder").fetchone()[0] == 500


def test_integrity_checks(connection):
    assert quick_check(connection)
    assert integrity_check(connection)


def test_backup_and_restore(connection, tmp_path):
    with transaction(connection):
        connection.execute("UPDATE general_remainder SET balance = 700")
    backup = backup_database(connection, tmp_path / "backups" / "copy.db")
    with pytest.raises(StorageError):
        backup_database(connection, backup)
    target = tmp_path / "restored.db"
    restore_from_backup(backup, target)
    restored = open_database(target)
    assert restored.execute("SELECT balance FROM general_remainder").fetchone()[0] == 700
    restored.close()


def test_corrupted_database_detected_and_quarantined(tmp_path):
    path = tmp_path / "budget.db"
    path.write_bytes(b"this is not a sqlite database" * 100)
    with pytest.raises(DatabaseCorruptedError):
        open_database(path)
    quarantined = quarantine_database(path, "20261006-120000")
    assert not path.exists()
    assert quarantined.read_bytes().startswith(b"this is not")


def test_newer_schema_is_rejected(connection):
    connection.execute(f"PRAGMA user_version = {LATEST_VERSION + 1}")
    with pytest.raises(StorageError):
        migrate(connection)
