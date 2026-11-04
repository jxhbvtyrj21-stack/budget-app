"""Центральне розпізнавання пошкодження бази під час роботи (Block C3).

Відновлення ще не виконується: перевіряється лише розпізнавання, один запис у журнал,
планування точки входу відновлення й відсутність хибних спрацювань.
"""

import logging
import sqlite3
import sys
from datetime import UTC, datetime

import pytest
from PySide6.QtCore import QCoreApplication, QTimer
from PySide6.QtWidgets import QPushButton

import budget.app as app_module
from budget.app import (
    ApplicationSession,
    RuntimeCorruptionGuard,
    show_window_or_report,
)
from budget.domain.calendar import FixedClock
from budget.domain.money import Money
from budget.platform.identity import load_product_identity
from budget.platform.paths import DataPaths
from budget.services.facade import AppServices
from budget.services.setup import SetupDraft
from budget.ui.main_window import MainWindow

CLOCK = FixedClock(datetime(2026, 10, 6, 9, 0, tzinfo=UTC))


def with_code(cls, code: int, message: str = "") -> sqlite3.Error:
    error = cls(message)
    error.sqlite_errorcode = code
    return error


@pytest.fixture
def previous_hook(monkeypatch):
    """Попередній ``sys.excepthook`` — записувач замість типового друку."""
    calls = []
    monkeypatch.setattr(sys, "excepthook", lambda *args: calls.append(args[1]))
    return calls


@pytest.fixture
def recoveries():
    return []


@pytest.fixture
def guard(recoveries):
    return RuntimeCorruptionGuard(recoveries.append)


def process_events() -> None:
    QCoreApplication.processEvents()


def deliver(error: BaseException) -> None:
    """Неперехоплений виняток, як його передає інтерпретатор у ``sys.excepthook``."""
    sys.excepthook(type(error), error, error.__traceback__)


# Обробник ----------------------------------------------------------------------------------


def test_corruption_is_scheduled_not_handled_synchronously(
    qapp, guard, recoveries, previous_hook, caplog
):
    error = with_code(sqlite3.DatabaseError, sqlite3.SQLITE_CORRUPT)
    with caplog.at_level(logging.INFO), guard:
        deliver(error)
        assert recoveries == []  # не синхронно, всередині слоту
        assert guard.detected
        process_events()
    assert recoveries == [error]
    assert previous_hook == []  # звичайний шлях друку винятку не виконується
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "sqlite code 11" in errors[0].getMessage()


@pytest.mark.parametrize(
    "error",
    [
        with_code(sqlite3.OperationalError, sqlite3.SQLITE_ERROR),
        with_code(sqlite3.OperationalError, sqlite3.SQLITE_BUSY, "database is locked"),
        with_code(sqlite3.OperationalError, sqlite3.SQLITE_READONLY),
        with_code(sqlite3.OperationalError, sqlite3.SQLITE_IOERR_WRITE),
        with_code(sqlite3.IntegrityError, sqlite3.SQLITE_CONSTRAINT_UNIQUE),
        sqlite3.DatabaseError("database disk image is malformed"),  # без коду
        ValueError("bad value"),
        RuntimeError("programming error"),
    ],
    ids=lambda e: f"{type(e).__name__}-{getattr(e, 'sqlite_errorcode', None)}",
)
def test_everything_else_goes_to_the_previous_hook(qapp, guard, recoveries, previous_hook, error):
    with guard:
        deliver(error)
        process_events()
    assert previous_hook == [error]
    assert recoveries == [] and not guard.detected


def test_repeated_corruption_schedules_one_recovery(qapp, guard, recoveries, previous_hook, caplog):
    first = with_code(sqlite3.DatabaseError, sqlite3.SQLITE_CORRUPT)
    second = with_code(sqlite3.DatabaseError, sqlite3.SQLITE_NOTADB)
    with caplog.at_level(logging.INFO), guard:
        deliver(first)
        deliver(second)
        process_events()
        deliver(with_code(sqlite3.DatabaseError, sqlite3.SQLITE_CORRUPT_INDEX))
        process_events()
    assert recoveries == [first]
    assert previous_hook == []
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1


