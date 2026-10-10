"""Пошкодження до запуску циклу подій (H2).

Справжній життєвий цикл ``run_application`` (усе, що робить ``_run_gui`` після створення
``QApplication``): блокування, сесія, звичайний запуск, побудова вікна, цикл подій.
Справжнє пошкодження сторінок бази стається вже після відкриття сесії, коли будується
головне вікно (``show_main_window``), — саме той стан, у якому раніше був тихий вихід.
Далі — наявний шлях запуску (Block B): карантин, «Дані пошкоджено», відновлення.
"""

import hashlib
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

import budget.app as app_module
from budget.app import (
    EXIT_DATA_CORRUPTED,
    EXIT_OK,
    ApplicationSession,
    RuntimeCorruptionGuard,
    RuntimeRecoveryFlow,
    open_application_database,
    run_application,
)
from budget.domain.calendar import FixedClock
from budget.domain.money import Money
from budget.errors import DatabaseCorruptedError
from budget.platform.identity import load_product_identity
from budget.platform.paths import DataPaths
from budget.services import backup as backup_service_module
from budget.services.backup import BackupKind, RestoreError
from budget.services.facade import AppServices
from budget.services.month import MonthTransitionService
from budget.services.setup import SetupDraft
from budget.storage.recovery import database_files, open_backup_read_only
from budget.ui.dialogs.long_gap_dialog import LongGapDialog
from budget.ui.dialogs.recovery_dialog import (
    RUNTIME_EXPLANATION,
    STARTUP_EXPLANATION,
    RecoveryDialog,
)
from budget.ui.main_window import MainWindow

SEPTEMBER = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)  # копія
OCTOBER = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)  # запуск із пошкодженням


def snapshot(connection: sqlite3.Connection) -> dict[str, list]:
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
    ]
    return {t: connection.execute(f'SELECT * FROM "{t}" ORDER BY 1').fetchall() for t in tables}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def corrupt_pages(path: Path) -> None:
    pages = path.stat().st_size // 4096
    with path.open("r+b") as handle:
        for page in range(2, pages):
            handle.seek(page * 4096)
            handle.write(b"\xa5" * 4096)


