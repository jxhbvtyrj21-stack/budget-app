"""Успішне відновлення під час роботи (Block C5.2).

Справжній цикл подій Qt, справжній ``RecoveryDialog`` і справжня копія з іншого місяця:
пошкодження з реального слоту → C3 → карантин → вибрана копія → ``restore_and_open`` →
``session.adopt`` → ``AppServices.create`` → ``MainWindow.replace_services`` → вікна
активні → ``guard`` знову готовий. Невдачі на цьому шляху — C5.3/C6.
"""

import logging
import sqlite3
import sys
from datetime import UTC, datetime, timedelta

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

import budget.app as app_module
from budget.app import EXIT_DATA_CORRUPTED, ApplicationSession, runtime_recovery_guard
from budget.domain.calendar import FixedClock
from budget.domain.models import ReplenishmentPart, SourceKind, SourceRef
from budget.domain.money import Money
from budget.platform.paths import DataPaths
from budget.services.backup import BackupKind, find_backups
from budget.services.facade import AppServices
from budget.services.month import MonthTransitionService
from budget.services.setup import InitialAccumulation, InitialDebt, SetupDraft
from budget.storage.recovery import open_backup_read_only
from budget.ui.dialogs.long_gap_dialog import LongGapDialog
from budget.ui.dialogs.recovery_dialog import RecoveryDialog
from budget.ui.main_window import MainWindow

SEPTEMBER = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)  # копія
OCTOBER = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)  # пошкодження й відновлення
REMAINDER = SourceRef(SourceKind.GENERAL_REMAINDER)
PAGES = ("overview", "month", "accumulations", "debts", "service")


def snapshot(connection: sqlite3.Connection) -> dict[str, list]:
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
    ]
    return {t: connection.execute(f'SELECT * FROM "{t}" ORDER BY 1').fetchall() for t in tables}


def state(services: AppServices, month) -> dict[str, list]:
    """Фінансовий стан, прочитаний через сервіси."""
    return {
        "incomes": services.incomes.list_for_month(month),
        "expenses": services.expenses.list_for_month(month),
        "replenishments": services.replenishments.list_for_month(month),
        "accumulations": services.accumulations.list_working(),
        "debts": services.debts.list_active(),
        "repayments": services.debts.repayments_for_month(month),
    }


class Running:
    """Працюючий застосунок: сесія, вікно, guard і те, що отримав ``_run_gui``."""

    def __init__(self, tmp_path, qtbot) -> None:
        self.clock = FixedClock(SEPTEMBER)
        self.paths = DataPaths(tmp_path / "Users" / "Олена Коваль" / "AppData" / "Local" / "Budget")
        self.session = ApplicationSession(self.paths, self.clock)
        connection = self.session.open()
        services = AppServices.create(connection, self.clock, self.paths.backups)
        services.setup.complete(
            SetupDraft(
                general_remainder=Money(1_000_000),
                accumulations=(InitialAccumulation("Подорож", None, Money(500_000)),),
                debts=(InitialDebt("Позика", None, Money(300_000)),),
            )
        )
        income = services.incomes.create("Вереснева зарплата", None, Money(40_000))
        source = SourceRef(SourceKind.INCOME, income_id=income.income.id)
        services.expenses.create("Продукти", None, Money(1_500), source)
        trip = services.accumulations.list_working()[0].accumulation.id
        services.replenishments.create(
            "Відкладаю", None, trip, [ReplenishmentPart(REMAINDER, Money(20_000))]
        )
        debt = services.debts.list_active()[0].debt.id
        services.debts.repay(debt, Money(50_000), REMAINDER)
        for index in range(150):  # достатньо сторінок, щоб читання зачепило пошкоджені
            services.incomes.create(f"Дохід {index}", "опис " * 40, Money(100))
        self.month = services.months.current_month()
        self.clock.set(SEPTEMBER + timedelta(hours=1))
        self.backup = services.backups.create_backup(BackupKind.ON_DEMAND)  # найновіша копія
        self.expected = snapshot(connection)
        self.expected_state = state(services, self.month)
        # Після копії: ця зміна не повинна пережити відновлення.
        services.incomes.create("Після копії", None, Money(9_900))
        self.clock.set(OCTOBER)
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("PRAGMA cache_size = 0")
        self.old_connection = connection
        self.old_services = services
        self.window = MainWindow("Budget", services)
        qtbot.addWidget(self.window)
        self.window.show()
        self.old_pages = {page: getattr(self.window, page) for page in PAGES}
        self.exits: list[int] = []
        self.errors: list[str] = []
        self.guard = runtime_recovery_guard(
            self.session,
            self.paths,
            self.clock,
            lambda: self.window,
            exit_application=self.exits.append,
            show_error=self.errors.append,
        )

    def close(self) -> None:
        self.window.close()
        self.session.close()


