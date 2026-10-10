"""Відновлення з копії на екрані «Сервіс» під час звичайної роботи (S3; IA 8; DS-5).

Справжня сесія, справжні копії, справжній ``restore_and_open`` і справжнє головне вікно.
Відновлення виконується синхронно в обробнику кнопки, тож тести детерміновані: жодних
очікувань чи таймерів. Невдачі вносяться лише в конкретну точку виробничого коду:
``check_restored`` (перевірка відновленої бази), ``discard_database`` (відкат),
``open_application_database`` (повторне відкриття), ``replace_services``.
"""

import ast
import errno
import inspect
import logging
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject
from PySide6.QtWidgets import QApplication, QMessageBox

import budget.app as app_module
import budget.ui.screens.service as service_module
from budget.app import (
    APPLICATION_WILL_CLOSE,
    EXIT_DATA_CORRUPTED,
    MANUAL_RESTORE_REOPEN_FAILED_MESSAGE,
    RESTORE_INCOMPLETE_MESSAGE,
    ApplicationSession,
    ManualRestore,
)
from budget.domain.calendar import FixedClock
from budget.domain.models import SourceKind, SourceRef
from budget.domain.money import Money
from budget.errors import StorageError
from budget.platform.identity import load_product_identity
from budget.platform.paths import DataPaths
from budget.services import backup as backup_service_module
from budget.services.backup import (
    ROLLBACK_FAILED_MESSAGE,
    BackupKind,
    BackupService,
    RestoreError,
    find_backups,
)
from budget.services.facade import AppServices
from budget.services.month import MonthTransitionService
from budget.services.setup import SetupDraft
from budget.storage.recovery import open_backup_read_only
from budget.ui.dialogs.long_gap_dialog import LongGapDialog
from budget.ui.dialogs.recovery_dialog import RecoveryDialog
from budget.ui.main_window import MainWindow
from budget.ui.screens.service import ServicePage

SEPTEMBER = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)  # копія
OCTOBER = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)  # відновлення
REMAINDER = SourceRef(SourceKind.GENERAL_REMAINDER)


def snapshot(connection: sqlite3.Connection) -> dict[str, list]:
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
    ]
    return {t: connection.execute(f'SELECT * FROM "{t}" ORDER BY 1').fetchall() for t in tables}


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


def flush_deletions() -> None:
    """Старий центральний віджет видаляє Qt (``deleteLater``) після повернення в цикл подій."""
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def is_closed(connection: sqlite3.Connection) -> bool:
    try:
        connection.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return True
    return False


class Running:
    """Застосунок працює на здоровій базі; копія «на вимогу» — з вересня."""

    def __init__(self, tmp_path, qtbot) -> None:
        self.clock = FixedClock(SEPTEMBER)
        self.paths = DataPaths(tmp_path / "Users" / "Олена Коваль" / "AppData" / "Local" / "Budget")
        self.session = ApplicationSession(self.paths, self.clock)
        connection = self.session.open()
        services = AppServices.create(connection, self.clock, self.paths.backups)
        services.setup.complete(SetupDraft(general_remainder=Money(1_000_000)))
        income = services.incomes.create("Вереснева зарплата", None, Money(40_000))
        services.expenses.create(
            "Продукти", None, Money(1_500), SourceRef(SourceKind.INCOME, income_id=income.income.id)
        )
        self.clock.set(SEPTEMBER + timedelta(hours=1))
        self.backup = services.backups.create_backup(BackupKind.ON_DEMAND)
        reader = open_backup_read_only(self.backup)
        self.expected = snapshot(reader)  # стан копії
        reader.close()
        services.incomes.create("Після копії", None, Money(9_900))
        self.clock.set(OCTOBER)
        self.current = snapshot(connection)  # стан перед відновленням
        self.old_connection = connection
        self.old_services = services
        self.window = MainWindow(load_product_identity().name, services)
        qtbot.addWidget(self.window)
        self.window.show()
        self.old_overview = self.window.overview
        self.exits: list[int] = []
        self.errors: list[str] = []
        self.handler = ManualRestore(
            self.session,
            self.paths,
            self.clock,
            self.window,
            exit_application=self.exits.append,
            show_error=self.errors.append,
        )
        self.window.set_restore_handler(self.handler)

    def select(self, backup: Path) -> ServicePage:
        self.window.navigate("service")
        page = self.window.service
        row = next(i for i, c in enumerate(page.candidates) if c.backup.path == backup)
        page.table.selectRow(row)
        return page

    def restore(self, backup: Path | None = None) -> ServicePage:
        page = self.select(backup or self.backup)
        page.restore_button.click()  # підтвердження, потім синхронне відновлення
        return page

    def close(self) -> None:
        self.window.close()
        self.session.close()