class Startup:
    """Дані з вересня (копія «на вимогу»), запуск у жовтні; записи всього, що сталося."""

    def __init__(self, tmp_path, monkeypatch) -> None:
        self.paths = DataPaths(tmp_path / "Users" / "Олена Коваль" / "AppData" / "Local" / "Budget")
        self.clock = FixedClock(SEPTEMBER)
        connection = open_application_database(self.paths, self.clock)
        services = AppServices.create(connection, self.clock, self.paths.backups)
        services.setup.complete(SetupDraft(general_remainder=Money(100_000)))
        services.incomes.create("Вереснева зарплата", None, Money(40_000))
        for index in range(150):  # достатньо сторінок, щоб читання зачепило пошкоджені
            services.incomes.create(f"Дохід {index}", "опис " * 40, Money(100))
        self.backup = services.backups.create_backup(BackupKind.ON_DEMAND)
        backup_reader = open_backup_read_only(self.backup)
        self.expected = snapshot(backup_reader)
        backup_reader.close()
        connection.close()
        self.clock.set(OCTOBER)
        self.events: list = []
        self.windows: list[MainWindow] = []
        self.adopted: list[sqlite3.Connection] = []
        self.returned: list[sqlite3.Connection] = []
        self.at_close: dict[str, str] = {}
        self.dialogs: list[RecoveryDialog] = []
        self.dialog_texts: list[list[str]] = []
        self.messages: list[str] = []
        self.transitions = 0
        self.long_gaps: list = []
        self.runtime_flows: list = []
        self.rearms = 0
        self._patch(monkeypatch)

    def _patch(self, monkeypatch) -> None:
        original_show = app_module.show_main_window
        corrupted = []

        def show_main_window(identity, connection, clock, backups_dir, *, restored):
            if not corrupted:
                # Справжнє пошкодження вже відкритої бази: кадри у WAL, основний файл
                # пошкоджено, кеш порожній — побудова вікна читає пошкоджені сторінки.
                corrupted.append(True)
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.execute("UPDATE general_remainder SET balance = 4242")
                connection.execute("PRAGMA cache_size = 0")
                corrupt_pages(self.paths.database)
                self.events.append("corrupted")
            window = original_show(identity, connection, clock, backups_dir, restored=restored)
            self.events.append(("window", restored))
            self.windows.append(window)
            return window

        monkeypatch.setattr(app_module, "show_main_window", show_main_window)

        original_close = ApplicationSession.close_after_corruption

        def close_after_corruption(session):
            if session.is_open:
                files = database_files(self.paths.database)
                self.at_close = {p.name: digest(p) for p in files[:2] if p.exists()}
                self.events.append("close_after_corruption")
            original_close(session)

        monkeypatch.setattr(ApplicationSession, "close_after_corruption", close_after_corruption)

        original_adopt = ApplicationSession.adopt

        def adopt(session, connection):
            self.adopted.append(connection)
            original_adopt(session, connection)

        monkeypatch.setattr(ApplicationSession, "adopt", adopt)

        original_restore = app_module.restore_and_open

        def restore_and_open(paths, clock, backup_path):
            connection = original_restore(paths, clock, backup_path)
            self.returned.append(connection)
            return connection

        monkeypatch.setattr(app_module, "restore_and_open", restore_and_open)

        original_transition = MonthTransitionService.run_on_startup

        def run_on_startup(service):
            self.transitions += 1
            return original_transition(service)

        monkeypatch.setattr(MonthTransitionService, "run_on_startup", run_on_startup)
        monkeypatch.setattr(
            LongGapDialog, "exec", lambda dialog: self.long_gaps.append(dialog) or 0
        )
        monkeypatch.setattr(
            RuntimeRecoveryFlow, "__call__", lambda flow, error: self.runtime_flows.append(error)
        )
        original_rearm = RuntimeCorruptionGuard.rearm

        def rearm(guard):
            self.rearms += 1
            original_rearm(guard)

        monkeypatch.setattr(RuntimeCorruptionGuard, "rearm", rearm)
        monkeypatch.setattr(
            app_module, "_show_message", lambda title, text: self.messages.append(text)
        )
        monkeypatch.setattr(QMessageBox, "exec", lambda box: 0)
        monkeypatch.setattr(
            QMessageBox,
            "clickedButton",
            lambda box: next(
                b
                for b in box.buttons()
                if box.buttonRole(b) == QMessageBox.ButtonRole.DestructiveRole
            ),
        )

    def run(self, qapp, action) -> int:
        """Справжній ``run_application``; ``action`` діє в діалозі, як користувач.

        Цикл подій ще не працює, коли відкривається діалог: його власний цикл
        (``exec``) обробляє таймер умови. Після успішного відновлення справжній цикл
        подій застосунку завершується, щойно головне вікно видиме.
        """

        finished = []

        def poll_dialog():
            if finished:
                return
            dialog = QApplication.activeModalWidget()
            if isinstance(dialog, RecoveryDialog) and dialog not in self.dialogs:
                self.dialogs.append(dialog)
                self.dialog_texts.append(
                    [label.text() for label in dialog.findChildren(type(dialog.empty))]
                )
                self.events.append("dialog")
                action(dialog)
            elif not self.windows or self.windows[-1] is None:
                QTimer.singleShot(10, poll_dialog)

        def poll_window():
            if finished:
                return
            window = self.windows[-1] if self.windows else None
            if window is not None and window.isVisible() and window.isEnabled():
                self.events.append("event_loop")
                qapp.exit(EXIT_OK)
            else:
                QTimer.singleShot(10, poll_window)

        QTimer.singleShot(10, poll_dialog)
        QTimer.singleShot(10, poll_window)
        try:
            return run_application(load_product_identity(), self.paths, self.clock, qapp)
        finally:
            finished.append(True)  # таймери умови більше нічого не роблять
            for window in self.windows:
                if window is not None:
                    window.close()
                    window.deleteLater()

    def quarantined(self) -> list[Path]:
        return sorted(p for p in self.paths.root.iterdir() if ".corrupted-" in p.name)


@pytest.fixture
def startup(tmp_path, monkeypatch, qapp):
    return Startup(tmp_path, monkeypatch)


@pytest.fixture
def previous_hook(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "excepthook", lambda *args: calls.append(args[1]))
    return calls


def choose_backup(startup: Startup):
    def action(dialog: RecoveryDialog) -> None:
        for row in range(dialog.list.count()):
            if dialog.list.item(row).data(Qt.ItemDataRole.UserRole).backup.path == startup.backup:
                dialog.list.setCurrentRow(row)
        dialog.restore_selected()

    return action


