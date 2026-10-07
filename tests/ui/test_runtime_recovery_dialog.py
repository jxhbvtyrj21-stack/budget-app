"""Відновлення після пошкодження під час роботи: підготовка до відновлення (Blocks C4, C5.1).

Справжній цикл подій Qt і справжній ``RecoveryDialog``: пошкодження з реального слоту →
C3 → вікна неактивні → ``session.close()`` → карантин ``.db``/``-wal``/``-shm`` → діалог
з перевіреними копіями → вибір або скасування (``EXIT_DATA_CORRUPTED``). Самого
відновлення (C5.2) ще немає.
"""

import errno
import logging
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

import budget.app as app_module
from budget.app import (
    EXIT_DATA_CORRUPTED,
    QUARANTINE_FAILED_MESSAGE,
    ApplicationSession,
    RuntimeCorruptionGuard,
    RuntimeRecoveryFlow,
)
from budget.domain.calendar import FixedClock
from budget.domain.money import Money
from budget.errors import StorageError
from budget.platform.paths import DataPaths
from budget.services import backup as backup_service_module
from budget.services.backup import (
    BackupKind,
    RecoveryService,
    find_backups,
    restore_candidates,
)
from budget.services.facade import AppServices
from budget.services.setup import SetupDraft
from budget.storage.recovery import database_files
from budget.ui.dialogs.recovery_dialog import (
    RUNTIME_EXPLANATION,
    STARTUP_EXPLANATION,
    RecoveryDialog,
    candidate_text,
)
from budget.ui.main_window import MainWindow

START = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
CLOCK = FixedClock(START)


@pytest.fixture
def running(tmp_path, qtbot, monkeypatch):
    """Застосунок працює на справжній базі з копіями; пошкоджені й чужі файли поруч."""
    clock = FixedClock(START)
    paths = DataPaths(tmp_path / "Users" / "Олена Коваль" / "AppData" / "Local" / "Budget")
    session = ApplicationSession(paths, clock)
    connection = session.open()  # щоденна, щотижнева, щомісячна
    services = AppServices.create(connection, clock, paths.backups)
    services.setup.complete(SetupDraft(general_remainder=Money(100_000)))
    for index in range(150):
        services.incomes.create(f"Дохід {index}", "опис " * 40, Money(100))
    clock.set(START + timedelta(hours=2))
    services.backups.create_backup(BackupKind.ON_DEMAND)
    (paths.backups / "budget-20261005-120000-daily.db").write_bytes(b"broken" * 500)
    (paths.backups / "budget-20261004-120000-manual.db").write_bytes(b"unknown kind")
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.execute("PRAGMA cache_size = 0")
    window = MainWindow("Budget", services)
    qtbot.addWidget(window)
    window.show()
    # Відновлення, заміна сервісів чи нове з'єднання — поза межами C5.1.
    forbidden = []
    monkeypatch.setattr(app_module, "restore_and_open", lambda *a: forbidden.append("restore"))
    monkeypatch.setattr(MainWindow, "replace_services", lambda *a: forbidden.append("replace"))
    monkeypatch.setattr(ApplicationSession, "open", lambda *a: forbidden.append("open"))
    monkeypatch.setattr(ApplicationSession, "adopt", lambda *a: forbidden.append("adopt"))
    yield paths, session, connection, window, forbidden, services
    window.close()
    session.close()


@pytest.fixture
def previous_hook(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "excepthook", lambda *args: calls.append(args[1]))
    return calls


class Recorder:
    """Що оркестратор передав застосунку: вибір, коди виходу, повідомлення."""

    def __init__(self) -> None:
        self.choices = []
        self.exits = []
        self.errors = []

    @property
    def finished(self) -> bool:
        return bool(self.choices or self.exits)


def make_flow(session, paths, recorder) -> RuntimeRecoveryFlow:
    return RuntimeRecoveryFlow(
        session,
        RecoveryService(paths.database, paths.backups, CLOCK),
        recorder.choices.append,
        exit_application=recorder.exits.append,
        show_error=recorder.errors.append,
    )


def corrupt_pages(path) -> None:
    pages = path.stat().st_size // 4096
    with path.open("r+b") as handle:
        for page in range(2, pages):
            handle.seek(page * 4096)
            handle.write(b"\xa5" * 4096)


def confirm(monkeypatch, confirmed: bool) -> None:
    role = (
        QMessageBox.ButtonRole.DestructiveRole if confirmed else QMessageBox.ButtonRole.RejectRole
    )
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    monkeypatch.setattr(
        QMessageBox,
        "clickedButton",
        lambda self: next(b for b in self.buttons() if self.buttonRole(b) == role),
    )


