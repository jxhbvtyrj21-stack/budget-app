"""Закриття пошкодженої бази без checkpoint WAL (H1).

Звичайна конфігурація застосунку (WAL, автоматичний checkpoint), без жодних перемикачів
у тестах: зафіксовані кадри лежать лише у WAL, основний файл пошкоджено. Звичайне
закриття переносить кадри в пошкоджений файл; закриття після пошкодження — ні.
"""

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from budget.domain.calendar import FixedClock
from budget.errors import DatabaseCorruptedError
from budget.storage.database import close_without_checkpoint, open_database
from budget.storage.migrations import migrate
from budget.storage.recovery import database_files, quarantine_database


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def corrupt_pages(path: Path) -> None:
    pages = path.stat().st_size // 4096
    with path.open("r+b") as handle:
        for page in range(2, pages):
            handle.seek(page * 4096)
            handle.write(b"\xa5" * 4096)


def live_corrupted(path: Path):
    """Відкрите з'єднання: кадри лише у WAL, основний файл пошкоджено."""
    connection = open_database(path)
    migrate(connection)
    connection.execute("CREATE TABLE padding (payload TEXT)")
    connection.executemany("INSERT INTO padding VALUES (?)", [("x" * 400,)] * 200)
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.execute("UPDATE general_remainder SET balance = 4242")  # лише у WAL
    corrupt_pages(path)
    files = database_files(path)
    assert all(p.exists() for p in files) and files[1].stat().st_size > 0
    return connection, {p: digest(p) for p in files[:2]}


def test_ordinary_close_writes_wal_into_the_corrupted_file(tmp_path):
    """Контроль: саме від цього захищає закриття без checkpoint."""
    path = tmp_path / "budget.db"
    connection, before = live_corrupted(path)
    connection.close()
    assert digest(path) != before[path]
    assert not database_files(path)[1].exists()


def test_close_without_checkpoint_leaves_db_and_wal_untouched(tmp_path):
    path = tmp_path / "budget.db"
    connection, before = live_corrupted(path)
    close_without_checkpoint(connection)
    files = database_files(path)
    assert all(p.exists() for p in files)  # -wal і -shm не «поглинуто»
    assert {p: digest(p) for p in files[:2]} == before  # жодного запису в .db і WAL
    # Файли вільні: усі три переносяться в карантин разом і без змін.
    kept = quarantine_database(path, "20261006-120000")
    assert not any(p.exists() for p in files)
    kept_files = database_files(kept)
    assert all(p.exists() for p in kept_files)
    assert [digest(p) for p in kept_files[:2]] == [before[files[0]], before[files[1]]]


def test_corruption_found_on_open_is_closed_without_checkpoint(tmp_path):
    """``open_database``/``quick_check`` при запуску: той самий спосіб закриття."""
    from budget.services.startup import prepare_database

    path = tmp_path / "budget.db"
    connection, _ = live_corrupted(path)
    # Аварійний стан, як після збою процесу: .db (пошкоджений), WAL і SHM на диску.
    crash = {p: p.read_bytes() for p in database_files(path)}
    connection.close()
    for file, data in crash.items():
        file.write_bytes(data)
    before = {p: digest(p) for p in database_files(path)[:2]}
    with pytest.raises(DatabaseCorruptedError):
        prepare_database(path, tmp_path / "backups", FixedClock(datetime(2026, 10, 6, tzinfo=UTC)))
    files = database_files(path)
    assert all(p.exists() for p in files)
    assert {p: digest(p) for p in files[:2]} == before
    # Далі — карантин запуску: усі три файли разом і без змін.
    kept = quarantine_database(path, "20261006-120000")
    assert not any(p.exists() for p in files)
    assert [digest(p) for p in database_files(kept)[:2]] == [before[files[0]], before[files[1]]]
