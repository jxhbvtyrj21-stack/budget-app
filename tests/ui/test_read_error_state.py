"""Стан екрана «Помилка» під час читання даних (IA 12, E2).

Звичайна помилка читання (``sqlite3.OperationalError`` без пошкодження) на вже
побудованому екрані — «Помилка» · «Не вдалося прочитати дані.» · «Сервіс» замість
вмісту. Пошкодження бази (той самий об'єкт винятку) межа не чіпає: воно доходить до
``RuntimeCorruptionGuard``. Читання під час побудови сторінок (створення вікна,
``replace_services``, запуск, відновлення) межею не загортаються — як до E2.

Невдачі вносяться в метод сервісу (атрибут класу), не в конструктори.
"""

import gc
import logging
import sqlite3
import sys
import weakref
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QTimer
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

import budget.ui.components.read_error as read_error_module
from budget.app import (
    EXIT_DATA_CORRUPTED,
    RESTORE_INCOMPLETE_MESSAGE,
    ApplicationSession,
    ManualRestore,
    RuntimeCorruptionGuard,
    RuntimeRecoveryFlow,
    open_application_database,
    resume_after_runtime_restore,
    show_window_or_report,
)
from budget.domain.calendar import FixedClock
from budget.domain.models import AccumulationStatus
from budget.domain.money import Money
from budget.platform.identity import load_product_identity
from budget.platform.paths import DataPaths
from budget.services.accumulation import AccumulationService
from budget.services.backup import BackupKind, RecoveryService
from budget.services.balances import BalanceService
from budget.services.debt import DebtService
from budget.services.facade import AppServices
from budget.services.month_analysis import MonthAnalysisService
from budget.services.setup import InitialAccumulation, InitialDebt, SetupDraft
from budget.ui.components.read_error import (
    READ_ERROR_TEXT,
    READ_ERROR_TITLE,
    SERVICE_BUTTON,
    ReadErrorState,
)
from budget.ui.dialogs.long_gap_dialog import LongGapDialog
from budget.ui.dialogs.recovery_dialog import RecoveryDialog
from budget.ui.main_window import MainWindow

START = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
BOUNDARY_LOGGER = read_error_module.log.name


def with_code(cls, code: int, message: str = "введена помилка") -> sqlite3.Error:
    error = cls(message)
    error.sqlite_errorcode = code
    return error


def ordinary() -> sqlite3.Error:
    return with_code(sqlite3.OperationalError, sqlite3.SQLITE_BUSY, "database is locked")


def corruption(code: int = sqlite3.SQLITE_CORRUPT) -> sqlite3.Error:
    return with_code(sqlite3.DatabaseError, code, "database disk image is malformed")


def make_services(tmp_path, clock, name: str = "data"):
    paths = DataPaths(tmp_path / name)
    connection = open_application_database(paths, clock)
    services = AppServices.create(connection, clock, paths.backups)
    services.setup.complete(
        SetupDraft(
            general_remainder=Money(100_000),
            accumulations=(InitialAccumulation("Подорож", None, Money(30_000)),),
            debts=(InitialDebt("Позика", None, Money(15_000)),),
        )
    )
    old = services.accumulations.create("Стара ціль", None, None).accumulation.id
    services.accumulations.change_status(old, AccumulationStatus.CLOSED)
    services.accumulations.archive(old)
    services.incomes.create("Зарплата", None, Money(50_000))
    return services, connection


@pytest.fixture
def clock():
    return FixedClock(START)


@pytest.fixture
def services(tmp_path, clock):
    services, connection = make_services(tmp_path, clock)
    yield services
    connection.close()


@pytest.fixture
def window(qtbot, services):
    widget = MainWindow("Budget", services)
    qtbot.addWidget(widget)
    widget.show()
    return widget


@pytest.fixture
def previous_hook(monkeypatch):
    """Попередній ``sys.excepthook``: усе, що вийшло зі слоту повз межу й guard."""
    calls = []
    monkeypatch.setattr(sys, "excepthook", lambda *args: calls.append(args[1]))
    return calls


