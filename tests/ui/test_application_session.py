"""ApplicationSession — єдиний власник з'єднання з робочою базою (Block C1)."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from PySide6.QtWidgets import QMessageBox

import budget.app as app_module
from budget.app import (
    EXIT_DATA_CORRUPTED,
    EXIT_STARTUP_FAILED,
    ApplicationSession,
    show_main_window,
    start_session,
)
from budget.domain.calendar import FixedClock
from budget.domain.money import Money
from budget.errors import StorageError
from budget.platform.identity import load_product_identity
from budget.platform.paths import DataPaths
from budget.services.backup import BackupKind, BackupService, find_backups
from budget.services.facade import AppServices
from budget.services.month import MonthTransitionService
from budget.services.setup import SetupDraft
from budget.ui.dialogs.recovery_dialog import RecoveryDialog

START = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)


@pytest.fixture
def clock():
    return FixedClock(START)


@pytest.fixture
def paths(tmp_path):
    return DataPaths(tmp_path / "Users" / "Олена Коваль" / "AppData" / "Local" / "Budget")


@pytest.fixture
def session(paths, clock):
    session = ApplicationSession(paths, clock)
    yield session
    session.close()


@pytest.fixture
def connections(monkeypatch):
    """Усі з'єднання SQLite, відкриті під час тесту (і робочі, і короткі службові)."""
    opened: list[sqlite3.Connection] = []
    original = sqlite3.connect

    def connect(*args, **kwargs):
        connection = original(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    return opened


def is_closed(connection: sqlite3.Connection) -> bool:
    try:
        connection.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return True
    return False


def connection_holders(root) -> list[tuple[str, sqlite3.Connection]]:
    """Пари (власник.атрибут, з'єднання) у графі об'єктів ``budget`` від ``root``."""
    holders, seen, stack = [], set(), [root]
    while stack:
        obj = stack.pop()
        if id(obj) in seen or not type(obj).__module__.startswith("budget."):
            continue
        seen.add(id(obj))
        names = list(getattr(obj, "__dict__", {}))
        for cls in type(obj).__mro__:
            names += list(getattr(cls, "__slots__", ()))
        for name in names:
            value = getattr(obj, name, None)
            if isinstance(value, sqlite3.Connection):
                holders.append((f"{type(obj).__name__}.{name}", value))
            elif value is not None:
                stack.append(value)
    return holders


# Відкриття й закриття ----------------------------------------------------------------------


def test_open_gives_one_connection_shared_by_all_services(session, clock, paths):
    connection = session.open()
    assert session.is_open and session.connection is connection
    services = AppServices.create(session.connection, clock, paths.backups)
    holders = connection_holders(services)
    # Кожен сервіс, вкладений сервіс і репозиторій тримає той самий об'єкт.
    owners = {owner.split(".")[0] for owner, _ in holders}
    assert {"SourceLedger", "IncomeRepository", "BackupService"} <= owners
    assert len(holders) > len(services.__slots__)
    assert all(held is connection for _, held in holders)


def test_close_closes_the_connection_and_services_cannot_use_it(session, clock):
    connection = session.open()
    services = AppServices.create(connection, clock)
    session.close()
    assert not session.is_open
    assert is_closed(connection)
    with pytest.raises(sqlite3.ProgrammingError):
        services.setup.is_completed()  # жодного неявного перепідключення
    with pytest.raises(RuntimeError):
        _ = session.connection


def test_close_is_idempotent(session):
    session.close()  # ще не відкрита
    session.open()
    session.close()
    session.close()
    assert not session.is_open


def test_second_open_requires_close(session):
    first = session.open()
    with pytest.raises(RuntimeError):
        session.open()
    with pytest.raises(RuntimeError):
        session.adopt(first)
    assert session.connection is first and not is_closed(first)
    session.close()
    second = session.open()  # після закриття — нове з'єднання, старе закрите
    assert second is not first and is_closed(first)


# Запуск через сесію -----------------------------------------------------------------------


def test_normal_startup_keeps_a_single_live_connection(qtbot, session, paths, clock, connections):
    assert start_session(load_product_identity(), session, paths, clock) == (None, False)
    live = [c for c in connections if not is_closed(c)]
    assert live == [session.connection]
    # Звичайний запуск: автоматичні копії створено, як і раніше.
    assert {b.kind for b in find_backups(paths.backups)} == {
        BackupKind.DAILY,
        BackupKind.WEEKLY,
        BackupKind.MONTHLY,
    }


def test_normal_startup_runs_month_transition(qtbot, session, paths, clock, monkeypatch):
    calls = []
    original = MonthTransitionService.run_on_startup
    monkeypatch.setattr(
        MonthTransitionService,
        "run_on_startup",
        lambda self: calls.append(self) or original(self),
    )
    _, restored = start_session(load_product_identity(), session, paths, clock)
    window = show_main_window(
        load_product_identity(), session.connection, clock, paths.backups, restored=restored
    )
    qtbot.addWidget(window)
    assert len(calls) == 1


def test_startup_failure_leaves_no_open_connection(session, paths, clock, monkeypatch):
    messages = []
    monkeypatch.setattr(app_module, "_show_message", lambda title, text: messages.append(text))

    def failing(paths, clock):
        raise StorageError("Не вдалося відкрити базу.")

    monkeypatch.setattr(app_module, "open_application_database", failing)
    result = start_session(load_product_identity(), session, paths, clock)
    assert result == (EXIT_STARTUP_FAILED, False)
    assert not session.is_open and messages == ["Не вдалося відкрити базу."]


# Відновлення під час запуску (Block B) через сесію ------------------------------------------


@pytest.fixture
def corrupted_with_backup(paths, clock):
    """Справна копія з даними, після неї робоча база пошкоджена."""
    session = ApplicationSession(paths, clock)
    connection = session.open()
    AppServices.create(connection, clock).setup.complete(
        SetupDraft(general_remainder=Money(250_000))
    )
    clock.set(START + timedelta(hours=2))
    BackupService(connection, paths.backups, clock).create_backup(BackupKind.ON_DEMAND)
    session.close()
    paths.database.write_bytes(b"corrupted" * 1000)
    for side in ("-wal", "-shm"):
        paths.database.with_name(paths.database.name + side).unlink(missing_ok=True)
    clock.set(START + timedelta(days=40))


def confirm_restore(monkeypatch):
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    monkeypatch.setattr(
        QMessageBox,
        "clickedButton",
        lambda self: next(
            b
            for b in self.buttons()
            if self.buttonRole(b) == QMessageBox.ButtonRole.DestructiveRole
        ),
    )


def test_startup_restore_hands_the_restored_connection_to_the_session(
    qtbot, session, paths, clock, corrupted_with_backup, monkeypatch, connections
):
    confirm_restore(monkeypatch)
    monkeypatch.setattr(
        RecoveryDialog, "exec", lambda self: self.restore_selected() or self.result()
    )
    returned = []
    original = app_module.restore_and_open
    monkeypatch.setattr(
        app_module,
        "restore_and_open",
        lambda *args: returned.append(original(*args)) or returned[-1],
    )
    transitions = []
    monkeypatch.setattr(
        MonthTransitionService, "run_on_startup", lambda self: transitions.append(1)
    )

    result = start_session(load_product_identity(), session, paths, clock)

    assert result == (None, True)
    # Сесія володіє саме тим з'єднанням, яке повернуло відновлення, — не новим поверх нього.
    assert session.connection is returned[0]
    assert [c for c in connections if not is_closed(c)] == [session.connection]
    balance = session.connection.execute("SELECT balance FROM general_remainder").fetchone()
    assert balance == (250_000,)
    window = show_main_window(
        load_product_identity(), session.connection, clock, paths.backups, restored=True
    )
    qtbot.addWidget(window)
    assert transitions == []  # після відновлення переходу місяця немає (Block B)


def test_startup_recovery_cancelled_leaves_session_closed(
    qtbot, session, paths, clock, corrupted_with_backup, monkeypatch, connections
):
    monkeypatch.setattr(RecoveryDialog, "exec", lambda self: 0)
    result = start_session(load_product_identity(), session, paths, clock)
    assert result == (EXIT_DATA_CORRUPTED, False)
    assert not session.is_open
    assert all(is_closed(c) for c in connections)
