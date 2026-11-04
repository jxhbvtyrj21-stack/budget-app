"""Порожній стан «Огляду» (IA 12): після налаштування немає жодного фінансового запису."""

import sqlite3
from datetime import UTC, datetime

import pytest
from PySide6.QtWidgets import QLabel, QPushButton

from budget.app import open_application_database
from budget.domain.calendar import FixedClock
from budget.domain.models import AccumulationStatus, SourceKind, SourceRef
from budget.domain.money import Money
from budget.platform.paths import DataPaths
from budget.services.facade import AppServices
from budget.services.month import MonthTransitionService
from budget.services.setup import InitialAccumulation, SetupDraft
from budget.ui.formatting import format_money
from budget.ui.main_window import MainWindow
from budget.ui.screens.overview import START_WITH_INCOME


@pytest.fixture
def clock():
    return FixedClock(datetime(2026, 10, 6, 9, 0, tzinfo=UTC))


@pytest.fixture
def connection(tmp_path, clock):
    connection = open_application_database(DataPaths(tmp_path / "data"), clock)
    yield connection
    connection.close()


def complete(connection, clock, draft=None):
    services = AppServices.create(connection, clock)
    services.setup.complete(draft or SetupDraft())
    return services


def plain(text: str) -> str:
    return text.replace(" ", " ").replace(" ", " ")


def overview_texts(window) -> list[str]:
    return [plain(label.text()) for label in window.overview.findChildren(QLabel)]


def new_income_buttons(window) -> list[QPushButton]:
    return [b for b in window.overview.findChildren(QPushButton) if b.text() == "Новий дохід"]


def open_window(qtbot, services):
    window = MainWindow("Budget", services)
    qtbot.addWidget(window)
    return window


def test_no_financial_records_shows_empty_state(qtbot, connection, clock, monkeypatch):
    # Стартовий стан майстра (залишок і накопичення) фінансовим записом не є.
    services = complete(
        connection,
        clock,
        SetupDraft(
            general_remainder=Money(100_000),
            accumulations=(InitialAccumulation("Подорож", None, Money(30_000)),),
        ),
    )
    connects, transitions = [], []
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: connects.append(a))
    monkeypatch.setattr(
        MonthTransitionService, "run_on_startup", lambda self: transitions.append(self)
    )
    opened = []
    monkeypatch.setattr(MainWindow, "open_income_dialog", lambda self: opened.append(self))
    window = open_window(qtbot, services)
    assert not window.overview.empty_state.isHidden()
    assert START_WITH_INCOME in overview_texts(window)
    # Кнопка «Новий дохід» — одна, у заголовку екрана, і веде до форми доходу.
    assert new_income_buttons(window) == [window.overview.new_income_button]
    window.overview.new_income_button.click()
    assert opened == [window]
    assert connects == [] and transitions == []


def test_any_financial_record_hides_empty_state(qtbot, connection, clock):
    services = complete(connection, clock)
    window = open_window(qtbot, services)
    assert not window.overview.empty_state.isHidden()
    services.incomes.create("Зарплата", None, Money(5_000))
    window.refresh()
    assert window.overview.empty_state.isHidden()
    assert len(new_income_buttons(window)) == 1


def test_past_records_with_zero_balance_are_not_empty(qtbot, connection, clock):
    """Записи минулого місяця, нульовий баланс і порожній поточний місяць — не порожній стан."""
    services = complete(connection, clock)
    income = services.incomes.create("Жовтень", None, Money(5_000)).income
    services.expenses.create(
        "Усе", None, Money(5_000), SourceRef(SourceKind.INCOME, income_id=income.id)
    )
    clock.set(datetime(2026, 11, 3, 9, 0, tzinfo=UTC))
    services = AppServices.create(connection, clock)
    services.transitions.run_on_startup()
    current = services.months.current_month()
    assert services.balances.available_funds().total == Money.zero()
    assert services.incomes.list_for_month(current) == []
    assert services.expenses.list_for_month(current) == []
    window = open_window(qtbot, services)
    assert window.overview.empty_state.isHidden()


def test_empty_state_keeps_archive_line(qtbot, connection, clock):
    """Рядок «з них в архіві» (етап A) лишається незалежним від порожнього стану."""
    services = complete(connection, clock)
    acc_id = services.accumulations.create("Стара ціль", None, None).accumulation.id
    services.accumulations.change_status(acc_id, AccumulationStatus.CLOSED)
    services.accumulations.archive(acc_id)
    archived = sum((v.balance for v in services.accumulations.list_archived()), Money.zero())
    window = open_window(qtbot, services)
    texts = overview_texts(window)
    assert not window.overview.empty_state.isHidden()
    assert f"з них в архіві: {plain(format_money(archived))}" in texts