class Injected:
    """Метод сервісу, що кидає заданий виняток, поки ``error`` не ``None``."""

    def __init__(self, monkeypatch, target, method: str) -> None:
        self.error: BaseException | None = None
        self.calls = 0
        original = getattr(target, method)

        def wrapper(service, *args, **kwargs):
            self.calls += 1
            if self.error is not None:
                raise self.error
            return original(service, *args, **kwargs)

        monkeypatch.setattr(target, method, wrapper)


@dataclass(frozen=True)
class Case:
    target: type
    method: str
    trigger: object  # (window, services) -> None
    boundary: object  # window -> ReadBoundary
    prepare: object = None  # (window, services) -> None
    direct: bool = True  # виняток повертається тому, хто викликав (не через сигнал)


def open_accumulation(window, services):
    window.accumulations.list_page.rows[0].open_button.click()
    assert window.accumulations.showing_detail()


def open_debt(window, services):
    window.debts.list_page.rows[0].open_button.click()
    assert window.debts.showing_detail()


def overview(w):
    return w.overview.reads


def month(w):
    return w.month.reads


def accumulation_list(w):
    return w.accumulations.list_page.reads


def accumulation_card(w):
    return w.accumulations.detail_page.reads


def debt_list(w):
    return w.debts.list_page.reads


def debt_card(w):
    return w.debts.detail_page.reads


CASES = {
    "overview.refresh": Case(
        BalanceService, "available_funds", lambda w, s: w.overview.refresh(), overview
    ),
    "overview.window_refresh": Case(
        BalanceService, "available_funds", lambda w, s: w.refresh(), overview
    ),
    "month.refresh": Case(MonthAnalysisService, "analyse", lambda w, s: w.month.refresh(), month),
    "month.show_month": Case(
        MonthAnalysisService,
        "analyse",
        lambda w, s: w.month.show_month(s.months.current_month()),
        month,
    ),
    "accumulations.list_refresh": Case(
        AccumulationService,
        "list_working",
        lambda w, s: w.accumulations.refresh(),
        accumulation_list,
    ),
    "accumulations.archive_switch": Case(
        AccumulationService,
        "list_archived",
        lambda w, s: w.accumulations.list_page.archive_button.click(),
        accumulation_list,
        direct=False,
    ),
    "accumulations.work_list_switch": Case(
        AccumulationService,
        "list_working",
        lambda w, s: w.accumulations.list_page.working_button.click(),
        accumulation_list,
        prepare=lambda w, s: w.accumulations.list_page.archive_button.click(),
        direct=False,
    ),
    "accumulations.card_open": Case(
        AccumulationService,
        "get",
        lambda w, s: w.accumulations.list_page.rows[0].open_button.click(),
        accumulation_card,
        direct=False,
    ),
    "accumulations.card_refresh": Case(
        AccumulationService,
        "get",
        lambda w, s: w.accumulations.refresh(),
        accumulation_card,
        prepare=open_accumulation,
    ),
    "accumulations.return_to_list": Case(
        AccumulationService,
        "list_working",
        lambda w, s: w.accumulations.detail_page.back_button.click(),
        accumulation_list,
        prepare=open_accumulation,
        direct=False,
    ),
    "debts.list_refresh": Case(
        DebtService, "list_active", lambda w, s: w.debts.refresh(), debt_list
    ),
    "debts.card_open": Case(
        DebtService,
        "get",
        lambda w, s: w.debts.list_page.rows[0].open_button.click(),
        debt_card,
        direct=False,
    ),
    "debts.card_refresh": Case(
        DebtService, "get", lambda w, s: w.debts.refresh(), debt_card, prepare=open_debt
    ),
    "debts.return_to_list": Case(
        DebtService,
        "list_active",
        lambda w, s: w.debts.detail_page.back_button.click(),
        debt_list,
        prepare=open_debt,
        direct=False,
    ),
}
DIRECT = [name for name, case in CASES.items() if case.direct]


def boundary_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == BOUNDARY_LOGGER]


def error_texts(boundary) -> list[str]:
    return [label.text() for label in boundary.error_state.findChildren(QLabel)]


