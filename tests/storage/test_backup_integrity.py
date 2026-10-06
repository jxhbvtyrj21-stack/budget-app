"""DS-4: повна перевірка вихідної бази перед копією й перевірка самої копії."""

import sqlite3

import pytest

from budget.errors import DatabaseCorruptedError, StorageError
from budget.storage import backup as backup_module
from budget.storage.backup import backup_database
from budget.storage.transaction import transaction


def checks(results):
    """Підміна integrity_check: перший виклик — вихідна база, другий — копія."""
    calls = iter(results)

    def fake(connection):
        return next(calls)

    return fake


def test_valid_source_is_copied_and_verified(connection, tmp_path):
    with transaction(connection):
        connection.execute("UPDATE general_remainder SET balance = 900")
    copy = backup_database(connection, tmp_path / "backups" / "copy.db")
    restored = sqlite3.connect(copy)
    assert restored.execute("SELECT balance FROM general_remainder").fetchone()[0] == 900
    assert restored.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    restored.close()


def test_corrupted_source_creates_nothing_and_keeps_old_backups(connection, tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    old = backup_database(connection, backups / "old.db")
    before = old.read_bytes()
    monkeypatch.setattr(backup_module, "integrity_check", checks([False]))
    with pytest.raises(DatabaseCorruptedError):
        backup_database(connection, backups / "new.db")
    assert not (backups / "new.db").exists()
    assert old.read_bytes() == before


def test_invalid_copy_is_removed(connection, tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    old = backup_database(connection, backups / "old.db")
    monkeypatch.setattr(backup_module, "integrity_check", checks([True, False]))
    with pytest.raises(StorageError):
        backup_database(connection, backups / "new.db")
    assert not (backups / "new.db").exists() and old.exists()


def test_failed_copy_such_as_full_disk_is_removed(connection, tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    old = backup_database(connection, backups / "old.db")

    class FullDisk:
        """Обгортка з'єднання, чиє backup() падає, як за нестачі місця на диску."""

        def __init__(self, inner):
            self._inner = inner

        def execute(self, *args):
            return self._inner.execute(*args)

        def backup(self, target):
            raise sqlite3.OperationalError("database or disk is full")

    with pytest.raises(StorageError):
        backup_database(FullDisk(connection), backups / "new.db")
    assert not (backups / "new.db").exists() and old.exists()


def test_source_check_is_full_integrity_check(connection, tmp_path, monkeypatch):
    seen = []
    original = backup_module.integrity_check

    def spy(conn):
        seen.append(conn)
        return original(conn)

    monkeypatch.setattr(backup_module, "integrity_check", spy)
    backup_database(connection, tmp_path / "copy.db")
    assert seen[0] is connection and len(seen) == 2
