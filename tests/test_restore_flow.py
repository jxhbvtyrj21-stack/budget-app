"""Відновлення з копії: заміна файлів, BEFORE_RESTORE, повторне відкриття (DS-5, DS-6)."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from budget.app import open_application_database, restore_and_open
from budget.domain.calendar import FixedClock
from budget.domain.models import SourceKind, SourceRef
from budget.domain.money import Money
from budget.platform.paths import DataPaths
from budget.services.backup import (
    BackupKind,
    BackupService,
    RecoveryService,
    find_backups,
)
from budget.services.facade import AppServices
from budget.services.setup import InitialAccumulation, SetupDraft
from budget.storage import migrations
from budget.storage.integrity import integrity_check
from budget.storage.migrations import LATEST_VERSION, schema_version
from budget.storage.recovery import database_files

START = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)  # 12:00 за Києвом
REMAINDER = SourceRef(SourceKind.GENERAL_REMAINDER)


@pytest.fixture
def clock():
    return FixedClock(START)


@pytest.fixture
def paths(tmp_path):
    # Профіль Windows із пробілами й кирилицею.
    return DataPaths(tmp_path / "Users" / "Олена Коваль" / "AppData Local" / "Budget")


def snapshot(connection: sqlite3.Connection) -> dict[str, list]:
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
    ]
    return {t: connection.execute(f'SELECT * FROM "{t}" ORDER BY 1').fetchall() for t in tables}


def later(clock, **delta) -> None:
    clock.set(clock.now() + timedelta(**delta))


@pytest.fixture
def saved(paths, clock):
    """Налаштований бюджет; копія «на вимогу»; після неї — ще операції лише у WAL.

    Повертає (знімок даних копії, шлях копії). З'єднання закрите.
    """
    connection = open_application_database(paths, clock)
    services = AppServices.create(connection, clock)
    services.setup.complete(
        SetupDraft(
            general_remainder=Money(100_000),
            accumulations=(InitialAccumulation("Подорож", None, Money(20_000)),),
        )
    )
    services.incomes.create("Зарплата", None, Money(40_000))
    later(clock, minutes=1)
    backup = BackupService(connection, paths.backups, clock).create_backup(BackupKind.ON_DEMAND)
    expected = snapshot(connection)
    connection.execute("PRAGMA wal_autocheckpoint=0")
    services.expenses.create("Після копії", None, Money(9_000), REMAINDER)
    assert snapshot(connection) != expected
    connection.close()
    later(clock, minutes=1)
    return expected, backup


def wal_crash_state(paths, clock) -> dict[str, list]:
    """Робоча база з незавершеним перенесенням WAL, як після аварійного завершення."""
    connection = open_application_database(paths, clock)
    connection.execute("PRAGMA wal_autocheckpoint=0")
    AppServices.create(connection, clock).incomes.create("Лише у WAL", None, Money(1_234))
    state = snapshot(connection)
    copies = {p: p.read_bytes() for p in database_files(paths.database)}
    connection.close()
    for path, data in copies.items():
        path.write_bytes(data)
    assert all(p.exists() for p in database_files(paths.database))
    return state


def test_restore_after_quarantine_with_real_wal_state(paths, clock, saved):
    expected, backup = saved
    crashed = wal_crash_state(paths, clock)
    quarantined = RecoveryService(paths.database, paths.backups, clock).quarantine_corrupted()
    assert not any(p.exists() for p in database_files(paths.database))
    later(clock, minutes=1)
    connection = restore_and_open(paths, clock, backup)
    assert snapshot(connection) == expected
    assert integrity_check(connection)
    connection.close()
    # Карантин зберігся разом зі своїм WAL і читається з даними на момент збою.
    reader = sqlite3.connect(quarantined.as_uri() + "?mode=ro", uri=True)
    assert snapshot(reader) == crashed
    reader.close()
    # Робоча база відкривається знову з тими самими даними.
    again = open_application_database(paths, clock)
    assert snapshot(again) == expected
    again.close()


def test_restore_over_healthy_database_creates_before_restore(paths, clock, saved):
    expected, backup = saved
    current = open_application_database(paths, clock)
    current_state = snapshot(current)
    current.close()
    connection = restore_and_open(paths, clock, backup)
    assert snapshot(connection) == expected
    connection.close()
    (before,) = [b for b in find_backups(paths.backups) if b.kind is BackupKind.BEFORE_RESTORE]
    copy = sqlite3.connect(before.path)
    assert integrity_check(copy) and snapshot(copy) == current_state
    copy.close()
    # Замінена база збережена копією, тож її файли прибрано; карантину немає.
    leftovers = [p.name for p in paths.root.iterdir() if p.name.startswith("budget.db.")]
    assert leftovers == []


def test_before_restore_survives_rotation(paths, clock, saved):
    _, backup = saved
    restore_and_open(paths, clock, backup).close()
    (before,) = [b for b in find_backups(paths.backups) if b.kind is BackupKind.BEFORE_RESTORE]
    for day in range(1, 420, 5):
        clock.set(START + timedelta(days=day))
        open_application_database(paths, clock).close()
    assert before.path.exists()
    assert len([b for b in find_backups(paths.backups) if b.kind is BackupKind.DAILY]) == 7


def test_corrupted_current_database_is_kept_not_backed_up(paths, clock, saved):
    expected, backup = saved
    garbage = b"not a database" * 500
    paths.database.write_bytes(garbage)
    connection = restore_and_open(paths, clock, backup)
    assert snapshot(connection) == expected
    connection.close()
    # Пошкоджений стан не видано за копію BEFORE_RESTORE, а збережено як карантин.
    kinds = {b.kind for b in find_backups(paths.backups)}
    assert BackupKind.BEFORE_RESTORE not in kinds
    (kept,) = [p for p in paths.root.iterdir() if ".corrupted-" in p.name]
    assert kept.read_bytes() == garbage


def test_orphaned_wal_files_are_not_mixed_into_restored_database(paths, clock, saved):
    expected, backup = saved
    wal_crash_state(paths, clock)
    paths.database.rename(paths.root / "elsewhere.db")  # лишилися тільки -wal і -shm
    connection = restore_and_open(paths, clock, backup)
    assert snapshot(connection) == expected
    connection.close()
    orphaned = sorted(p.name for p in paths.root.iterdir() if ".orphaned-" in p.name)
    assert [name.rsplit("-", 1)[-1] for name in orphaned] == ["shm", "wal"]


def test_restore_runs_pending_migration_after_backup(paths, clock, saved, monkeypatch):
    expected, backup = saved
    RecoveryService(paths.database, paths.backups, clock).quarantine_corrupted()
    extra = (LATEST_VERSION + 1, "CREATE TABLE probe (id INTEGER PRIMARY KEY) STRICT;")
    monkeypatch.setattr(migrations, "MIGRATIONS", (*migrations.MIGRATIONS, extra))
    monkeypatch.setattr(migrations, "LATEST_VERSION", extra[0])
    connection = restore_and_open(paths, clock, backup)
    assert schema_version(connection) == extra[0]
    restored = snapshot(connection)
    assert restored.pop("probe") == []
    assert restored == expected
    connection.close()
    # Обов'язкова копія перед міграцією зроблена саме з відновлених даних.
    migration = [b for b in find_backups(paths.backups) if b.kind is BackupKind.BEFORE_MIGRATION]
    assert len(migration) == 1
    copy = sqlite3.connect(migration[0].path)
    assert schema_version(copy) == LATEST_VERSION and snapshot(copy) == expected
    copy.close()