def assert_error_state(boundary) -> None:
    assert boundary.failed
    assert boundary.content.isHidden() and not boundary.error_state.isHidden()
    assert error_texts(boundary) == [READ_ERROR_TITLE, READ_ERROR_TEXT]
    assert [b.text() for b in boundary.error_state.findChildren(QPushButton)] == [SERVICE_BUTTON]


def assert_content_state(boundary) -> None:
    assert not boundary.failed
    assert not boundary.content.isHidden() and boundary.error_state.isHidden()


def visible_error_states(window) -> list[ReadErrorState]:
    return [w for w in window.findChildren(ReadErrorState) if not w.isHidden()]


def arm(monkeypatch, window, services, case: Case) -> Injected:
    if case.prepare is not None:
        case.prepare(window, services)
    return Injected(monkeypatch, case.target, case.method)


# B. Звичайна помилка читання — стан «Помилка» на кожному екрані ------------------------------


@pytest.mark.parametrize("name", list(CASES))
def test_ordinary_read_failure_shows_error_state(
    name, window, services, monkeypatch, previous_hook, caplog
):
    case = CASES[name]
    injected = arm(monkeypatch, window, services, case)
    assert_content_state(case.boundary(window))
    injected.error = ordinary()
    with caplog.at_level(logging.INFO):
        case.trigger(window, services)
    assert_error_state(case.boundary(window))
    assert previous_hook == []  # нічого не вийшло зі слоту
    (record,) = boundary_records(caplog)  # один запис у журнал
    assert record.levelno == logging.ERROR and record.exc_info[1].__cause__ is not None


@pytest.mark.parametrize("name", list(CASES))
def test_next_successful_read_restores_the_content(
    name, window, services, monkeypatch, previous_hook
):
    case = CASES[name]
    injected = arm(monkeypatch, window, services, case)
    injected.error = ordinary()
    case.trigger(window, services)
    assert_error_state(case.boundary(window))
    injected.error = None
    case.trigger(window, services)  # той самий шлях, без таймерів і повторів
    assert_content_state(case.boundary(window))
    assert previous_hook == []


@pytest.mark.parametrize("name", list(CASES))
def test_service_button_opens_service_without_rereading(
    name, window, services, monkeypatch, previous_hook
):
    case = CASES[name]
    injected = arm(monkeypatch, window, services, case)
    injected.error = ordinary()
    case.trigger(window, services)
    calls = injected.calls
    case.boundary(window).error_state.service_button.click()
    assert window.current_route() == "service"
    assert injected.calls == calls  # перехід не перечитує проблемний екран
    assert case.boundary(window).failed and previous_hook == []


def test_failed_card_does_not_keep_the_previous_card_title(window, services, monkeypatch):
    open_accumulation(window, services)
    detail = window.accumulations.detail_page
    assert detail.title_label.text() == "Подорож"
    detail.back_button.click()
    Injected(monkeypatch, AccumulationService, "get").error = ordinary()
    window.accumulations.list_page.rows[0].open_button.click()
    assert detail.reads.failed and detail.view is None
    assert detail.title_label.text() == "" and detail.breadcrumb.text() == ""


# C, D. Пошкодження: межа не чіпає, той самий об'єкт доходить до guard ----------------------


@pytest.mark.parametrize(
    "code",
    [sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB, sqlite3.SQLITE_CORRUPT_INDEX],
    ids=["CORRUPT", "NOTADB", "CORRUPT_INDEX"],
)
@pytest.mark.parametrize("name", list(CASES))
def test_corruption_reaches_the_guard_as_the_same_object(
    name, code, qtbot, window, services, monkeypatch, previous_hook, caplog
):
    case = CASES[name]
    injected = arm(monkeypatch, window, services, case)
    error = injected.error = corruption(code)
    recoveries = []
    with caplog.at_level(logging.INFO), RuntimeCorruptionGuard(recoveries.append):
        QTimer.singleShot(0, lambda: case.trigger(window, services))  # звичайний слот
        qtbot.waitUntil(lambda: bool(recoveries), timeout=5000)
    assert len(recoveries) == 1 and recoveries[0] is error
    assert not case.boundary(window).failed  # не стан «Помилка»
    assert previous_hook == [] and boundary_records(caplog) == []