@pytest.fixture
def running(tmp_path, qtbot):
    app = Running(tmp_path, qtbot)
    yield app
    app.close()


@pytest.fixture
def previous_hook(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "excepthook", lambda *args: calls.append(args[1]))
    return calls


@pytest.fixture
def confirmed(monkeypatch):
    """Підтвердження «Відновити» без показу вікна підтвердження."""
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


@pytest.fixture
def restores(monkeypatch):
    """Справжній ``restore_and_open``; записується аргумент і повернуте з'єднання."""
    calls = []
    original = app_module.restore_and_open

    def restore_and_open(paths, clock, backup_path):
        calls.append(("call", backup_path))
        connection = original(paths, clock, backup_path)
        calls.append(("returned", connection))
        return connection

    monkeypatch.setattr(app_module, "restore_and_open", restore_and_open)
    return calls


@pytest.fixture
def replaced(monkeypatch):
    """Що отримали ``AppServices.create`` і ``MainWindow.replace_services`` (справжні)."""
    seen = {"create": [], "replace": []}
    create = AppServices.create
    replace = MainWindow.replace_services

    def create_spy(connection, clock, backups_dir=None):
        services = create(connection, clock, backups_dir)
        seen["create"].append((connection, services))
        return services

    def replace_spy(window, services):
        seen["replace"].append(services)
        replace(window, services)

    monkeypatch.setattr(AppServices, "create", create_spy)
    monkeypatch.setattr(MainWindow, "replace_services", replace_spy)
    return seen


@pytest.fixture
def transitions(monkeypatch):
    calls = []
    monkeypatch.setattr(MonthTransitionService, "run_on_startup", lambda self: calls.append(self))
    return calls


@pytest.fixture
def long_gap_dialogs(monkeypatch):
    opened = []
    monkeypatch.setattr(LongGapDialog, "exec", lambda self: opened.append(self) or 0)
    return opened


def corrupt_pages(path) -> None:
    pages = path.stat().st_size // 4096
    with path.open("r+b") as handle:
        for page in range(2, pages):
            handle.seek(page * 4096)
            handle.write(b"\xa5" * 4096)


def visible_recovery_dialogs() -> list[RecoveryDialog]:
    return [
        widget
        for widget in QApplication.topLevelWidgets()
        if isinstance(widget, RecoveryDialog) and widget.isVisible()
    ]


def flush_deletions() -> None:
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def service_ids(services: AppServices) -> set[int]:
    return {id(services)} | {id(getattr(services, name)) for name in services.__slots__}


def references_to(window: MainWindow, services: AppServices) -> list[str]:
    """Хто з активного UI (вікно, усі його QObject-нащадки) посилається на граф ``services``."""
    targets = service_ids(services)
    found = []
    for obj in [window, *window.findChildren(QObject)]:
        for name, value in vars(obj).items():
            values = (
                value.values()
                if isinstance(value, dict)
                else (value if isinstance(value, list | tuple | set) else [value])
            )
            if any(id(v) in targets for v in values):
                found.append(f"{type(obj).__name__}.{name}")
    return found


def is_closed(connection: sqlite3.Connection) -> bool:
    try:
        connection.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return True
    return False


def choose(row: int, shown: dict | None = None):
    def action(dialog: RecoveryDialog) -> None:
        if shown is not None:
            shown["candidate"] = dialog.list.item(row).data(Qt.ItemDataRole.UserRole)
            shown["windows_enabled"] = [
                w.isEnabled() for w in QApplication.topLevelWidgets() if isinstance(w, MainWindow)
            ]
        dialog.list.setCurrentRow(row)
        dialog.restore_selected()

    return action