@pytest.fixture
def app(tmp_path, qtbot):
    running = Running(tmp_path, qtbot)
    yield running
    running.close()


@pytest.fixture
def confirmed(monkeypatch):
    return confirm_with(monkeypatch, QMessageBox.ButtonRole.DestructiveRole)


def confirm_with(monkeypatch, role) -> list:
    shown = []
    monkeypatch.setattr(QMessageBox, "exec", lambda box: shown.append(box.text()) or 0)
    monkeypatch.setattr(
        QMessageBox,
        "clickedButton",
        lambda box: next(b for b in box.buttons() if box.buttonRole(b) == role),
    )
    return shown


@pytest.fixture
def spies(monkeypatch):
    """Справжні виклики; записується, що саме викликано і з чим."""
    seen = {
        "restore": [],
        "returned": [],
        "raised": [],
        "open": 0,
        "adopt": [],
        "create": [],
        "replace": [],
        "transitions": 0,
        "long_gap": [],
        "connect": [],
        "corruption_close": 0,
    }
    original_corruption_close = ApplicationSession.close_after_corruption

    def close_after_corruption(session):
        seen["corruption_close"] += 1
        original_corruption_close(session)

    monkeypatch.setattr(ApplicationSession, "close_after_corruption", close_after_corruption)
    original_restore = app_module.restore_and_open

    def restore_and_open(paths, clock, backup_path):
        seen["restore"].append(backup_path)
        try:
            connection = original_restore(paths, clock, backup_path)
        except BaseException as error:
            seen["raised"].append(error)
            raise
        seen["returned"].append(connection)
        return connection

    monkeypatch.setattr(app_module, "restore_and_open", restore_and_open)
    original_open = ApplicationSession.open

    def session_open(session):
        seen["open"] += 1
        return original_open(session)

    monkeypatch.setattr(ApplicationSession, "open", session_open)
    original_adopt = ApplicationSession.adopt

    def adopt(session, connection):
        seen["adopt"].append(connection)
        original_adopt(session, connection)

    monkeypatch.setattr(ApplicationSession, "adopt", adopt)
    original_create = AppServices.create

    def create(connection, clock, backups_dir=None):
        services = original_create(connection, clock, backups_dir)
        seen["create"].append((connection, services))
        return services

    monkeypatch.setattr(AppServices, "create", create)
    original_replace = MainWindow.replace_services

    def replace(window, services):
        seen["replace"].append(services)
        original_replace(window, services)

    monkeypatch.setattr(MainWindow, "replace_services", replace)
    original_transition = MonthTransitionService.run_on_startup

    def run_on_startup(service):
        seen["transitions"] += 1
        return original_transition(service)

    monkeypatch.setattr(MonthTransitionService, "run_on_startup", run_on_startup)
    monkeypatch.setattr(LongGapDialog, "exec", lambda d: seen["long_gap"].append(d) or 0)
    original_connect = sqlite3.connect

    def connect(target, *args, **kwargs):
        connection = original_connect(target, *args, **kwargs)
        seen["connect"].append((str(target), connection))
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    return seen


@pytest.fixture
def previous_hook(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "excepthook", lambda *args: calls.append(args[1]))
    return calls