@pytest.mark.parametrize("name", DIRECT)
def test_boundary_reraises_corruption_unchanged(name, window, services, monkeypatch, caplog):
    case = CASES[name]
    injected = arm(monkeypatch, window, services, case)
    error = injected.error = corruption()
    with caplog.at_level(logging.INFO), pytest.raises(sqlite3.DatabaseError) as raised:
        case.trigger(window, services)
    assert raised.value is error  # тотожність, а не лише тип чи текст
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert not case.boundary(window).failed and boundary_records(caplog) == []


@pytest.mark.parametrize("link", ["__cause__", "__context__"])
def test_corruption_in_the_chain_is_reraised_as_the_same_object(
    link, qtbot, window, services, monkeypatch, previous_hook
):
    """OperationalError, за яким стоїть пошкодження (як невдалий ROLLBACK), — до guard."""
    outer = ordinary()
    setattr(outer, link, corruption())
    Injected(monkeypatch, BalanceService, "available_funds").error = outer
    with pytest.raises(sqlite3.OperationalError) as raised:
        window.overview.refresh()
    assert raised.value is outer and not window.overview.reads.failed
    recoveries = []
    with RuntimeCorruptionGuard(recoveries.append):
        QTimer.singleShot(0, window.refresh)
        qtbot.waitUntil(lambda: bool(recoveries), timeout=5000)
    assert recoveries == [outer] and recoveries[0] is outer and previous_hook == []


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(with_code(sqlite3.DatabaseError, sqlite3.SQLITE_ERROR), id="DatabaseError"),
        pytest.param(sqlite3.ProgrammingError("closed"), id="ProgrammingError"),
        pytest.param(with_code(sqlite3.IntegrityError, 19), id="IntegrityError"),
        pytest.param(RuntimeError("bug"), id="RuntimeError"),
    ],
)
def test_other_errors_are_not_turned_into_the_error_state(error, window, monkeypatch):
    Injected(monkeypatch, BalanceService, "available_funds").error = error
    with pytest.raises(type(error)) as raised:
        window.overview.refresh()
    assert raised.value is error and not window.overview.reads.failed


# F. Справжня пошкоджена база: діалог відновлення, а не стан «Помилка» ----------------------


def corrupt_pages(path) -> None:
    pages = path.stat().st_size // 4096
    with path.open("r+b") as handle:
        for page in range(2, pages):
            handle.seek(page * 4096)
            handle.write(b"\xa5" * 4096)


def test_real_corrupted_database_opens_recovery_not_error_state(
    qtbot, tmp_path, monkeypatch, previous_hook, caplog
):
    clock = FixedClock(START)
    paths = DataPaths(tmp_path / "Users" / "Олена Коваль" / "AppData" / "Local" / "Budget")
    session = ApplicationSession(paths, clock)
    connection = session.open()
    services = AppServices.create(connection, clock, paths.backups)
    services.setup.complete(SetupDraft(general_remainder=Money(100_000)))
    for index in range(150):
        services.incomes.create(f"Дохід {index}", "опис " * 40, Money(100))
    clock.set(START + timedelta(hours=2))
    services.backups.create_backup(BackupKind.ON_DEMAND)
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.execute("PRAGMA cache_size = 0")
    window = MainWindow("Budget", services)
    qtbot.addWidget(window)
    window.show()
    corrupt_pages(paths.database)
    exits, seen = [], []
    flow = RuntimeRecoveryFlow(
        session,
        RecoveryService(paths.database, paths.backups, clock),
        restore=lambda path: pytest.fail("відновлення не вибиралося"),
        on_restored=lambda c: pytest.fail("відновлення не вибиралося"),
        exit_application=exits.append,
        show_error=lambda text: pytest.fail(text),
    )

    def poll():
        dialog = QApplication.activeModalWidget()
        if isinstance(dialog, RecoveryDialog):
            seen.append(dialog)
            dialog.close_button.click()
        else:
            QTimer.singleShot(10, poll)

    QTimer.singleShot(10, poll)
    try:
        with caplog.at_level(logging.INFO), RuntimeCorruptionGuard(flow):
            QTimer.singleShot(0, window.refresh)  # оновлення через межу E2
            qtbot.waitUntil(lambda: bool(exits), timeout=5000)
    finally:
        window.close()
        session.close()
    assert len(seen) == 1 and exits == [EXIT_DATA_CORRUPTED]
    assert not window.overview.reads.failed and boundary_records(caplog) == []
    assert previous_hook == []


