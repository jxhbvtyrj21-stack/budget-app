"""Файли бази під час карантину й перевірка копій для відновлення (DS-6)."""

import hashlib
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from budget.errors import StorageError
from budget.storage import migrations
from budget.storage.backup import backup_database
from budget.storage.database import open_database
from budget.storage.integrity import integrity_check
from budget.storage.migrations import migrate
from budget.storage.recovery import (
    database_files,
    discard_database,
    quarantine_database,
    restore_from_backup,
    set_aside_database,
    verify_backup,
)


@pytest.fixture
def folder(tmp_path):
    # Пробіли, кирилиця й «#» — символи, які ламають наївний URI до SQLite.
    path = tmp_path / "Мої дані #1" / "Budget"
    path.mkdir(parents=True)
    return path


def make_database(path: Path) -> sqlite3.Connection:
    connection = open_database(path)
    migrate(connection)
    connection.execute("UPDATE general_remainder SET balance = 700")
    return connection


def make_backup(folder: Path, name: str = "budget-20261006-120000-daily.db") -> Path:
    connection = make_database(folder / "source.db")
    path = backup_database(connection, folder / "backups" / name)
    connection.close()
    return path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def wal_state(folder: Path) -> tuple[Path, list[Path]]:
    """Знімок «аварійного» стану: база, WAL якої ще не перенесено в основний файл."""
    live = folder / "live.db"
    connection = make_database(live)
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("UPDATE general_remainder SET balance = 4242")
    crash = folder / "budget.db"
    for source, target in zip(database_files(live), database_files(crash), strict=True):
        target.write_bytes(source.read_bytes())
    connection.close()
    return crash, database_files(crash)


# Перевірка копій ----------------------------------------------------------------------------


def test_valid_backup_is_restorable_and_unchanged(folder):
    backup = make_backup(folder)
    before = (digest(backup), backup.stat().st_mtime_ns, sorted(backup.parent.iterdir()))
    assert verify_backup(backup)
    # Файл копії не змінено, поруч не з'явилися -wal/-shm.
    assert (digest(backup), backup.stat().st_mtime_ns, sorted(backup.parent.iterdir())) == before


@pytest.mark.parametrize(
    "content",
    [b"", b"not a database" * 200, b"SQLite format 3\x00" + b"\xff" * 4000],
    ids=["empty", "garbage", "broken-header"],
)
def test_invalid_backup_files_are_rejected(folder, content):
    path = folder / "budget-20261006-120000-daily.db"
    path.write_bytes(content)
    assert not verify_backup(path)
    assert path.read_bytes() == content


