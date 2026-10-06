"""Автоматичні копії під час запуску: після підготовки бази, без дублікатів (DS-4, DS-5, DS-6)."""

import hashlib
import logging
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from budget.app import open_application_database
from budget.domain.calendar import FixedClock
from budget.errors import DatabaseCorruptedError, StorageError
from budget.platform.paths import DataPaths
from budget.services import backup as backup_service_module
from budget.services.backup import BackupKind, RecoveryService, parse_backup_name
from budget.storage import backup as backup_module
from budget.storage import migrations
from budget.storage.migrations import LATEST_VERSION, schema_version

START = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)  # 12:00 за Києвом


@pytest.fixture
def clock():
    return FixedClock(START)


@pytest.fixture
def paths(tmp_path):
    # Пробіли й не-ASCII у шляху — як у профілі користувача Windows.
    return DataPaths(tmp_path / "Local App Data" / "Бюджет")


def start(paths, clock) -> None:
    open_application_database(paths, clock).close()


def backup_names(paths, kind: BackupKind | None = None) -> list[str]:
    if not paths.backups.is_dir():
        return []
    infos = [parse_backup_name(p) for p in sorted(paths.backups.iterdir())]
    return [i.path.name for i in infos if i is not None and (kind is None or i.kind is kind)]


def digest(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_startup_creates_one_backup_per_pool(paths, clock):
    start(paths, clock)
    assert backup_names(paths) == [
        "budget-20261006-120000-daily.db",
        "budget-20261006-120000-monthly.db",
        "budget-20261006-120000-weekly.db",
    ]
    for name in backup_names(paths):
        copy = sqlite3.connect(paths.backups / name)
        assert schema_version(copy) == LATEST_VERSION
        copy.close()


def test_repeated_startup_same_period_creates_no_duplicates(paths, clock):
    for minutes in (0, 1, 30, 600):
        clock.set(START + timedelta(minutes=minutes))
        start(paths, clock)
    assert len(backup_names(paths)) == 3


def test_startup_on_following_days_weeks_and_months(paths, clock):
    start(paths, clock)
    clock.set(START + timedelta(days=1))  # середа — той самий тиждень і місяць
    start(paths, clock)
    assert len(backup_names(paths, BackupKind.DAILY)) == 2
    assert len(backup_names(paths, BackupKind.WEEKLY)) == 1
    clock.set(START + timedelta(days=6))  # понеділок 12 жовтня — новий тиждень ISO
    start(paths, clock)
    assert len(backup_names(paths, BackupKind.WEEKLY)) == 2
    assert len(backup_names(paths, BackupKind.MONTHLY)) == 1
    clock.set(datetime(2026, 10, 31, 22, 30, tzinfo=UTC))  # 1 листопада за Києвом
    start(paths, clock)
    assert len(backup_names(paths, BackupKind.MONTHLY)) == 2


def test_migration_backup_and_automatic_backups_coexist(paths, clock, monkeypatch):
    start(paths, clock)
    extra = (LATEST_VERSION + 1, "CREATE TABLE probe (id INTEGER PRIMARY KEY) STRICT;")
    monkeypatch.setattr(migrations, "MIGRATIONS", (*migrations.MIGRATIONS, extra))
    monkeypatch.setattr(migrations, "LATEST_VERSION", extra[0])
    clock.set(START + timedelta(days=1))
    start(paths, clock)
    migration = backup_names(paths, BackupKind.BEFORE_MIGRATION)
    assert migration == [f"budget-20261007-120000-before-migration-{extra[0]}.db"]
    # Копія перед міграцією — стара схема; щоденна після запуску — уже нова.
    old = sqlite3.connect(paths.backups / migration[0])
    new = sqlite3.connect(paths.backups / "budget-20261007-120000-daily.db")
    assert (schema_version(old), schema_version(new)) == (LATEST_VERSION, extra[0])
    old.close()
    new.close()


def test_ordinary_backup_failure_is_logged_and_startup_continues(paths, clock, monkeypatch, caplog):
    start(paths, clock)
    before = backup_names(paths)

    def disk_full(connection, destination):
        raise StorageError(detail=f"На диску бракує місця: {destination}")

    monkeypatch.setattr(backup_service_module, "backup_database", disk_full)
    clock.set(START + timedelta(days=40))
    with caplog.at_level(logging.ERROR):
        connection = open_application_database(paths, clock)
    # Запуск не зупинено: база працює; ротація не виконувалась, старі копії на місці.
    assert schema_version(connection) == LATEST_VERSION
    connection.close()
    assert backup_names(paths) == before
    assert "Автоматична копія" in caplog.text


def test_source_corruption_goes_to_quarantine_without_writes(paths, clock, monkeypatch):
    start(paths, clock)
    before = backup_names(paths)
    database_digest = digest(paths.database)
    monkeypatch.setattr(backup_module, "integrity_check", lambda connection: False)
    clock.set(START + timedelta(days=1))
    with pytest.raises(DatabaseCorruptedError):
        open_application_database(paths, clock)
    assert backup_names(paths) == before
    assert digest(paths.database) == database_digest  # у пошкоджену базу нічого не записано
    # З'єднання закрито — файл можна перенести в карантин (і на Windows).
    quarantined = RecoveryService(paths.database, clock).quarantine_corrupted()
    assert quarantined.exists() and not paths.database.exists()


def test_backups_stay_in_data_directory(paths, clock):
    start(paths, clock)
    assert paths.backups == paths.root / "backups"
    assert {p.parent for p in paths.backups.iterdir()} == {paths.backups}
    # Копії — окремі файли бази без супутніх -wal/-shm.
    assert not [p for p in paths.backups.iterdir() if p.suffix != ".db"]