# H. Політика (a): побудова сторінок і відновлення — як до E2 ------------------------------


def test_window_construction_is_not_guarded(qtbot, services, monkeypatch):
    error = ordinary()
    Injected(monkeypatch, BalanceService, "available_funds").error = error
    with pytest.raises(sqlite3.OperationalError) as raised:
        MainWindow("Budget", services)
    assert raised.value is error


def test_replace_services_is_not_guarded(tmp_path, clock, window, monkeypatch):
    other, connection = make_services(tmp_path, clock, "other")
    try:
        error = ordinary()
        Injected(monkeypatch, BalanceService, "available_funds").error = error
        with pytest.raises(sqlite3.OperationalError) as raised:
            window.replace_services(other)
        assert raised.value is error
    finally:
        connection.close()


@pytest.fixture
def session_app(tmp_path, clock):
    paths = DataPaths(tmp_path / "Users" / "Олена Коваль" / "AppData" / "Local" / "Budget")
    session = ApplicationSession(paths, clock)
    connection = session.open()
    services = AppServices.create(connection, clock, paths.backups)
    services.setup.complete(SetupDraft(general_remainder=Money(100_000)))
    yield paths, session, services
    if session.is_open:
        session.close()


def test_ordinary_failure_while_building_the_startup_window_is_not_hidden(
    qtbot, session_app, clock, monkeypatch
):
    paths, session, _ = session_app
    error = ordinary()
    Injected(monkeypatch, BalanceService, "available_funds").error = error
    guard = RuntimeCorruptionGuard(lambda e: pytest.fail("без відновлення"))
    with pytest.raises(sqlite3.OperationalError) as raised:
        show_window_or_report(load_product_identity(), session, clock, paths, False, guard)
    assert raised.value is error and not guard.detected


def test_corruption_while_building_the_startup_window_is_detected(
    qtbot, session_app, clock, monkeypatch
):
    paths, session, _ = session_app
    Injected(monkeypatch, BalanceService, "available_funds").error = corruption()
    guard = RuntimeCorruptionGuard(lambda e: pytest.fail("не до циклу подій"))
    window = show_window_or_report(load_product_identity(), session, clock, paths, False, guard)
    assert window is None and guard.detected


@pytest.fixture
def long_gap(tmp_path):
    """Дохід серпня з позитивним залишком, вересень порожній, зараз жовтень — запит на рішення."""
    clock = FixedClock(datetime(2026, 8, 10, 9, 0, tzinfo=UTC))
    paths = DataPaths(tmp_path / "gap")
    connection = open_application_database(paths, clock)
    services = AppServices.create(connection, clock, paths.backups)
    services.setup.complete(SetupDraft())
    services.incomes.create("Серпень", None, Money(5_000))
    clock.set(START)
    services = AppServices.create(connection, clock, paths.backups)
    assert services.transitions.run_on_startup() is not None
    yield services
    connection.close()


def test_long_gap_dialog_at_startup_is_not_guarded(qtbot, long_gap, monkeypatch):
    """``show_main_window`` викликає ``open_long_gap_dialog()`` — як до E2, без межі."""
    window = MainWindow("Budget", long_gap)
    qtbot.addWidget(window)
    monkeypatch.setattr(LongGapDialog, "exec", lambda dialog: 0)
    error = ordinary()
    Injected(monkeypatch, BalanceService, "available_funds").error = error
    with pytest.raises(sqlite3.OperationalError) as raised:
        window.open_long_gap_dialog()
    assert raised.value is error and not window.overview.reads.failed


