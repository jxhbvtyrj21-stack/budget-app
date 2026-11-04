"""Регресія резервних копій: WAL, незмінність фінансових даних, відновлення (DS-4, DS-5, DS-6)."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from budget.app import open_application_database
from budget.domain.calendar import FixedClock
from budget.domain.models import SourceKind, SourceRef
from budget.domain.money import Money
from budget.platform.paths import DataPaths
from budget.services.backup import BackupKind, BackupService, RecoveryService
from budget.services.facade import AppServices
from budget.services.setup import InitialAccumulation, InitialDebt, SetupDraft
from budget.storage.integrity import integrity_check

START = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
REMAINDER = SourceRef(SourceKind.GENERAL_REMAINDER)


@pytest.fixture
def clock():
    return FixedClock(START)


@pytest.fixture
def paths(tmp_path):
    return DataPaths(tmp_path / "Мої дані" / "Budget")


@pytest.fixture
def populated(paths, clock):
    """Налаштований бюджет з доходом, витратою й боргом; з'єднання з першого запуску."""
    connection = open_application_database(paths, clock)
    services = AppServices.create(connection, clock)
    services.setup.complete(
        SetupDraft(
            general_remainder=Money(100_000),
            accumulations=(InitialAccumulation("Подорож", None, Money(20_000)),),
            debts=(InitialDebt("Позика", None, Money(5_000)),),
        )
    )
    services.incomes.create("Зарплата", None, Money(40_000))
    services.expenses.create("Продукти", None, Money(3_000), REMAINDER)
    yield connection, services
    connection.close()


def snapshot(connection: sqlite3.Connection) -> dict[str, list]:
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
    ]
    return {t: connection.execute(f'SELECT * FROM "{t}" ORDER BY 1').fetchall() for t in tables}


def test_wal_contents_are_in_the_backup(populated, paths, clock):
    """Зміни, ще не перенесені з WAL в основний файл, потрапляють у копію."""
    connection, services = populated
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    services.incomes.create("Премія", None, Money(7_000))
    wal = paths.database.with_name(paths.database.name + "-wal")
    assert wal.exists() and wal.stat().st_size > 0
    path = BackupService(connection, paths.backups, clock).create_backup(BackupKind.ON_DEMAND)
    copy = sqlite3.connect(path)
    assert integrity_check(copy)
    assert snapshot(copy) == snapshot(connection)
    copy.close()


def test_automatic_backups_do_not_change_financial_state(populated, paths, clock):
    connection, services = populated
    before = snapshot(connection)
    totals = services.balances.available_funds()
    for day in range(1, 40):
        clock.set(START + timedelta(days=day))
        BackupService(connection, paths.backups, clock).run_automatic()
    assert snapshot(connection) == before
    assert services.balances.available_funds() == totals
    # Стан місяців (переходи, базовий мінімум) — частина знімка таблиць вище.


def test_restart_reads_same_data_and_keeps_backups(populated, paths, clock):
    connection, _ = populated
    before = snapshot(connection)
    connection.close()
    clock.set(START + timedelta(days=1))
    reopened = open_application_database(paths, clock)
    assert snapshot(reopened) == before
    reopened.close()
    names = sorted(p.name for p in paths.backups.iterdir())
    assert names.count("budget-20261006-120000-daily.db") == 1
    assert "budget-20261007-120000-daily.db" in names


def test_quarantine_then_restore_from_automatic_backup(populated, paths, clock):
    connection, _ = populated
    clock.set(START + timedelta(days=1))
    created = BackupService(connection, paths.backups, clock).run_automatic()
    path = next(p for p in created if p.name.endswith("-daily.db"))
    expected = snapshot(connection)
    connection.close()
    recovery = RecoveryService(paths.database, paths.backups, clock)
    quarantined = recovery.quarantine_corrupted()
    assert quarantined.exists() and not paths.database.exists()
    recovery.restore(path)
    restored = open_application_database(paths, clock)
    assert snapshot(restored) == expected
    restored.close()
