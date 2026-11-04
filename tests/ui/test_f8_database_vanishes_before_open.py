"""F8-S1: робоча база зникає між перевіркою її наявності й відкриттям — регресійний тест.

Справжні ``start_session``, ``startup_recovery_indicators`` і відкриття бази. Вікно
відтворюється детерміновано: обгортка над справжньою перевіркою (база є, ознак немає) одразу
після неї переносить ``budget.db`` у сусідню теку тестового каталогу, як це зробив би
сторонній процес. ``-wal`` лишається на місці й містить зафіксовану зміну, якої немає в
основному файлі. Підмінено лише показ повідомлення (модальне вікно).

Очікувано: базу, що мала існувати, відкривають у режимі ``mode=rw`` — контрольована помилка
запуску, нової порожньої бази немає, ``-wal`` не змінено, дані повертаються разом із файлом.
"""

import logging
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

import budget.app as app_module
from budget.app import EXIT_STARTUP_FAILED, ApplicationSession, start_session
from budget.domain.calendar import FixedClock
from budget.domain.money import Money
from budget.errors import StorageError
from budget.platform.identity import load_product_identity
from budget.platform.paths import DataPaths
from budget.services.backup import find_backups
from budget.services.facade import AppServices
from budget.services.setup import SetupDraft

START = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
IDENTITY = load_product_identity()
CHECKPOINTED_BALANCE = 250_000
WAL_ONLY_BALANCE = 777_777


@pytest.fixture
def clock():
    return FixedClock(START)


@pytest.fixture
def paths(tmp_path):
    return DataPaths(tmp_path / "Мої дані" / "Budget")


@pytest.fixture
def messages(monkeypatch):
    shown = []
    monkeypatch.setattr(app_module, "_show_message", lambda title, text: shown.append(text))
    return shown


def balance(database) -> int | None:
    """Залишок із ``database`` разом із її ``-wal``, якщо він поруч. Викликати лише для
    копій в окремих теках: звичайне закриття переносить WAL в основний файл."""
    connection = sqlite3.connect(database)
    try:
        row = connection.execute("SELECT balance FROM general_remainder").fetchone()
    finally:
        connection.close()
    return None if row is None else row[0]


def copies(tmp_path, name: str, files: dict):
    """Копії файлів в окремій теці ``name`` (``{назва: джерело або байти}``)."""
    folder = tmp_path / name
    folder.mkdir()
    for target, source in files.items():
        if isinstance(source, bytes):
            (folder / target).write_bytes(source)
        else:
            shutil.copy2(source, folder / target)
    return folder / "budget.db"


def make_database_with_wal_only_change(paths, clock):
    """База застосунку, у якій остання зафіксована зміна є лише у ``-wal``: після
    повного checkpoint — UPDATE і закриття без checkpoint (як після аварії)."""
    session = ApplicationSession(paths, clock)
    connection = session.open()
    services = AppServices.create(connection, clock, paths.backups)
    services.setup.complete(SetupDraft(general_remainder=Money(CHECKPOINTED_BALANCE)))
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.execute("UPDATE general_remainder SET balance = ?", (WAL_ONLY_BALANCE,))
    session.close_after_corruption()  # закриття без checkpoint


def test_vanished_database_stops_startup_without_creating_one_and_keeps_its_wal(
    paths, clock, tmp_path, monkeypatch, messages, caplog
):
    make_database_with_wal_only_change(paths, clock)
    wal = paths.database.with_name("budget.db-wal")
    original_wal = wal.read_bytes()
    assert len(original_wal) > 0
    # Передумова: зміна справді лише у WAL (перевірка на копіях в окремих теках).
    pair = {"budget.db": paths.database, "budget.db-wal": wal}
    assert balance(copies(tmp_path, "check-together", pair)) == WAL_ONLY_BALANCE
    alone = {"budget.db": paths.database}
    assert balance(copies(tmp_path, "check-alone", alone)) == CHECKPOINTED_BALANCE

    backups_before = {b.path.name: b.path.read_bytes() for b in find_backups(paths.backups)}
    moved_to = tmp_path / "moved-by-another-process"
    moved_to.mkdir()
    real_indicators = app_module.startup_recovery_indicators
    seen = []

    def indicators_then_database_vanishes(root, database, backups_dir):
        found = real_indicators(root, database, backups_dir)
        seen.append((database.exists(), found))
        shutil.move(database, moved_to / database.name)  # лише основний файл
        return found

    monkeypatch.setattr(
        app_module, "startup_recovery_indicators", indicators_then_database_vanishes
    )
    clock.set(clock.now() + timedelta(days=1))  # новий день: інакше копія й не знадобилася б
    session = ApplicationSession(paths, clock)
    with caplog.at_level(logging.ERROR):
        result = start_session(IDENTITY, session, paths, clock)
    try:
        # 1. Справжня перевірка: база була, ознак немає. 2. Потім файл зник, WAL лишився.
        assert seen == [(True, [])]
        # 6. Контрольована помилка запуску наявним шляхом: журнал і повідомлення.
        assert result == (EXIT_STARTUP_FAILED, False)
        assert not session.is_open
        assert messages == [StorageError.default_message]
        (record,) = [r for r in caplog.records if r.getMessage() == "Startup failed"]
        error = record.exc_info[1]
        assert type(error) is StorageError and str(paths.database) in error.detail
        assert isinstance(error.__cause__, sqlite3.OperationalError)  # першопричина SQLite
    finally:
        session.close()

    # 7. Нової порожньої бази немає. 8. WAL на місці й побайтово той самий.
    assert not paths.database.exists()
    assert wal.read_bytes() == original_wal
    # 10. Копій порожньої бази немає: набір копій і їхній вміст не змінилися.
    backups_after = {b.path.name: b.path.read_bytes() for b in find_backups(paths.backups)}
    assert backups_after == backups_before

    # 9. Основний файл повертається до свого WAL — зміна, що була лише у WAL, на місці.
    shutil.move(moved_to / "budget.db", paths.database)
    restored = copies(tmp_path, "check-returned", pair)
    assert balance(restored) == WAL_ONLY_BALANCE
    # І наступний звичайний запуск відкриває саме цю базу з цією зміною.
    monkeypatch.setattr(app_module, "startup_recovery_indicators", real_indicators)
    session = ApplicationSession(paths, clock)
    try:
        assert start_session(IDENTITY, session, paths, clock) == (None, False)
        value = session.connection.execute("SELECT balance FROM general_remainder").fetchone()
        assert value == (WAL_ONLY_BALANCE,)
    finally:
        session.close()