def cancel(dialog: RecoveryDialog) -> None:
    dialog.close_button.click()


def corrupt_and_recover(qtbot, app: Running, action, seen: list, previous_hook) -> None:
    """Пошкодження під час роботи: звичайний слот читає пошкоджену базу; далі — діалог."""
    corrupt_pages(app.paths.database)
    dialogs_before = len(seen)

    def poll():
        dialog = QApplication.activeModalWidget()
        if isinstance(dialog, RecoveryDialog):
            seen.append(dialog)
            action(dialog)
        else:
            QTimer.singleShot(10, poll)

    QTimer.singleShot(10, poll)
    QTimer.singleShot(0, app.window.refresh)
    qtbot.waitUntil(
        lambda: (
            len(seen) > dialogs_before
            and (app.window.isEnabled() or bool(app.exits) or bool(previous_hook))
        ),
        timeout=10000,
    )


@pytest.fixture
def restored(qtbot, running, confirmed, restores, replaced, previous_hook, caplog):
    """Найновішу копію (вересень) відновлено під час роботи (жовтень)."""
    seen, shown = [], {}
    with caplog.at_level(logging.INFO), running.guard:
        corrupt_and_recover(qtbot, running, choose(0, shown), seen, previous_hook)
    flush_deletions()
    return running, seen, shown


# Успішне відновлення -------------------------------------------------------------------------


def test_selected_backup_is_restored_and_work_continues(restored, previous_hook, caplog):
    app, seen, shown = restored
    assert len(seen) == 1 and shown["windows_enabled"] == [False]  # під час діалогу — неактивні
    assert app.exits == [] and app.errors == [] and previous_hook == []
    assert app.session.is_open and app.paths.database.exists()
    assert [p for p in app.paths.root.iterdir() if ".corrupted-" in p.name]  # карантин лишився
    assert BackupKind.BEFORE_RESTORE not in {b.kind for b in find_backups(app.paths.backups)}
    logged = "\n".join(r.getMessage() for r in caplog.get_records("setup"))
    assert "Runtime restore is not available yet" not in logged
    assert f"Database restored at runtime from {app.backup.name}" in logged
    assert "Runtime recovery finished" in logged


def test_exact_selected_candidate_is_restored(
    qtbot, running, confirmed, restores, replaced, previous_hook
):
    shown, seen = {}, []
    with running.guard:
        corrupt_and_recover(qtbot, running, choose(1, shown), seen, previous_hook)
    candidate = shown["candidate"]
    assert candidate.backup.path != running.backup  # не типова (найновіша) копія
    assert [value for kind, value in restores if kind == "call"] == [candidate.backup.path]
    backup = open_backup_read_only(candidate.backup.path)
    try:
        assert snapshot(running.session.connection) == snapshot(backup)
    finally:
        backup.close()


def test_session_adopts_the_exact_returned_connection(restored, restores):
    app, _, _ = restored
    (returned,) = [value for kind, value in restores if kind == "returned"]
    assert app.session.connection is returned
    assert is_closed(app.old_connection)


def test_no_second_connection_after_restore_and_open(
    qtbot, running, confirmed, restores, replaced, previous_hook, monkeypatch
):
    events = []
    original_connect = sqlite3.connect

    def connect(target, *args, **kwargs):
        connection = original_connect(target, *args, **kwargs)
        events.append(("connect", str(target), connection))
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    original_restore = app_module.restore_and_open

    def marked(paths, clock, backup_path):
        connection = original_restore(paths, clock, backup_path)
        events.append(("returned", None, connection))
        return connection

    monkeypatch.setattr(app_module, "restore_and_open", marked)
    with running.guard:
        corrupt_and_recover(qtbot, running, choose(0), [], previous_hook)
    boundary = next(i for i, event in enumerate(events) if event[0] == "returned")
    after = [(target, c) for kind, target, c in events[boundary + 1 :] if kind == "connect"]
    # Після відновлення — жодного з'єднання з робочою базою; лише короткі перевірки копій
    # (лише читання, immutable), уже закриті.
    assert all("backups" in t and "immutable=1" in t and is_closed(c) for t, c in after)
    to_database = [
        c for kind, t, c in events if kind == "connect" and t == str(running.paths.database)
    ]
    still_open = [c for c in to_database if not is_closed(c)]
    assert still_open == [running.session.connection]  # одна постійна база — у сесії


