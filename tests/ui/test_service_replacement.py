"""Заміна графа сервісів у MainWindow (Block C2): старий граф стає недосяжним з UI."""

import sqlite3
from datetime import UTC, datetime

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QTimer
from PySide6.QtWidgets import QApplication, QDialog

from budget.app import ApplicationSession, open_application_database
from budget.domain.calendar import FixedClock
from budget.domain.money import Money
from budget.platform.paths import DataPaths
from budget.services.facade import AppServices
from budget.services.setup import SetupDraft
from budget.ui.main_window import MainWindow
from budget.ui.screens.overview import OverviewPage
from budget.ui.screens.service import ServicePage
from budget.ui.screens.setup_wizard import SetupWizardPage

CLOCK = FixedClock(datetime(2026, 10, 6, 9, 0, tzinfo=UTC))


def graph(tmp_path, name: str, *, completed: bool = True, remainder: int = 0):
    paths = DataPaths(tmp_path / name)
    connection = open_application_database(paths, CLOCK)
    services = AppServices.create(connection, CLOCK, paths.backups)
    if completed:
        services.setup.complete(SetupDraft(general_remainder=Money(remainder)))
    return services, connection


@pytest.fixture
def services_a(tmp_path):
    services, connection = graph(tmp_path, "a", remainder=111_100)
    yield services
    connection.close()


@pytest.fixture
def services_b(tmp_path):
    services, connection = graph(tmp_path, "b", remainder=222_200)
    yield services
    connection.close()


@pytest.fixture
def window(qtbot, services_a):
    widget = MainWindow("Budget", services_a)
    qtbot.addWidget(widget)
    widget.show()
    return widget


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


def total_text(window: MainWindow) -> str:
    return window.overview.total_label.text().replace("\u00a0", " ").replace("\u202f", " ")


def test_replacement_rebuilds_ui_from_new_services(window, services_a, services_b):
    old_central, old_overview = window.centralWidget(), window.overview
    assert references_to(window, services_a)
    window.replace_services(services_b)
    flush_deletions()
    assert window._services is services_b
    assert window.centralWidget() is not old_central
    assert window.overview is not old_overview
    assert isinstance(window.overview, OverviewPage) and isinstance(window.service, ServicePage)
    assert window.current_route() == "overview"  # безпечний звичайний екран
    # Жодне посилання з активного вікна й сторінок не веде до старого графа.
    assert references_to(window, services_a) == []
    assert references_to(window, services_b)


def test_new_ui_works_with_new_services(window, services_b):
    window.replace_services(services_b)
    flush_deletions()
    assert total_text(window) == "2 222"
    services_b.incomes.create("Після заміни", None, Money(5_000))
    window.refresh()
    assert total_text(window) == "2 272"


def test_replacement_with_incomplete_setup_drops_old_pages(tmp_path, window, services_a):
    """Новий граф без завершеного налаштування: майстер, а старі сторінки не тримаються."""
    services_c, connection = graph(tmp_path, "c", completed=False)
    try:
        window.replace_services(services_c)
        flush_deletions()
        assert isinstance(window.wizard, SetupWizardPage) and window.sidebar is None
        assert window.overview is None and window.month is None
        assert window.accumulations is None and window.debts is None
        assert references_to(window, services_a) == []
    finally:
        window.close()  # майстер зберігає чернетку під час закриття — поки база відкрита
        connection.close()


def test_open_modal_dialog_is_rejected_before_replacement(qtbot, window, services_a, services_b):
    """Діалог зі старими сервісами відкритий під час заміни: його відхилено, і код після
    ``exec()`` не працює зі старим графом."""
    saved = []
    window.overview.changed.connect(lambda: saved.append(1))

    opened = []

    def replace_while_open():
        dialog = QApplication.activeModalWidget()
        opened.append(isinstance(dialog, QDialog) and dialog.isVisible())
        try:
            window.replace_services(services_b)
        finally:
            if QApplication.activeModalWidget() is dialog and dialog is not None:
                dialog.done(QDialog.DialogCode.Rejected.value)  # не зависнути, якщо заміна впала

    QTimer.singleShot(0, replace_while_open)
    # BaseMinimumDialog(services_a.base_minimums, ...).exec() — заміна під час його роботи.
    window.overview.open_base_minimum(services_a.months.current_month())
    flush_deletions()
    assert opened == [True]
    assert saved == []  # діалог відхилено — жодної дії після exec()
    assert QApplication.activeModalWidget() is None
    assert [d for d in window.findChildren(QDialog) if d.isVisible()] == []
    assert references_to(window, services_a) == []
    assert services_a.base_minimums.get(services_a.months.current_month()) is None


def test_replacement_opens_no_connection_and_keeps_session_connection(tmp_path, qtbot, monkeypatch):
    session = ApplicationSession(DataPaths(tmp_path / "s"), CLOCK)
    connection = session.open()
    try:
        first = AppServices.create(connection, CLOCK)
        first.setup.complete(SetupDraft(general_remainder=Money(0)))
        window = MainWindow("Budget", first)
        qtbot.addWidget(window)
        second = AppServices.create(session.connection, CLOCK)
        opened = []
        original = sqlite3.connect
        monkeypatch.setattr(
            sqlite3, "connect", lambda *a, **k: opened.append(a) or original(*a, **k)
        )
        window.replace_services(second)
        flush_deletions()
        assert opened == []  # MainWindow не відкриває баз
        assert session.connection is connection
        assert connection.execute("SELECT 1").fetchone() == (1,)  # і не закриває
    finally:
        session.close()


def test_setup_wizard_still_completes_into_normal_screens(tmp_path, qtbot):
    services, connection = graph(tmp_path, "w", completed=False)
    try:
        window = MainWindow("Budget", services)
        qtbot.addWidget(window)
        assert isinstance(window.wizard, SetupWizardPage) and window.overview is None
        services.setup.complete(SetupDraft(general_remainder=Money(0)))
        window._on_setup_completed()
        assert isinstance(window.overview, OverviewPage) and window.wizard is None
    finally:
        connection.close()