def open_to_database(spies, app) -> list[sqlite3.Connection]:
    """Відкриті з'єднання з робочою базою, створені під час тесту."""
    return [c for t, c in spies["connect"] if t == str(app.paths.database) and not is_closed(c)]


def no_recovery(app) -> bool:
    dialogs = [
        w for w in QApplication.topLevelWidgets() if isinstance(w, RecoveryDialog) and w.isVisible()
    ]
    quarantined = [p for p in app.paths.root.iterdir() if ".corrupted-" in p.name]
    return dialogs == [] and quarantined == []


def fail_check_once(monkeypatch) -> None:
    """Відновлена база не пройшла перевірку — ``restore_and_open`` робить відкат."""
    original = backup_service_module.RecoveryService.check_restored
    used = []

    def check_restored(service, connection):
        if not used:
            used.append(True)
            raise RestoreError(detail="integrity_check не пройдено (введено тестом)")
        original(service, connection)

    monkeypatch.setattr(backup_service_module.RecoveryService, "check_restored", check_restored)


# S3.1–S3.3: кнопка й підтвердження -----------------------------------------------------------


def test_restore_button_is_enabled_with_candidates(app):
    app.window.navigate("service")
    page = app.window.service
    assert page.candidates and page.restore_button.isEnabled()


def test_restore_button_is_disabled_without_candidates(qtbot, app, tmp_path):
    page = ServicePage(
        BackupService(app.old_connection, tmp_path / "немає копій", app.clock), "Budget"
    )
    qtbot.addWidget(page)
    requested = []
    page.restore_requested.connect(requested.append)
    assert not page.restore_button.isEnabled()
    page.request_restore()
    assert requested == []


def test_cancelled_confirmation_changes_nothing(app, spies, monkeypatch):
    shown = confirm_with(monkeypatch, QMessageBox.ButtonRole.RejectRole)
    app.restore()
    assert len(shown) == 1 and shown[0].startswith("Поточні дані буде замінено даними копії від")
    assert spies["restore"] == [] and spies["open"] == 0 and spies["replace"] == []
    assert app.session.connection is app.old_connection and not is_closed(app.old_connection)
    assert app.window.overview is app.old_overview and references_to(app.window, app.old_services)
    assert snapshot(app.old_connection) == app.current
    assert app.window.isEnabled() and app.exits == [] and app.errors == []


# S3.4, S3.5, S3.11: успіх --------------------------------------------------------------------


def test_successful_restore_adopts_the_returned_connection(app, spies, confirmed, previous_hook):
    before = {b.path.name for b in find_backups(app.paths.backups)}
    app.restore()
    # Саме вибрана копія, один виклик; сесія взяла саме повернуте з'єднання.
    assert spies["restore"] == [app.backup]
    (returned,) = spies["returned"]
    assert spies["adopt"] == [returned] and app.session.connection is returned
    assert is_closed(app.old_connection)  # старе закрито звичайним close() перед відновленням
    assert spies["corruption_close"] == 0  # база здорова: не закриття після пошкодження
    # Новий граф на цьому з'єднанні, і саме його отримало вікно; старий недосяжний.
    (created,) = spies["create"]
    assert created[0] is returned and spies["replace"] == [created[1]]
    assert references_to(app.window, created[1])
    flush_deletions()
    assert references_to(app.window, app.old_services) == []
    # Рівно одне постійне з'єднання з робочою базою.
    assert open_to_database(spies, app) == [returned]
    # Точний стан копії; перед відновленням створено копію поточного стану (DS-5).
    assert snapshot(returned) == app.expected
    created_backups = {b for b in find_backups(app.paths.backups) if b.path.name not in before}
    assert BackupKind.BEFORE_RESTORE in {b.kind for b in created_backups}
    # Без переходу між місяцями й діалогу тривалої перерви; повторного відкриття немає.
    assert spies["transitions"] == 0 and spies["long_gap"] == [] and spies["open"] == 0
    assert app.window.isEnabled() and app.exits == [] and app.errors == []
    assert previous_hook == [] and no_recovery(app)