def test_hook_is_installed_only_for_the_lifecycle(guard, previous_hook):
    before = sys.excepthook
    with guard:
        assert sys.excepthook == guard._excepthook
    assert sys.excepthook is before
    # Повторне використання й вихід без втручання не ламають чужий hook.
    with guard:
        replacement = lambda *args: None  # noqa: E731
        sys.excepthook = replacement
    assert sys.excepthook is replacement


def test_hook_is_not_installed_on_import():
    import subprocess

    check = "import sys; h = sys.excepthook; import budget.app; print(sys.excepthook is h)"
    result = subprocess.run([sys.executable, "-c", check], capture_output=True, text=True)
    assert result.stdout.strip() == "True", result.stderr


# Справжній сценарій Qt -----------------------------------------------------------------------


def corrupt_pages(path) -> None:
    pages = path.stat().st_size // 4096
    with path.open("r+b") as handle:
        for page in range(2, pages):
            handle.seek(page * 4096)
            handle.write(b"\xa5" * 4096)


@pytest.fixture
def running(tmp_path, qtbot):
    """Застосунок працює: сесія, сервіси й головне вікно на справжній базі."""
    paths = DataPaths(tmp_path / "Мої дані" / "Budget")
    session = ApplicationSession(paths, CLOCK)
    connection = session.open()
    services = AppServices.create(connection, CLOCK, paths.backups)
    services.setup.complete(SetupDraft(general_remainder=Money(100_000)))
    for index in range(150):
        services.incomes.create(f"Дохід {index}", "опис " * 40, Money(100))
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.execute("PRAGMA cache_size = 0")
    window = MainWindow("Budget", services)
    qtbot.addWidget(window)
    yield paths, session, services, window
    window.close()
    session.close()


def test_slot_corruption_reaches_the_guard_through_qt(
    qtbot, running, guard, recoveries, previous_hook
):
    paths, session, _, window = running
    corrupt_pages(paths.database)
    with guard:
        # Звичайний слот застосунку (оновлення екранів) читає пошкоджену базу.
        QTimer.singleShot(0, window.refresh)
        qtbot.waitUntil(lambda: bool(recoveries), timeout=2000)
        process_events()
    assert len(recoveries) == 1
    assert recoveries[0].sqlite_errorcode & 0xFF == sqlite3.SQLITE_CORRUPT
    assert previous_hook == []
    assert session.is_open  # закриття бази — справа відновлення (C5), не розпізнавання


def test_ordinary_error_in_a_qt_slot_is_not_recovery(qtbot, guard, recoveries, previous_hook):
    button = QPushButton()
    qtbot.addWidget(button)

    def busy():
        raise with_code(sqlite3.OperationalError, sqlite3.SQLITE_BUSY, "database is locked")

    button.clicked.connect(busy)
    with guard:
        button.click()
        process_events()
    assert recoveries == [] and len(previous_hook) == 1


def test_suspend_disables_all_windows(qtbot, running):
    _, _, _, window = running
    window.show()
    app_module._suspend_ui_for_recovery(with_code(sqlite3.DatabaseError, sqlite3.SQLITE_CORRUPT))
    assert not window.isEnabled()


# До запуску циклу подій ----------------------------------------------------------------------


def test_corruption_before_event_loop_is_a_controlled_status(
    qtbot, running, monkeypatch, guard, recoveries, caplog
):
    paths, session, _, _ = running

    def corrupted(*args, **kwargs):
        raise with_code(sqlite3.DatabaseError, sqlite3.SQLITE_CORRUPT)

    monkeypatch.setattr(app_module, "show_main_window", corrupted)
    with caplog.at_level(logging.ERROR):
        window = show_window_or_report(load_product_identity(), session, CLOCK, paths, False, guard)
    process_events()
    assert window is None and guard.detected
    assert recoveries == []  # цикл подій ще не працює — нічого не заплановано
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1


def test_ordinary_error_before_event_loop_is_not_hidden(running, monkeypatch, guard):
    paths, session, _, _ = running

    def busy(*args, **kwargs):
        raise with_code(sqlite3.OperationalError, sqlite3.SQLITE_BUSY)

    monkeypatch.setattr(app_module, "show_main_window", busy)
    with pytest.raises(sqlite3.OperationalError):
        show_window_or_report(load_product_identity(), session, CLOCK, paths, False, guard)
    assert not guard.detected