def test_resolve_button_on_overview_is_guarded(qtbot, long_gap, monkeypatch, previous_hook):
    window = MainWindow("Budget", long_gap)
    qtbot.addWidget(window)
    monkeypatch.setattr(LongGapDialog, "exec", lambda dialog: 0)
    (resolve,) = [b for b in window.overview.findChildren(QPushButton) if b.text() == "Вирішити"]
    Injected(monkeypatch, BalanceService, "available_funds").error = ordinary()
    resolve.click()
    assert_error_state(window.overview.reads)
    assert previous_hook == []


def test_ordinary_failure_after_manual_restore_stays_terminal(
    qtbot, session_app, clock, monkeypatch, previous_hook
):
    """S3: помилка під час ``replace_services`` після відновлення — вихід, як до E2."""
    paths, session, services = session_app
    services.backups.create_backup(BackupKind.ON_DEMAND)
    window = MainWindow("Budget", services)
    qtbot.addWidget(window)
    window.show()
    exits, errors = [], []
    handler = ManualRestore(
        session, paths, clock, window, exit_application=exits.append, show_error=errors.append
    )
    candidate = next(
        c for c in services.backups.candidates() if c.backup.kind is BackupKind.ON_DEMAND
    )
    Injected(monkeypatch, BalanceService, "available_funds").error = ordinary()
    assert handler(candidate) is None
    assert exits == [EXIT_DATA_CORRUPTED] and errors == [RESTORE_INCOMPLETE_MESSAGE]
    assert handler.terminated and not session.is_open and not window.isEnabled()
    assert visible_error_states(window) == [] and previous_hook == []


def test_ordinary_failure_while_resuming_after_runtime_restore_is_not_hidden(
    qtbot, session_app, clock, monkeypatch
):
    paths, session, services = session_app
    window = MainWindow("Budget", services)
    qtbot.addWidget(window)
    window.setEnabled(False)
    guard = RuntimeCorruptionGuard(lambda e: None)
    guard.handle_runtime_corruption(corruption(), schedule=False)
    error = ordinary()
    Injected(monkeypatch, BalanceService, "available_funds").error = error
    with pytest.raises(sqlite3.OperationalError) as raised:
        resume_after_runtime_restore(window, session.connection, clock, paths.backups, guard)
    assert raised.value is error
    assert guard.detected and not window.isEnabled()  # guard не перезаряджено, вікна не активні


# I. Після стану «Помилка»: новий граф працює, виняток не утримується ---------------------


def flush_deletions() -> None:
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def references_to(window: MainWindow, services: AppServices) -> list[str]:
    targets = {id(services)} | {id(getattr(services, name)) for name in services.__slots__}
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


class Probe(sqlite3.OperationalError):
    """Звичайна помилка читання, на яку можна тримати слабке посилання."""


def test_error_state_keeps_no_exception_and_new_graph_works(
    tmp_path, clock, window, services, monkeypatch, previous_hook
):
    quiet = logging.getLogger("e2-test-quiet")
    quiet.propagate = False
    quiet.addHandler(logging.NullHandler())
    monkeypatch.setattr(read_error_module, "log", quiet)  # журнал pytest утримував би запис
    injected = Injected(monkeypatch, BalanceService, "available_funds")
    probe = Probe("database is locked")
    probe.sqlite_errorcode = sqlite3.SQLITE_BUSY
    alive = weakref.ref(probe)
    injected.error = probe
    del probe
    window.overview.refresh()
    assert_error_state(window.overview.reads)
    injected.error = None
    gc.collect()
    assert alive() is None  # ні межа, ні сторінка не тримають виняток

    other, connection = make_services(tmp_path, clock, "restored")
    try:
        window.replace_services(other)
        flush_deletions()
        assert references_to(window, services) == []
        assert_content_state(window.overview.reads)
        window.refresh()
        assert_content_state(window.overview.reads) and previous_hook == []
    finally:
        connection.close()