def test_service_screen_works_on_the_new_graph_after_restore(app, spies, confirmed):
    app.restore()
    (returned,) = spies["returned"]
    app.window.navigate("service")
    page = app.window.service
    assert BackupKind.BEFORE_RESTORE in {c.backup.kind for c in page.candidates}
    app.clock.set(OCTOBER + timedelta(hours=1))
    page.create_button.click()  # копія на вимогу з нового графа
    assert page.failure.isHidden()
    assert page.candidates[0].backup.kind is BackupKind.ON_DEMAND
    app.window.navigate("overview")
    app.window.refresh()
    assert open_to_database(spies, app) == [returned]


# S3.6, S3.9: невдача з успішним відкатом → повторне відкриття ---------------------------------


@pytest.mark.parametrize("stage", ["integrity", "invalid backup"])
def test_failed_restore_reopens_current_data_and_keeps_working(
    app, spies, confirmed, previous_hook, monkeypatch, caplog, stage
):
    if stage == "integrity":
        fail_check_once(monkeypatch)  # після заміни файлів → відкат
    else:
        app.select(app.backup)  # кандидат показано…
        app.backup.write_bytes(b"not a database" * 600)  # …а потім копія зіпсувалася
    with caplog.at_level(logging.INFO):
        page = app.window.service
        page.restore_button.click()
    (error,) = spies["raised"]
    assert isinstance(error, RestoreError) and error.user_message != ROLLBACK_FAILED_MESSAGE
    # Повторне відкриття через сесію — рівно один раз; новий граф на новому з'єднанні.
    assert spies["open"] == 1 and spies["adopt"] == []
    reopened = app.session.connection
    assert reopened is not app.old_connection and is_closed(app.old_connection)
    assert spies["corruption_close"] == 0
    (created,) = spies["create"]
    assert created[0] is reopened and spies["replace"] == [created[1]]
    flush_deletions()
    assert references_to(app.window, app.old_services) == []
    assert open_to_database(spies, app) == [reopened]
    # Дані — попередні: відновлення не відбулося; без переходу між місяцями.
    assert snapshot(reopened) == app.current
    assert spies["transitions"] == 0 and spies["long_gap"] == []
    # Причина показана на екрані «Сервіс» уже після побудови нового графа; без хибного успіху.
    page = app.window.service
    assert app.window.current_route() == "service" and not page.failure.isHidden()
    assert page.failure.title.text() == "Помилка"
    assert page.failure.body.text() == error.user_message
    assert app.window.isEnabled() and app.exits == [] and app.errors == []
    assert previous_hook == [] and no_recovery(app)
    # Застосунок працює: звичайна дія через новий граф і копія на вимогу.
    created[1].incomes.create("Після невдалого відновлення", None, Money(100))
    app.window.refresh()
    app.clock.set(OCTOBER + timedelta(hours=1))
    page.create_button.click()
    assert page.failure.isHidden() and page.candidates[0].backup.kind is BackupKind.ON_DEMAND


# S3.7: невдалий відкат → завершення ------------------------------------------------------------


def test_rollback_failure_is_terminal_without_reopening(
    app, spies, confirmed, previous_hook, monkeypatch
):
    fail_check_once(monkeypatch)
    original_discard = backup_service_module.discard_database

    def discard(path):
        if path == app.paths.database:
            raise PermissionError(errno.EACCES, "відновлена база зайнята")
        original_discard(path)

    monkeypatch.setattr(backup_service_module, "discard_database", discard)
    app.restore()
    (error,) = spies["raised"]
    assert error.user_message == ROLLBACK_FAILED_MESSAGE
    assert app.handler.failure is error
    assert isinstance(error.__cause__, RestoreError)  # первинна причина
    assert isinstance(error.__context__.__cause__, PermissionError)  # невдалий відкат
    assert spies["open"] == 0 and spies["replace"] == []  # повторного відкриття немає
    assert app.exits == [EXIT_DATA_CORRUPTED]
    assert app.errors == [f"{ROLLBACK_FAILED_MESSAGE} {APPLICATION_WILL_CLOSE}"]
    assert "Резервні копії" not in app.errors[0] and "не змінено" not in app.errors[0]
    assert not app.window.isEnabled() and not app.session.is_open
    assert open_to_database(spies, app) == []
    assert previous_hook == [] and no_recovery(app)
    # Повторної спроби в цьому процесі немає.
    assert app.handler(app.window.service.candidates[0]) is None
    assert len(spies["restore"]) == 1


