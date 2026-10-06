"""Невдале відновлення: успіх не повідомляється, дані не губляться (DS-6).

У кожному сценарії ``restore_and_open`` піднімає ``RestoreError``, а файли бази
повертаються до стану перед спробою: або попередня база, або її відсутність після
карантину. Вибрана копія й копія BEFORE_RESTORE лишаються на місці.
"""

import errno
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import budget.app as app_module
from budget.app import open_application_database, restore_and_open
from budget.domain.calendar import FixedClock
from budget.domain.money import Money
from budget.errors import DatabaseCorruptedError, StorageError
from budget.platform.paths import DataPaths
from budget.services import backup as backup_service_module
from budget.services.backup import (
    BackupKind,
    BackupService,
    RecoveryService,
    RestoreError,
    find_backups,
)
from budget.services.facade import AppServices
from budget.services.setup import SetupDraft
from budget.storage import migrations
from budget.storage import recovery as recovery_module
from budget.storage.migrations import LATEST_VERSION
from budget.storage.recovery import database_files

START = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)


@pytest.fixture
def clock():
    return FixedClock(START)


@pytest.fixture
def paths(tmp_path):
    return DataPaths(tmp_path / "Мої документи" / "Budget")


def later(clock, **delta) -> None:
    clock.set(clock.now() + timedelta(**delta))


def balance(connection) -> int:
    return connection.execute("SELECT balance FROM general_remainder").fetchone()[0]


@pytest.fixture
def backup(paths, clock):
    """Копія з залишком 1 000; поточна база після неї — залишок 5 000. З'єднання закрите."""
    connection = open_application_database(paths, clock)
    AppServices.create(connection, clock).setup.complete(
        SetupDraft(general_remainder=Money(100_000))
    )
    later(clock, minutes=1)
    path = BackupService(connection, paths.backups, clock).create_backup(BackupKind.ON_DEMAND)
    connection.execute("UPDATE general_remainder SET balance = 500000")
    connection.close()
    later(clock, minutes=1)
    return path


def files_state(paths) -> dict[str, bytes]:
    """Вміст файлів бази поруч із робочою (без копій)."""
    return {p.name: p.read_bytes() for p in sorted(paths.root.iterdir()) if p.is_file()}


def current_balance(paths, clock) -> int:
    connection = open_application_database(paths, clock)
    try:
        return balance(connection)
    finally:
        connection.close()


def quarantined(paths, clock) -> None:
    """Стан після запуску з пошкодженою базою: робочої бази немає."""
    RecoveryService(paths.database, paths.backups, clock).quarantine_corrupted()
    later(clock, minutes=1)


def restore_fails(paths, clock, backup) -> RestoreError:
    with pytest.raises(RestoreError) as caught:
        restore_and_open(paths, clock, backup)
    assert caught.value.user_message  # причина для повідомлення «Помилка»
    assert not paths.database.with_name("budget.db.restoring").exists()
    return caught.value


# 1–3. Успіх, пошкоджена вибрана копія, пошкоджена поточна база ----------------------------


def test_success_is_reported_only_with_open_validated_connection(paths, clock, backup):
    connection = restore_and_open(paths, clock, backup)
    assert balance(connection) == 100_000
    connection.close()


def test_selected_backup_corrupted_after_listing(paths, clock, backup):
    (candidate,) = [
        c
        for c in RecoveryService(paths.database, paths.backups, clock).candidates()
        if c.backup.path == backup
    ]
    backup.write_bytes(backup.read_bytes()[:4096])  # зіпсовано після показу переліку
    before = files_state(paths)
    error = restore_fails(paths, clock, candidate.backup.path)
    assert "Оберіть іншу копію" in error.user_message
    assert files_state(paths) == before
    assert not [b for b in find_backups(paths.backups) if b.kind is BackupKind.BEFORE_RESTORE]
    assert current_balance(paths, clock) == 500_000


# 4. З'єднання не закрито / файл тримає інший процес --------------------------------------


def test_locked_database_file_keeps_current_state(paths, clock, backup, monkeypatch):
    original = Path.rename

    def rename(self, target):
        if self == paths.database:
            raise PermissionError(errno.EACCES, "файл зайнятий іншим процесом")
        return original(self, target)

    monkeypatch.setattr(Path, "rename", rename)
    restore_fails(paths, clock, backup)
    monkeypatch.undo()
    assert current_balance(paths, clock) == 500_000


@pytest.mark.skipif(sys.platform != "win32", reason="блокування відкритого файлу — лише Windows")
def test_open_connection_blocks_restore_on_windows(paths, clock, backup):
    holder = sqlite3.connect(paths.database)
    try:
        restore_fails(paths, clock, backup)
    finally:
        holder.close()
    assert current_balance(paths, clock) == 500_000


# 5. Не вдалося створити BEFORE_RESTORE --------------------------------------------------


def test_failed_before_restore_backup_stops_restore(paths, clock, backup, monkeypatch):
    def disk_full(connection, destination):
        raise StorageError(detail=f"На диску бракує місця: {destination}")

    monkeypatch.setattr(backup_service_module, "backup_database", disk_full)
    before = files_state(paths)
    error = restore_fails(paths, clock, backup)
    assert "Поточні дані не змінено" in error.user_message
    assert files_state(paths) == before
    monkeypatch.undo()
    assert current_balance(paths, clock) == 500_000