def cancel(dialog: RecoveryDialog) -> None:
    dialog.close_button.click()


# A + B: пошкодження до циклу подій → відновлення → звичайна робота -------------------------


def test_corruption_before_event_loop_is_recovered_and_app_continues(qapp, startup, previous_hook):
    code = startup.run(qapp, choose_backup(startup))
    assert code == EXIT_OK
    # Порядок: пошкодження → закриття без checkpoint → один діалог запуску → вікно → цикл.
    assert startup.events == [
        "corrupted",
        "close_after_corruption",
        "dialog",
        ("window", True),
        "event_loop",
    ]
    assert len(startup.dialogs) == 1
    (texts,) = startup.dialog_texts
    (kept,) = [p for p in startup.quarantined() if not p.name.endswith(("-wal", "-shm"))]
    assert STARTUP_EXPLANATION.format(name=kept.name) in texts
    assert not any(RUNTIME_EXPLANATION.format(name=kept.name) == t for t in texts)
    # H1: закриття не перенесло WAL у пошкоджений файл; у карантині ті самі .db і WAL.
    kept_files = database_files(kept)
    assert all(p.exists() for p in kept_files[:2])
    assert {
        name.replace(kept.name, "budget.db"): digest(p)
        for name, p in ((p.name, p) for p in kept_files[:2])
    } == startup.at_close
    # Сесія взяла саме повернуте з'єднання; guard знову готовий; відновлення під час
    # роботи (QTimer) не запускалося.
    assert startup.adopted == startup.returned and len(startup.returned) == 1
    assert startup.rearms == 1 and startup.runtime_flows == []
    # Стан копії без переходу між місяцями: перехід — лише в першій (невдалій) спробі.
    assert startup.transitions == 1 and startup.long_gaps == []
    reader = sqlite3.connect(startup.paths.database.as_uri() + "?mode=ro", uri=True)
    try:
        assert snapshot(reader) == startup.expected
    finally:
        reader.close()
    assert startup.messages == [] and previous_hook == []


# C: скасування --------------------------------------------------------------------------------


def test_cancel_exits_data_corrupted_without_main_window(qapp, startup, previous_hook):
    code = startup.run(qapp, cancel)
    assert code == EXIT_DATA_CORRUPTED
    assert startup.events == ["corrupted", "close_after_corruption", "dialog"]
    assert startup.windows == []  # головного вікна не показано
    assert startup.adopted == [] and startup.returned == []
    assert not startup.paths.database.exists() and startup.quarantined()
    assert startup.rearms == 0 and startup.runtime_flows == []
    assert startup.messages == [] and previous_hook == []


# D: невдачі -----------------------------------------------------------------------------------


def test_failed_restore_stays_in_dialog_then_cancel(qapp, startup, previous_hook, monkeypatch):
    original_check = backup_service_module.RecoveryService.check_restored
    failed = []

    def check_restored(service, connection):
        if not failed:
            failed.append(True)
            raise RestoreError(detail="integrity_check не пройдено (введено тестом)")
        original_check(service, connection)

    monkeypatch.setattr(backup_service_module.RecoveryService, "check_restored", check_restored)
    shown = {}

    def fail_then_cancel(dialog):
        choose_backup(startup)(dialog)
        shown["visible"] = dialog.isVisible()
        shown["failure"] = dialog.failure.body.text()
        shown["database"] = startup.paths.database.exists()
        cancel(dialog)

    code = startup.run(qapp, fail_then_cancel)
    assert code == EXIT_DATA_CORRUPTED
    assert shown["visible"] is True and "Не вдалося відновити" in shown["failure"]
    assert shown["database"] is False  # відкат прибрав відновлену базу
    assert startup.windows == [] and startup.adopted == []
    assert startup.messages == [] and previous_hook == []  # жодного неперехопленого винятку


def test_quarantine_failure_is_reported_and_exits(qapp, startup, previous_hook, monkeypatch):
    original_rename = Path.rename

    def locked(path, target):
        if path == startup.paths.database:
            raise PermissionError(13, "файл зайнятий іншим процесом")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", locked)
    code = startup.run(qapp, cancel)
    assert code == EXIT_DATA_CORRUPTED
    assert startup.events == ["corrupted", "close_after_corruption"]
    assert startup.messages == [DatabaseCorruptedError().user_message]
    assert startup.dialogs == [] and startup.windows == []
    assert startup.paths.database.exists() and startup.quarantined() == []
    assert previous_hook == []
