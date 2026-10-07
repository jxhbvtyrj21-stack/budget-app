"""Діалог відновлення після пошкодження під час роботи (Block C4).

Справжній цикл подій Qt і справжній ``RecoveryDialog``: пошкодження з реального слоту →
C3 → один діалог з перевіреними копіями → вибір або скасування. Відновлення (C5) немає.
"""

import sqlite3
import sys
from datetime import UTC, datetime, timedelta

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

import budget.app as app_module
from budget.app import (
    ApplicationSession,
    RuntimeCorruptionGuard,
    RuntimeRecoveryFlow,
)
from budget.domain.calendar import FixedClock
from budget.domain.money import Money
from budget.platform.paths import DataPaths
from budget.services.backup import (
    BackupKind,
    RecoveryService,
    find_backups,
    restore_candidates,
)
from budget.services.facade import AppServices
from budget.services.setup import SetupDraft
from budget.ui.dialogs.recovery_dialog import (
    RUNTIME_EXPLANATION,
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
    # Будь-яка спроба відновити, замінити сервіси чи відкрити базу — порушення меж C4.
    forbidden = []
    monkeypatch.setattr(app_module, "restore_and_open", lambda *a: forbidden.append("restore"))
    monkeypatch.setattr(MainWindow, "replace_services", lambda *a: forbidden.append("replace"))
    monkeypatch.setattr(ApplicationSession, "open", lambda *a: forbidden.append("open"))
    monkeypatch.setattr(ApplicationSession, "adopt", lambda *a: forbidden.append("adopt"))
    yield paths, session, connection, window, forbidden
    window.close()
    session.close()


@pytest.fixture
def previous_hook(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "excepthook", lambda *args: calls.append(args[1]))
    return calls


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


def run_runtime_recovery(qtbot, paths, window, action, choices, seen):
    flow = RuntimeRecoveryFlow(
        RecoveryService(paths.database, paths.backups, CLOCK).candidates, choices.append
    )
    guard = RuntimeCorruptionGuard(flow)
    when_dialog_opens(action, seen)
    with guard:
        QTimer.singleShot(0, window.refresh)  # звичайний слот читає пошкоджену базу
        qtbot.waitUntil(lambda: bool(choices), timeout=5000)
    return guard


def test_runtime_corruption_opens_dialog_and_returns_the_selected_candidate(
    qtbot, running, monkeypatch, previous_hook
):
    paths, session, connection, window, forbidden = running
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
        shown["explanation"] = RUNTIME_EXPLANATION in [
            label.text() for label in dialog.findChildren(type(dialog.empty))
        ]
        dialog.list.setCurrentRow(1)
        dialog.restore_selected()

    choices, seen = [], []
    run_runtime_recovery(qtbot, paths, window, choose_second, choices, seen)

    # Лише перевірені копії, від найновішої; пошкоджена й нерозпізнана не показуються.
    expected = restore_candidates(paths.backups)
    assert shown["texts"] == [candidate_text(c) for c in expected]
    assert expected[0].backup.kind is BackupKind.ON_DEMAND
    assert shown["explanation"] and shown["windows_enabled"] is False
    # Повертається саме кандидат із показаного переліку, а не довільний шлях.
    (choice,) = choices
    assert not choice.cancelled and choice.candidate is shown["candidates"][1]
    assert len(seen) == 1 and previous_hook == []
    # C4 нічого не відновлює і не чіпає базу.
    assert forbidden == []
    assert session.is_open and session.connection is connection
    assert sorted(p.name for p in paths.backups.iterdir()) == before
    assert BackupKind.BEFORE_RESTORE not in {b.kind for b in find_backups(paths.backups)}
    assert not window.isEnabled()  # звичайна робота не продовжується до C5


def test_cancel_is_a_controlled_result_and_does_not_resume_work(qtbot, running, previous_hook):
    paths, session, connection, window, forbidden = running
    corrupt_pages(paths.database)
    choices, seen = [], []
    run_runtime_recovery(
        qtbot, paths, window, lambda dialog: dialog.close_button.click(), choices, seen
    )
    (choice,) = choices
    assert choice.cancelled and choice.candidate is None
    assert not window.isEnabled()  # скасування не повертає до звичайної роботи
    assert session.is_open and session.connection is connection and forbidden == []


def test_declined_confirmation_keeps_the_dialog_open(qtbot, running, monkeypatch, previous_hook):
    paths, _, _, window, forbidden = running
    corrupt_pages(paths.database)
    confirm(monkeypatch, False)
    states = []

    def decline_then_cancel(dialog):
        dialog.restore_selected()  # підтвердження відхилено
        states.append(dialog.isVisible())
        dialog.close_button.click()

    choices, seen = [], []
    run_runtime_recovery(qtbot, paths, window, decline_then_cancel, choices, seen)
    assert states == [True] and choices[0].cancelled and forbidden == []


def test_repeated_corruption_while_dialog_is_open_opens_no_second_dialog(
    qtbot, running, previous_hook, monkeypatch
):
    paths, _, _, window, forbidden = running
    corrupt_pages(paths.database)
    opened = []
    original_init = RecoveryDialog.__init__

    def counting_init(self, *args, **kwargs):
        opened.append(self)
        original_init(self, *args, **kwargs)

    def more_corruption_then_cancel(dialog):
        for _ in range(3):
            error = sqlite3.DatabaseError("database disk image is malformed")
            error.sqlite_errorcode = sqlite3.SQLITE_CORRUPT
            sys.excepthook(type(error), error, None)
        QTimer.singleShot(50, dialog.close_button.click)

    choices, seen = [], []
    monkeypatch.setattr(RecoveryDialog, "__init__", counting_init)
    run_runtime_recovery(qtbot, paths, window, more_corruption_then_cancel, choices, seen)
    qtbot.wait(100)
    assert len(opened) == 1 and len(choices) == 1 and forbidden == []
    assert len([w for w in QApplication.topLevelWidgets() if isinstance(w, MainWindow)]) == 1


def test_ordinary_error_opens_no_recovery_dialog(qtbot, running, previous_hook):
    _, _, _, window, forbidden = running
    choices = []
    flow = RuntimeRecoveryFlow(lambda: [], choices.append)
    with RuntimeCorruptionGuard(flow):
        error = sqlite3.OperationalError("database is locked")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        QTimer.singleShot(0, lambda: sys.excepthook(type(error), error, None))
        qtbot.wait(100)
    assert choices == [] and previous_hook and window.isEnabled() and forbidden == []
    assert not [
        w for w in QApplication.topLevelWidgets() if isinstance(w, RecoveryDialog) and w.isVisible()
    ]


def test_startup_dialog_text_is_unchanged(qtbot):
    dialog = RecoveryDialog("budget.db.corrupted-20261006-120000", lambda: [], lambda c: c)
    qtbot.addWidget(dialog)
    texts = [label.text() for label in dialog.findChildren(type(dialog.empty))]
    assert any("Під час запуску" in t and "budget.db.corrupted-20261006-120000" in t for t in texts)
    assert RUNTIME_EXPLANATION not in texts


def test_runtime_listing_matches_backup_service_candidates(running):
    """Діалог під час роботи бере той самий перевірений перелік, що й BackupService."""
    paths, _, _, window, _ = running
    runtime = RecoveryService(paths.database, paths.backups, CLOCK).candidates()
    assert [c.backup.path for c in runtime] == [
        c.backup.path for c in window._services.backups.candidates()
    ]
