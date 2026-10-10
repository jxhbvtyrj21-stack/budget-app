"""Ознаки попередньої бази, коли робочої бази немає (R1).

``startup_recovery_indicators`` лише читає теку даних: нічого не створює й не відкриває.
Помилка читання — не «ознак немає», а ``OSError`` для контрольованого виходу.
"""

import errno
import os
import sqlite3
from datetime import UTC, datetime

import pytest

import budget.services.startup as startup_module
import budget.storage.recovery as recovery_module
from budget.domain.calendar import FixedClock
from budget.errors import StorageError
from budget.services.startup import (
    ORPHANS_NOT_SET_ASIDE_MESSAGE,
    set_aside_orphaned_files,
    startup_recovery_indicators,
)

CLOCK = FixedClock(datetime(2026, 10, 6, 9, 0, tzinfo=UTC))
BACKUP = "budget-20261005-120000-daily.db"


@pytest.fixture
def root(tmp_path):
    folder = tmp_path / "Budget"
    folder.mkdir()
    return folder


def inspect(root):
    return startup_recovery_indicators(root, root / "budget.db", root / "backups")


def tree(path) -> set[str]:
    return {p.relative_to(path).as_posix() for p in path.rglob("*")}


def test_missing_data_directory_is_a_first_run_and_creates_nothing(tmp_path):
    root = tmp_path / "Budget"
    assert inspect(root) == []
    assert not root.exists()  # перевірка не створює ні теки, ні файлів


def test_logs_lock_and_settings_are_not_indicators(root):
    (root / "logs").mkdir()
    (root / "logs" / "budget.log").write_text("x")
    (root / "budget.lock").write_text("1")
    (root / "settings.json").write_text("{}")
    before = tree(root)
    assert inspect(root) == []
    assert tree(root) == before


@pytest.mark.parametrize(
    "name",
    [
        "budget.db.corrupted-20261006-120000",
        "budget.db.corrupted-20261006-120000-2",
        "budget.db.corrupted-20261006-120000-wal",
        "budget.db.replaced-20261006-120000",
        "budget.db.orphaned-20261006-120000-shm",
        "budget.db.restoring",
        "budget.db.restoring-journal",
        "budget.db-wal",
        "budget.db-shm",
    ],
)
def test_each_set_aside_or_leftover_file_is_an_indicator(root, name):
    (root / name).write_bytes(b"x")
    assert inspect(root) == [root / name]


def test_recognised_backup_name_is_an_indicator_without_opening_it(root, monkeypatch):
    backups = root / "backups"
    backups.mkdir()
    (backups / BACKUP).write_bytes(b"not even a database")  # цілісність тут не перевіряється
    connects = []
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: connects.append(a))
    assert inspect(root) == [backups / BACKUP]
    assert connects == []


@pytest.mark.parametrize(
    "name",
    ["budget.dbx", "other.db-wal", "budget.db.bak", "budget.database", "notes.txt", "budget"],
)
def test_unrelated_files_in_root_are_not_indicators(root, name):
    (root / name).write_bytes(b"x")
    assert inspect(root) == []


def test_unrecognised_files_in_backups_are_not_indicators(root):
    backups = root / "backups"
    backups.mkdir()
    (backups / "notes.txt").write_text("x")
    (backups / "budget-foo.db").write_bytes(b"x")
    (backups / "budget-20261005-120000-daily.db").mkdir()  # тека з такою назвою — не копія
    assert inspect(root) == []


def test_existing_database_means_no_indicators_and_no_scan(root, monkeypatch):
    (root / "budget.db").write_bytes(b"x")
    (root / "budget.db.corrupted-20261006-120000").write_bytes(b"old")
    (root / "backups").mkdir()
    (root / "backups" / BACKUP).write_bytes(b"x")
    monkeypatch.setattr(startup_module.os, "scandir", lambda *a: pytest.fail("без читання теки"))
    assert inspect(root) == []


def failing_scandir(monkeypatch, target):
    original = os.scandir

    def scandir(path="."):
        if os.fspath(path) == os.fspath(target):
            raise PermissionError(errno.EACCES, "доступ заборонено", os.fspath(path))
        return original(path)

    monkeypatch.setattr(startup_module.os, "scandir", scandir)


def test_unreadable_data_directory_is_an_error_not_a_first_run(root, monkeypatch):
    failing_scandir(monkeypatch, root)
    with pytest.raises(PermissionError):
        inspect(root)


def test_unreadable_backups_directory_is_an_error_not_no_backups(root, monkeypatch):
    (root / "backups").mkdir()
    failing_scandir(monkeypatch, root / "backups")
    with pytest.raises(PermissionError):
        inspect(root)


def test_backups_path_that_is_not_a_directory_is_an_error(root):
    (root / "backups").write_bytes(b"file")
    with pytest.raises(OSError):
        inspect(root)


# Відкладення залишкових -wal/-shm перед порожнім стартом ------------------------------------


def test_nothing_to_set_aside(root):
    assert set_aside_orphaned_files(root / "budget.db", CLOCK) is None
    assert tree(root) == set()


@pytest.mark.parametrize("sides", [("-wal",), ("-shm",), ("-wal", "-shm")])
def test_leftovers_are_set_aside_byte_for_byte(root, sides):
    content = {side: os.urandom(64) for side in sides}
    for side, data in content.items():
        (root / f"budget.db{side}").write_bytes(data)
    target = set_aside_orphaned_files(root / "budget.db", CLOCK)
    assert target.name == "budget.db.orphaned-20261006-120000"
    for side, data in content.items():
        assert not (root / f"budget.db{side}").exists()
        assert (root / f"{target.name}{side}").read_bytes() == data


def test_existing_database_is_never_set_aside(root):
    (root / "budget.db").write_bytes(b"db")
    (root / "budget.db-wal").write_bytes(b"wal")
    assert set_aside_orphaned_files(root / "budget.db", CLOCK) is None
    assert tree(root) == {"budget.db", "budget.db-wal"}


def test_failed_set_aside_is_a_storage_error_and_keeps_files(root, monkeypatch):
    (root / "budget.db-wal").write_bytes(b"wal")
    (root / "budget.db-shm").write_bytes(b"shm")

    def locked(source, target):
        raise StorageError(detail="файл зайнятий іншим процесом")

    monkeypatch.setattr(recovery_module, "move_database_files", locked)
    with pytest.raises(StorageError) as raised:
        set_aside_orphaned_files(root / "budget.db", CLOCK)
    assert raised.value.user_message == ORPHANS_NOT_SET_ASIDE_MESSAGE
    assert tree(root) == {"budget.db-wal", "budget.db-shm"}