def test_services_are_built_on_the_new_connection(restored, restores, replaced):
    (returned,) = [value for kind, value in restores if kind == "returned"]
    (created,) = replaced["create"]
    assert created[0] is returned
    services = created[1]
    assert replaced["replace"] == [services]
    services.incomes.create("Після відновлення", None, Money(700))
    count = returned.execute(
        "SELECT count(*) FROM incomes WHERE name = ?", ("Після відновлення",)
    ).fetchone()
    assert count == (1,)


def test_main_window_uses_the_new_graph(restored, replaced):
    app, _, _ = restored
    (services,) = replaced["replace"]
    assert references_to(app.window, services)
    assert all(getattr(app.window, page) is not None for page in PAGES)


def test_old_graph_is_unreachable_from_the_window(restored):
    app, _, _ = restored
    for page in PAGES:
        assert getattr(app.window, page) is not app.old_pages[page], page
    assert references_to(app.window, app.old_services) == []


def test_ui_is_enabled_again_and_dialog_is_gone(restored):
    app, _, _ = restored
    assert app.window.isEnabled()
    assert all(w.isEnabled() for w in QApplication.topLevelWidgets() if isinstance(w, MainWindow))
    assert visible_recovery_dialogs() == []
    assert QApplication.activeModalWidget() is None


# Саме стан копії: без переходу між місяцями -------------------------------------------------


def test_no_month_transition_and_no_long_gap_dialog(
    qtbot, running, confirmed, restores, replaced, previous_hook, transitions, long_gap_dialogs
):
    assert running.month.month == 9 and running.clock.now().month == 10  # копія — з іншого місяця
    with running.guard:
        corrupt_and_recover(qtbot, running, choose(0), [], previous_hook)
    assert running.window.isEnabled() and running.exits == []
    assert transitions == [] and long_gap_dialogs == []


def test_exact_backup_state_is_preserved(restored, replaced):
    app, _, _ = restored
    (services,) = replaced["replace"]
    assert app.month.month == 9 and app.clock.now().month == 10
    # Через нові сервіси: доходи, витрата, поповнення, накопичення, борг і погашення.
    assert state(services, app.month) == app.expected_state
    names = [view.income.name for view in services.incomes.list_for_month(app.month)]
    assert "Вереснева зарплата" in names and "Після копії" not in names
    # І на рівні бази: усі таблиці — точно як у копії (жовтневого переходу немає).
    assert snapshot(app.session.connection) == app.expected


# Після відновлення guard знову працює --------------------------------------------------------


def test_second_corruption_after_restore_starts_one_new_recovery(
    qtbot, running, confirmed, restores, replaced, previous_hook, caplog
):
    seen = []
    with caplog.at_level(logging.INFO), running.guard:
        corrupt_and_recover(qtbot, running, choose(0), seen, previous_hook)
        assert running.window.isEnabled() and not running.guard.detected
        restored_connection = running.session.connection
        restored_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        restored_connection.execute("PRAGMA cache_size = 0")
        running.clock.set(OCTOBER + timedelta(hours=1))  # нова назва карантину
        corrupt_and_recover(qtbot, running, cancel, seen, previous_hook)
    assert len(seen) == 2 and seen[0] is not seen[1]
    assert running.exits == [EXIT_DATA_CORRUPTED] and previous_hook == []
    detected = [r for r in caplog.records if "Database corruption detected at runtime" in r.message]
    assert len(detected) == 2
    assert is_closed(restored_connection) and not running.session.is_open
    quarantined = [
        p.name for p in running.paths.root.iterdir() if p.name.startswith("budget.db.corrupted-")
    ]
    assert len([name for name in quarantined if not name.endswith(("-wal", "-shm"))]) == 2