def visible_recovery_dialogs() -> list[RecoveryDialog]:
    """Видимі діалоги відновлення (без діалогів попередніх тестів, що чекають видалення)."""
    return [
        widget
        for widget in QApplication.topLevelWidgets()
        if isinstance(widget, RecoveryDialog) and widget.isVisible()
    ]


def drain_qt_events(qtbot) -> None:
    """Обробити всі вже заплановані події Qt: сигнальний таймер з нульовою затримкою
    спрацьовує після таймерів, запланованих раніше, — очікування умови, а не часу."""
    reached = []
    QTimer.singleShot(0, lambda: reached.append(True))
    qtbot.waitUntil(lambda: bool(reached), timeout=5000)


def when_dialog_opens(action, seen: list) -> None:
    """Дочекатися справжнього модального діалогу в циклі подій і діяти як користувач."""

    def poll():
        dialog = QApplication.activeModalWidget()
        if isinstance(dialog, RecoveryDialog):
            seen.append(dialog)
            action(dialog)
        else:
            QTimer.singleShot(10, poll)

    QTimer.singleShot(10, poll)


def run_runtime_recovery(qtbot, flow, window, recorder, action=None, seen=None):
    if action is not None:
        when_dialog_opens(action, seen if seen is not None else [])
    with RuntimeCorruptionGuard(flow):
        QTimer.singleShot(0, window.refresh)  # звичайний слот читає пошкоджену базу
        qtbot.waitUntil(lambda: recorder.finished, timeout=5000)


def cancel(dialog) -> None:
    dialog.close_button.click()


def dialog_texts(dialog) -> list[str]:
    return [label.text() for label in dialog.findChildren(type(dialog.empty))]


# Порядок і карантин -------------------------------------------------------------------------


def test_session_is_closed_before_quarantine_and_dialog_comes_last(
    qtbot, running, monkeypatch, previous_hook
):
    paths, session, connection, window, forbidden, _ = running
    corrupt_pages(paths.database)
    events = []
    original_close = ApplicationSession.close
    original_quarantine = RecoveryService.quarantine_corrupted

    def close(self):
        events.append(("close", self.is_open))
        original_close(self)

    def quarantine(self):
        events.append(("quarantine", session.is_open, paths.database.exists()))
        return original_quarantine(self)

    monkeypatch.setattr(ApplicationSession, "close", close)
    monkeypatch.setattr(RecoveryService, "quarantine_corrupted", quarantine)

    def note_dialog(dialog):
        events.append(("dialog", session.is_open, paths.database.exists()))
        cancel(dialog)

    recorder = Recorder()
    run_runtime_recovery(qtbot, make_flow(session, paths, recorder), window, recorder, note_dialog)
    assert events == [
        ("close", True),  # спершу закрито живе з'єднання
        ("quarantine", False, True),  # потім карантин, коли база ще на місці
        ("dialog", False, False),  # і лише тоді діалог — бази на місці вже немає
    ]
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")  # старий граф працювати з базою вже не може
    assert forbidden == []


def test_quarantine_moves_db_wal_and_shm_together(qtbot, running, monkeypatch, previous_hook):
    paths, session, connection, window, forbidden, _ = running
    # Справжній стан WAL: закриття не переносить WAL в основний файл, тож лишаються всі три.
    connection.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
    connection.execute("UPDATE general_remainder SET balance = 4242")
    corrupt_pages(paths.database)
    wal_before, dialog_seen = [], []
    original_quarantine = RecoveryService.quarantine_corrupted

    def capture_wal(self):
        dialog_seen.append(all(p.exists() for p in database_files(paths.database)))
        wal_before.append(database_files(paths.database)[1].read_bytes())
        return original_quarantine(self)

    def remember_and_cancel(dialog):
        dialog_seen.append(dialog_texts(dialog))
        cancel(dialog)

    monkeypatch.setattr(RecoveryService, "quarantine_corrupted", capture_wal)
    recorder = Recorder()
    run_runtime_recovery(
        qtbot, make_flow(session, paths, recorder), window, recorder, remember_and_cancel
    )
    (kept,) = [
        p
        for p in paths.root.iterdir()
        if ".corrupted-" in p.name and not p.name.endswith(("-wal", "-shm"))
    ]
    assert dialog_seen[0] is True  # у момент карантину існували всі три файли
    assert not any(p.exists() for p in database_files(paths.database))
    assert all(p.exists() for p in database_files(kept))
    assert database_files(kept)[1].read_bytes() == wal_before[0]  # WAL не відірвано
    assert RUNTIME_EXPLANATION.format(name=kept.name) in dialog_seen[1]
    assert forbidden == []