def test_truncated_backup_is_rejected(folder):
    backup = make_backup(folder)
    data = backup.read_bytes()
    backup.write_bytes(data[: len(data) // 2])
    assert not verify_backup(backup)


def test_backup_with_unknown_schema_is_rejected(folder, monkeypatch):
    backup = make_backup(folder)
    raw = sqlite3.connect(backup)
    raw.execute(f"PRAGMA user_version = {migrations.LATEST_VERSION + 1}")
    raw.close()
    assert not verify_backup(backup)  # новішу схему застосунок не відкриє
    raw = sqlite3.connect(backup)
    raw.execute("PRAGMA user_version = 0")
    raw.close()
    assert not verify_backup(backup)  # порожня база без схеми застосунку


# Карантин: .db, -wal і -shm разом ------------------------------------------------------------


def test_quarantine_moves_wal_triple_together_and_keeps_data(folder):
    crash, files = wal_state(folder)
    assert all(p.exists() for p in files)
    quarantined = quarantine_database(crash, "20261006-120000")
    assert not any(p.exists() for p in files)
    assert all(p.exists() for p in database_files(quarantined))
    reader = sqlite3.connect(quarantined.as_uri() + "?mode=ro", uri=True)
    assert reader.execute("SELECT balance FROM general_remainder").fetchone() == (4242,)
    reader.close()


def test_quarantine_rolls_back_when_a_side_file_cannot_move(folder, monkeypatch):
    crash, files = wal_state(folder)
    digests = [digest(p) for p in files]
    original = Path.rename

    def rename(self, target):
        if self.name.endswith("-wal"):
            raise PermissionError("файл зайнятий іншим процесом")
        return original(self, target)

    monkeypatch.setattr(Path, "rename", rename)
    with pytest.raises(StorageError):
        quarantine_database(crash, "20261006-120000")
    # Пару не розірвано: усі три файли на місці й незмінні.
    assert [digest(p) for p in files] == digests
    assert sorted(p.name for p in folder.iterdir() if "corrupted" in p.name) == []


def test_quarantine_without_database_is_an_error(folder):
    with pytest.raises(StorageError):
        quarantine_database(folder / "budget.db", "20261006-120000")


def test_set_aside_picks_a_free_name(folder):
    first, _ = wal_state(folder)
    taken = set_aside_database(first, "corrupted-x")
    again, _ = wal_state(folder)
    second = set_aside_database(again, "corrupted-x")
    assert taken != second and second.name.endswith("corrupted-x-2")


@pytest.mark.skipif(sys.platform != "win32", reason="блокування відкритого файлу — лише Windows")
def test_quarantine_of_open_database_fails_cleanly_on_windows(folder):
    path = folder / "budget.db"
    connection = make_database(path)
    try:
        with pytest.raises(StorageError):
            quarantine_database(path, "20261006-120000")
        assert path.exists()
    finally:
        connection.close()


# Видалення тимчасової бази разом із журналом відкату (F12) ---------------------------------


def side_files(path: Path) -> list[Path]:
    """Основний файл, -wal, -shm і -journal — у порядку, в якому їх видаляють."""
    return [*database_files(path), path.with_name(path.name + "-journal")]


def restoring_leftovers(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir() if p.name.startswith("budget.db.restoring"))


def test_discard_removes_database_wal_shm_and_journal(folder):
    database = folder / "ledger.sqlite"  # шлях до журналу — від переданої назви
    for path in side_files(database):
        path.write_bytes(b"x")
    neighbour = folder / "budget.db-journal"  # журнал іншої бази не чіпати
    neighbour.write_bytes(b"x")
    discard_database(database)
    assert not any(p.exists() for p in side_files(database))
    assert neighbour.exists()


def test_failed_restore_leaves_no_orphan_journal(folder):
    """F12: після аварії лишилися ``.restoring`` і ``.restoring-journal``; наступна спроба
    прибирає тимчасову базу й падає, бо копію не відкрити. Журнал не лишається сиротою."""
    (folder / "budget.db.restoring").write_bytes(b"partial copy")
    (folder / "budget.db.restoring-journal").write_bytes(b"x" * 512)
    with pytest.raises(StorageError):
        restore_from_backup(folder / "немає такої копії.db", folder / "budget.db")
    assert restoring_leftovers(folder) == []
    assert not (folder / "budget.db").exists()


def test_journal_stays_when_the_database_cannot_be_removed(folder, monkeypatch):
    database = folder / "budget.db.restoring"
    files = side_files(database)
    for path in files:
        path.write_bytes(b"x")
    attempts = []
    original = Path.unlink

    def unlink(self, missing_ok=False):
        attempts.append(self)
        if self == database:
            raise PermissionError(13, "файл зайнятий", str(self))
        return original(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(PermissionError):
        discard_database(database)
    assert attempts == [database]  # після невдачі з основним файлом — нічого
    assert all(p.exists() for p in files)  # журнал і -wal/-shm на місці


CRASH_WHILE_WRITING = """
import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1], isolation_level=None)
connection.execute("PRAGMA cache_size = 1")
connection.execute("BEGIN IMMEDIATE")
connection.execute("CREATE TABLE junk (x)")
connection.executemany("INSERT INTO junk VALUES (?)", [(os.urandom(900),)] * 300)
os._exit(9)  # аварія посеред транзакції: без COMMIT і без закриття
"""


def test_restore_after_a_crash_with_a_hot_journal(folder):
    """Аварія посеред запису в тимчасову базу лишає справжній журнал SQLite; наступне
    відновлення все одно дає цілу базу з даними копії й без залишків."""
    backup = make_backup(folder)
    temporary = folder / "budget.db.restoring"
    crashed = subprocess.run([sys.executable, "-c", CRASH_WHILE_WRITING, str(temporary)])
    assert crashed.returncode == 9
    assert (folder / "budget.db.restoring-journal").exists()  # передумова: журнал лишився
    restore_from_backup(backup, folder / "budget.db")
    assert restoring_leftovers(folder) == []
    connection = sqlite3.connect(folder / "budget.db")
    try:
        assert integrity_check(connection)
        assert connection.execute("SELECT balance FROM general_remainder").fetchone() == (700,)
    finally:
        connection.close()
