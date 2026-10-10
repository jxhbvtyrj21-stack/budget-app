"""DS-4: повна перевірка вихідної бази перед копією й перевірка самої копії."""

import json
import logging
import sqlite3
import subprocess
import sys
import textwrap

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


# Атомарне створення копії (B1) -------------------------------------------------------------


def tree(folder) -> set[str]:
    return {p.name for p in folder.iterdir()}


def no_partials(folder) -> bool:
    """Жодного тимчасового файлу копії чи його -wal/-shm/-journal."""
    return not [name for name in tree(folder) if backup_module.PARTIAL_SUFFIX in name]


class FailingBackup:
    """Обгортка з'єднання, чиє backup() падає, як за помилки введення-виведення."""

    def __init__(self, inner):
        self._inner = inner

    def execute(self, *args):
        return self._inner.execute(*args)

    def backup(self, target):
        raise sqlite3.OperationalError("disk I/O error")


def test_successful_copy_is_published_without_temporary_files(connection, tmp_path):
    backups = tmp_path / "backups"
    copy = backup_database(connection, backups / "budget-20261006-120000-daily.db")
    assert tree(backups) == {"budget-20261006-120000-daily.db"}
    check = sqlite3.connect(copy)
    try:
        assert check.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    finally:
        check.close()


@pytest.mark.parametrize("failure", ["copy", "check"])
def test_failed_copy_or_check_publishes_nothing_and_cleans_up(
    connection, tmp_path, monkeypatch, failure
):
    backups = tmp_path / "backups"
    old = backup_database(connection, backups / "old.db")
    source = connection
    if failure == "copy":
        source = FailingBackup(connection)
    else:
        monkeypatch.setattr(backup_module, "integrity_check", checks([True, False]))
    with pytest.raises(StorageError):
        backup_database(source, backups / "new.db")
    assert tree(backups) == {old.name}  # ні копії, ні тимчасових файлів


def test_failed_publication_leaves_no_copy(connection, tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    old = backup_database(connection, backups / "old.db")

    def replace(source, target):
        raise PermissionError(13, "файл зайнятий", str(target))

    monkeypatch.setattr(backup_module, "replace", replace)
    with pytest.raises(StorageError) as raised:
        backup_database(connection, backups / "new.db")
    assert isinstance(raised.value.__cause__, PermissionError)
    assert tree(backups) == {old.name}


def test_name_taken_during_copy_is_not_overwritten(connection, tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    destination = backups / "new.db"
    original = backup_module.integrity_check
    calls = []

    def check(conn):
        calls.append(conn)
        if len(calls) == 2:  # копія вже записана — остаточну назву хтось зайняв
            destination.write_bytes(b"other")
        return original(conn)

    monkeypatch.setattr(backup_module, "integrity_check", check)
    with pytest.raises(StorageError):
        backup_database(connection, destination)
    assert destination.read_bytes() == b"other"
    assert tree(backups) == {"new.db"}


def test_failed_cleanup_keeps_the_primary_error(connection, tmp_path, monkeypatch, caplog):
    backups = tmp_path / "backups"

    def locked(path):
        raise PermissionError(13, "файл зайнятий", str(path))

    monkeypatch.setattr(backup_module, "discard_database", locked)
    with caplog.at_level(logging.WARNING), pytest.raises(StorageError) as raised:
        backup_database(FailingBackup(connection), backups / "new.db")
    assert "disk I/O error" in raised.value.detail
    assert not (backups / "new.db").exists()
    assert any("Не вдалося видалити невдалу копію" in r.getMessage() for r in caplog.records)


CRASH = textwrap.dedent(
    """
    import os, sys
    from pathlib import Path
    import budget.storage.backup as backup
    from budget.storage.database import open_database

    database, destination, stage = sys.argv[1:4]
    if stage == "check":  # аварія під час перевірки вже записаної тимчасової копії
        calls = []
        original = backup.integrity_check

        def check(connection):
            calls.append(connection)
            if len(calls) == 2:
                os._exit(9)
            return original(connection)

        backup.integrity_check = check
    else:  # аварія в момент публікації
        backup.replace = lambda *args: os._exit(9)
    backup.backup_database(open_database(Path(database)), Path(destination))
    """
)


@pytest.mark.parametrize("stage", ["check", "publish"])
def test_crash_leaves_no_copy_under_the_final_name(connection, db_path, tmp_path, stage):
    backups = tmp_path / "backups"
    destination = backups / "budget-20261006-120000-daily.db"
    crashed = subprocess.run([sys.executable, "-c", CRASH, str(db_path), str(destination), stage])
    assert crashed.returncode == 9
    assert not destination.exists()
    assert tree(backups) and not no_partials(backups)  # лишився лише тимчасовий файл
    assert all(".db." in name for name in tree(backups))
    assert backup_database(connection, destination) == destination  # назва не заблокована
    check = sqlite3.connect(destination)
    try:
        assert check.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    finally:
        check.close()


DISK_FULL = textwrap.dedent(
    """
    import json, resource, signal, sys
    from pathlib import Path
    from budget.errors import StorageError
    from budget.storage.backup import backup_database
    from budget.storage.database import open_database

    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    connection = open_database(Path(sys.argv[1]))
    hard = resource.getrlimit(resource.RLIMIT_FSIZE)[1]
    resource.setrlimit(resource.RLIMIT_FSIZE, (100_000, hard))
    try:
        backup_database(connection, Path(sys.argv[2]))
        result = "published"
    except StorageError as error:
        result = type(error).__name__
    print(json.dumps(result))
    """
)


@pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_FSIZE — лише Linux")
def test_real_disk_full_publishes_nothing_and_cleans_up(connection, db_path, tmp_path):
    """Справжня помилка запису (ліміт розміру файлу в окремому процесі на тимчасових файлах)."""
    with transaction(connection):
        connection.execute("CREATE TABLE padding (payload BLOB)")
        connection.executemany("INSERT INTO padding VALUES (randomblob(900))", [()] * 400)
    backups = tmp_path / "backups"
    backups.mkdir()
    destination = backups / "budget-20261006-120000-daily.db"
    child = subprocess.run(
        [sys.executable, "-c", DISK_FULL, str(db_path), str(destination)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(child.stdout) == "StorageError"
    assert tree(backups) == set()  # ні копії, ні тимчасових файлів