# Діалог: вибір і скасування -----------------------------------------------------------------


def test_selected_candidate_is_handed_over_without_restoring(
    qtbot, running, monkeypatch, previous_hook
):
    paths, session, _, window, forbidden, _ = running
    before = sorted(p.name for p in paths.backups.iterdir())
    corrupt_pages(paths.database)
    confirm(monkeypatch, True)
    shown = {}

    def choose_second(dialog):
        shown["texts"] = [dialog.list.item(i).text() for i in range(dialog.list.count())]
        shown["candidates"] = [
            dialog.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(dialog.list.count())
        ]
        shown["windows_enabled"] = window.isEnabled()
        dialog.list.setCurrentRow(1)
        dialog.restore_selected()

    recorder, seen = Recorder(), []
    run_runtime_recovery(
        qtbot, make_flow(session, paths, recorder), window, recorder, choose_second, seen
    )
    # Лише перевірені копії, від найновішої; пошкоджена й нерозпізнана не показуються.
    expected = restore_candidates(paths.backups)
    assert shown["texts"] == [candidate_text(c) for c in expected]
    assert expected[0].backup.kind is BackupKind.ON_DEMAND
    assert shown["windows_enabled"] is False
    (choice,) = recorder.choices
    assert not choice.cancelled and choice.candidate is shown["candidates"][1]
    assert recorder.exits == [] and len(seen) == 1 and previous_hook == []
    # Відновлення ще немає: копії не змінені, BEFORE_RESTORE немає, база в карантині.
    assert forbidden == [] and not session.is_open
    assert sorted(p.name for p in paths.backups.iterdir()) == before
    assert BackupKind.BEFORE_RESTORE not in {b.kind for b in find_backups(paths.backups)}
    assert not paths.database.exists()
    assert not window.isEnabled()


def test_cancel_exits_with_data_corrupted(qtbot, running, previous_hook):
    paths, session, _, window, forbidden, _ = running
    before = sorted(p.name for p in paths.backups.iterdir())
    corrupt_pages(paths.database)
    recorder = Recorder()
    run_runtime_recovery(qtbot, make_flow(session, paths, recorder), window, recorder, cancel)
    assert recorder.exits == [EXIT_DATA_CORRUPTED]
    assert recorder.choices == [] and recorder.errors == []
    assert not window.isEnabled()  # звичайна робота не продовжується
    assert visible_recovery_dialogs() == []
    assert not session.is_open and not paths.database.exists()
    assert [p for p in paths.root.iterdir() if ".corrupted-" in p.name]
    assert sorted(p.name for p in paths.backups.iterdir()) == before
    assert forbidden == []


def test_declined_confirmation_keeps_the_dialog_open(qtbot, running, monkeypatch, previous_hook):
    paths, session, _, window, forbidden, _ = running
    corrupt_pages(paths.database)
    confirm(monkeypatch, False)
    states = []

    def decline_then_cancel(dialog):
        dialog.restore_selected()  # підтвердження відхилено
        states.append(dialog.isVisible())
        cancel(dialog)

    recorder = Recorder()
    run_runtime_recovery(
        qtbot, make_flow(session, paths, recorder), window, recorder, decline_then_cancel
    )
    assert states == [True] and recorder.exits == [EXIT_DATA_CORRUPTED] and forbidden == []


# Невдалий карантин --------------------------------------------------------------------------


def test_quarantine_failure_shows_error_and_exits_without_dialog(
    qtbot, running, monkeypatch, previous_hook, caplog
):
    paths, session, _, window, forbidden, _ = running
    before = sorted(p.name for p in paths.backups.iterdir())
    corrupt_pages(paths.database)
    original_rename = Path.rename

    def locked(self, target):
        if self == paths.database:
            raise PermissionError(errno.EACCES, "файл зайнятий іншим процесом")
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", locked)
    recorder = Recorder()
    flow = make_flow(session, paths, recorder)
    with caplog.at_level(logging.ERROR):
        run_runtime_recovery(qtbot, flow, window, recorder)
    assert recorder.errors == [QUARANTINE_FAILED_MESSAGE]
    assert recorder.exits == [EXIT_DATA_CORRUPTED] and recorder.choices == []
    assert visible_recovery_dialogs() == []  # діалог не відкривався
    # Первинна помилка збережена й у журналі; файли бази лишилися на місці.
    assert isinstance(flow.quarantine_error, StorageError)
    assert isinstance(flow.quarantine_error.__cause__, PermissionError)
    assert "Runtime quarantine of the corrupted database failed" in caplog.text
    assert paths.database.exists() and not session.is_open
    assert sorted(p.name for p in paths.backups.iterdir()) == before  # копій не створено
    assert forbidden == []