# S3.8: невдале повторне відкриття → завершення -------------------------------------------------


def test_reopen_failure_is_terminal_and_keeps_both_errors(
    app, spies, confirmed, previous_hook, monkeypatch
):
    fail_check_once(monkeypatch)
    original_open = app_module.open_application_database
    calls = []

    def open_application_database(paths, clock):
        calls.append(True)
        if len(calls) == 2:  # перше — у restore_and_open, друге — повторне відкриття сесії
            raise StorageError(detail="повторне відкриття не вдалося (введено тестом)")
        return original_open(paths, clock)

    monkeypatch.setattr(app_module, "open_application_database", open_application_database)
    app.restore()
    (primary,) = spies["raised"]
    failure = app.handler.failure
    assert isinstance(failure, StorageError) and "повторне відкриття" in failure.detail
    assert failure.__context__ is primary  # первинна RestoreError збережена
    assert spies["open"] == 1 and spies["replace"] == []
    assert app.exits == [EXIT_DATA_CORRUPTED]
    assert app.errors == [MANUAL_RESTORE_REOPEN_FAILED_MESSAGE]
    assert not app.window.isEnabled() and not app.session.is_open
    assert open_to_database(spies, app) == []
    assert previous_hook == [] and no_recovery(app)
    assert app.handler(app.window.service.candidates[0]) is None  # без повторної спроби
    assert len(spies["restore"]) == 1


def test_failure_after_successful_restore_is_terminal(
    app, spies, confirmed, previous_hook, monkeypatch
):
    def broken(window, services):
        raise RuntimeError("введена невдача: replace_services")

    monkeypatch.setattr(MainWindow, "replace_services", broken)
    app.restore()
    (returned,) = spies["returned"]
    assert is_closed(returned) and not app.session.is_open
    assert app.exits == [EXIT_DATA_CORRUPTED] and app.errors == [RESTORE_INCOMPLETE_MESSAGE]
    assert not app.window.isEnabled() and previous_hook == [] and no_recovery(app)


# S3.9: два відновлення одночасно неможливі ----------------------------------------------------


def test_second_restore_cannot_start_while_one_is_running(app, spies, confirmed, monkeypatch):
    attempts = {}
    original_restore = app_module.restore_and_open

    def restore_and_open(paths, clock, backup_path):
        page = app.window.service
        attempts["window_enabled"] = app.window.isEnabled()
        page.restore_button.click()  # вимкнене вікно: натискання нічого не робить
        attempts["direct"] = app.handler(page.candidates[0])  # повторний виклик ігнорується
        return original_restore(paths, clock, backup_path)

    monkeypatch.setattr(app_module, "restore_and_open", restore_and_open)
    app.restore()
    assert attempts == {"window_enabled": False, "direct": None}
    assert len(spies["adopt"]) == 1 and len(spies["replace"]) == 1
    assert app.window.isEnabled() and app.exits == []


# S3.10: межа архітектури -----------------------------------------------------------------------


def test_service_page_gets_no_session_connection_or_storage():
    parameters = list(inspect.signature(ServicePage.__init__).parameters)
    assert parameters == ["self", "backups", "product_name"]
    tree = ast.parse(inspect.getsource(service_module))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
    forbidden = ("sqlite3", "budget.storage", "budget.app", "ApplicationSession", "RecoveryService")
    assert not [name for name in imported if any(f in name for f in forbidden)]