# 6–7. Не вдалося замінити .db або перенести -wal/-shm ------------------------------------


def test_failed_replacement_puts_current_database_back(paths, clock, backup, monkeypatch):
    def replace(source, target):
        raise PermissionError(errno.EACCES, "Відмовлено в доступі")

    monkeypatch.setattr(recovery_module.os, "replace", replace)
    restore_fails(paths, clock, backup)
    monkeypatch.undo()
    assert current_balance(paths, clock) == 500_000
    assert not [p for p in paths.root.iterdir() if ".replaced-" in p.name]
    # Копія поточного стану перед спробою лишилася й справна.
    (before,) = [b for b in find_backups(paths.backups) if b.kind is BackupKind.BEFORE_RESTORE]
    assert before.path.exists()


def test_wal_file_that_cannot_move_keeps_database_files_together(paths, clock, backup, monkeypatch):
    """Справну базу копія BEFORE_RESTORE відкриває й закриває, і SQLite переносить WAL у
    основний файл. Неперенесений WAL лишається лише біля пошкодженої бази."""
    connection = open_application_database(paths, clock)
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("UPDATE general_remainder SET balance = 777")
    wal, shm = (p.read_bytes() for p in database_files(paths.database)[1:])
    connection.close()
    paths.database.write_bytes(b"corrupted main file" * 300)
    database_files(paths.database)[1].write_bytes(wal)
    database_files(paths.database)[2].write_bytes(shm)
    before = {p.name: p.read_bytes() for p in database_files(paths.database)}
    original = Path.rename

    def rename(self, target):
        if self.name == "budget.db-wal":
            raise PermissionError(errno.EACCES, "файл зайнятий іншим процесом")
        return original(self, target)

    monkeypatch.setattr(Path, "rename", rename)
    restore_fails(paths, clock, backup)
    # Усі три файли на місці; .db і WAL незмінні — WAL не відірвано, даних не записано.
    # (-shm — лише індекс WAL і стан блокувань SQLite; спроба відкриття оновлює його.)
    after = {p.name: p.read_bytes() for p in database_files(paths.database)}
    assert after.keys() == before.keys()
    assert after["budget.db"] == before["budget.db"]
    assert after["budget.db-wal"] == before["budget.db-wal"]
    assert not [b for b in find_backups(paths.backups) if b.kind is BackupKind.BEFORE_RESTORE]


# 8–9. Відновлена база не проходить перевірку або міграцію -------------------------------


def test_restored_database_failing_startup_check_is_rolled_back(paths, clock, backup, monkeypatch):
    quarantined(paths, clock)

    def corrupted(*args):
        raise DatabaseCorruptedError(detail="quick_check не пройдено")

    monkeypatch.setattr(app_module, "prepare_database", corrupted)
    restore_fails(paths, clock, backup)  # не DatabaseCorruptedError: це невдале відновлення
    assert not any(p.exists() for p in database_files(paths.database))
    assert backup.exists()


def test_restored_database_failing_integrity_check_is_rolled_back(
    paths, clock, backup, monkeypatch
):
    monkeypatch.setattr(backup_service_module, "integrity_check", lambda connection: False)
    restore_fails(paths, clock, backup)
    monkeypatch.undo()
    assert current_balance(paths, clock) == 500_000


def test_failed_migration_after_restore_is_rolled_back(paths, clock, backup, monkeypatch):
    quarantined(paths, clock)
    broken = (LATEST_VERSION + 1, "CREATE TABLE broken (;")
    monkeypatch.setattr(migrations, "MIGRATIONS", (*migrations.MIGRATIONS, broken))
    monkeypatch.setattr(migrations, "LATEST_VERSION", broken[0])
    restore_fails(paths, clock, backup)
    assert not any(p.exists() for p in database_files(paths.database))
    monkeypatch.undo()
    connection = restore_and_open(paths, clock, backup)  # без зламаної міграції — успіх
    assert balance(connection) == 100_000
    connection.close()


# 10. Помилка диска під час відновлення --------------------------------------------------


def test_disk_error_while_writing_restored_file(paths, clock, backup, monkeypatch):
    quarantined(paths, clock)

    def fsync(descriptor):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(recovery_module.os, "fsync", fsync)
    restore_fails(paths, clock, backup)
    assert not any(p.exists() for p in database_files(paths.database))
    assert not [p for p in paths.root.iterdir() if "restoring" in p.name]


# Повторне відновлення -------------------------------------------------------------------


def test_repeated_recovery(paths, clock, backup, monkeypatch):
    quarantined(paths, clock)

    def fsync(descriptor):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(recovery_module.os, "fsync", fsync)
    restore_fails(paths, clock, backup)
    monkeypatch.undo()
    connection = restore_and_open(paths, clock, backup)  # друга спроба — успіх
    connection.execute("UPDATE general_remainder SET balance = 300000")
    connection.close()
    later(clock, days=1)
    # Нове пошкодження наступного дня: карантин і ще одне відновлення.
    paths.database.write_bytes(b"broken again" * 400)
    for side in database_files(paths.database)[1:]:
        side.unlink(missing_ok=True)
    quarantined(paths, clock)
    connection = restore_and_open(paths, clock, backup)
    assert balance(connection) == 100_000
    connection.close()
    kept = [p.name for p in paths.root.iterdir() if ".corrupted-" in p.name]
    assert len([name for name in kept if not name.endswith(("-wal", "-shm"))]) == 2