# Жодного нового з'єднання до карантину --------------------------------------------------------


def test_no_new_connection_to_the_database_before_quarantine(
    qtbot, running, monkeypatch, previous_hook
):
    paths, session, _, window, forbidden, _ = running
    corrupt_pages(paths.database)
    opened, reopened = [], []
    original_connect = sqlite3.connect

    def connect(target, *args, **kwargs):
        opened.append(str(target))
        return original_connect(target, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(backup_service_module, "open_database", lambda *a: reopened.append(a))
    recorder = Recorder()
    run_runtime_recovery(qtbot, make_flow(session, paths, recorder), window, recorder, cancel)
    assert [target for target in opened if target == str(paths.database)] == []
    assert reopened == [] and forbidden == []
    # Інші відкриття — лише перевірка копій у теці backups (immutable, лише читання).
    assert all("backups" in target and "immutable=1" in target for target in opened)


# Повторні й звичайні помилки ----------------------------------------------------------------


def test_repeated_corruption_while_dialog_is_open_opens_no_second_dialog(
    qtbot, running, previous_hook
):
    paths, session, _, window, forbidden, _ = running
    corrupt_pages(paths.database)
    visible_during = []

    def more_corruption_then_cancel(dialog):
        for _ in range(3):
            error = sqlite3.DatabaseError("database disk image is malformed")
            error.sqlite_errorcode = sqlite3.SQLITE_CORRUPT
            sys.excepthook(type(error), error, None)

        def check_then_cancel():
            # Усе, що повторні пошкодження могли запланувати, уже оброблено цим моментом.
            visible_during.append(len(visible_recovery_dialogs()))
            cancel(dialog)

        QTimer.singleShot(0, check_then_cancel)

    recorder, seen = Recorder(), []
    run_runtime_recovery(
        qtbot,
        make_flow(session, paths, recorder),
        window,
        recorder,
        more_corruption_then_cancel,
        seen,
    )
    drain_qt_events(qtbot)
    assert visible_during == [1]  # рівно один видимий діалог під час повторних пошкоджень
    assert len(seen) == 1 and recorder.exits == [EXIT_DATA_CORRUPTED] and forbidden == []
    assert visible_recovery_dialogs() == []
    assert len([w for w in QApplication.topLevelWidgets() if isinstance(w, MainWindow)]) == 1


def test_ordinary_error_starts_no_recovery(qtbot, running, previous_hook):
    paths, session, _, window, forbidden, _ = running
    recorder = Recorder()
    with RuntimeCorruptionGuard(make_flow(session, paths, recorder)):
        error = sqlite3.OperationalError("database is locked")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        QTimer.singleShot(0, lambda: sys.excepthook(type(error), error, None))
        drain_qt_events(qtbot)
    assert not recorder.finished and previous_hook == [error]
    assert window.isEnabled() and session.is_open and paths.database.exists()
    assert visible_recovery_dialogs() == [] and forbidden == []


# Тексти діалогу ---------------------------------------------------------------------------


def test_startup_and_runtime_texts(qtbot):
    name = "budget.db.corrupted-20261006-120000"
    startup = RecoveryDialog(name, lambda: [], lambda c: c)
    runtime = RecoveryDialog(name, lambda: [], lambda c: c, runtime=True)
    qtbot.addWidget(startup)
    qtbot.addWidget(runtime)
    assert STARTUP_EXPLANATION.format(name=name) in dialog_texts(startup)
    runtime_text = RUNTIME_EXPLANATION.format(name=name)
    assert runtime_text in dialog_texts(runtime)
    assert "Під час роботи" in runtime_text and name in runtime_text
    assert "Під час запуску" not in runtime_text and "BEFORE_RESTORE" not in runtime_text
    assert runtime.close_button.text() == "Закрити застосунок"


def test_runtime_listing_matches_backup_service_candidates(running):
    """Діалог під час роботи бере той самий перевірений перелік, що й BackupService."""
    paths, _, _, _, _, services = running
    runtime = RecoveryService(paths.database, paths.backups, CLOCK).candidates()
    assert [c.backup.path for c in runtime] == [
        c.backup.path for c in services.backups.candidates()
    ]
