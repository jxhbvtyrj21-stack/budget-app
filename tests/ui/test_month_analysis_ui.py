"""Місяць: «Підсумки місяця» з базовим мінімумом і «Рух накопичень» (ADR 0020, ADR 0022)."""

from datetime import UTC, datetime

import pytest
from PySide6.QtWidgets import QLabel

from budget.app import open_application_database
from budget.domain.calendar import FixedClock
from budget.domain.models import ReplenishmentPart, SourceKind, SourceRef
from budget.domain.money import Money
from budget.platform.paths import DataPaths
from budget.services.facade import AppServices
from budget.services.setup import InitialAccumulation, InitialDebt, SetupDraft
from budget.ui.dialogs.base_minimum_dialog import BaseMinimumDialog
from budget.ui.main_window import MainWindow

REMAINDER = SourceRef(SourceKind.GENERAL_REMAINDER)


@pytest.fixture
def clock():
    return FixedClock(datetime(2026, 10, 6, 9, 0, tzinfo=UTC))


@pytest.fixture
def services(tmp_path, clock):
    connection = open_application_database(DataPaths(tmp_path / "data"), clock)
    services = AppServices.create(connection, clock)
    services.setup.complete(
        SetupDraft(
            general_remainder=Money(1_000_000),
            accumulations=(InitialAccumulation("Подорож", None, Money(500_000)),),
            debts=(InitialDebt("Позика", None, Money(100_000)),),
        )
    )
    yield services
    connection.close()


def plain(text: str) -> str:
    return text.replace(" ", " ").replace(" ", " ")


def texts(widget) -> set[str]:
    return {plain(label.text()) for label in widget.findChildren(QLabel)}


def window(qtbot, services) -> MainWindow:
    main = MainWindow("Budget", services)
    qtbot.addWidget(main)
    return main


def populate(services):
    trip = services.accumulations.list_working()[0].accumulation.id
    acc = SourceRef(SourceKind.ACCUMULATION, accumulation_id=trip)
    debt = services.debts.list_active()[0].debt.id
    services.expenses.create("Продукти", None, Money(300_000), REMAINDER)
    services.expenses.create("Квитки", None, Money(100_000), acc)
    services.replenishments.create(
        "Відкладаю", None, trip, [ReplenishmentPart(REMAINDER, Money(200_000))]
    )
    services.debts.receive_loan("Картка", None, Money(50_000))
    services.debts.repay(debt, Money(30_000), acc)


def test_summary_amounts_and_breakdown(qtbot, services):
    populate(services)
    page = window(qtbot, services).month
    amounts = {k: plain(v.text()) for k, v in page.summary_amounts.items()}
    assert amounts == {
        "Доходи місяця": "0",
        "Фактичні витрати": "4 000",  # без поповнення й погашення
        "Поповнення накопичень": "2 000",
        "Погашення боргів": "300",
        "Отримані позикові кошти": "500",
    }
    assert plain(page.breakdown.text()) == (
        "Фактичні витрати за джерелом: з доходів 0 · із загального нерозподіленого залишку "
        "3 000 · з накопичень 1 000"
    )


def test_movement_includes_repayment_from_accumulation(qtbot, services):
    populate(services)
    page = window(qtbot, services).month
    row = page.movement_rows.itemAt(0).widget()
    labels = texts(row)
    assert "Подорож" in labels
    assert "Поповнення +2 000 · витрати −1 000 · погашення боргів −300 · чиста зміна" in labels
    assert "700" in labels


def test_base_minimum_absent_set_and_change(qtbot, services, monkeypatch):
    page = window(qtbot, services).month
    assert plain(page.base_minimum_label.text()) == "Базовий мінімум не задано"
    assert page.base_minimum_button.text() == "Задати"
    assert page.comparison_label.isHidden()

    def save_value(self):
        self.amount.field.setText("3 000")
        self.save()
        return 1

    monkeypatch.setattr(BaseMinimumDialog, "exec", save_value)
    page.base_minimum_button.click()
    assert plain(page.base_minimum_label.text()) == "3 000"
    assert page.base_minimum_button.text() == "Змінити"
    assert (
        plain(page.comparison_label.text()) == "Фактичні витрати на 3 000 менші за базовий мінімум"
    )
    assert services.balances.general_remainder() == Money(1_000_000)


@pytest.mark.parametrize(
    ("minimum", "text"),
    [
        (300_000, "Фактичні витрати дорівнюють базовому мінімуму"),
        (100_000, "Фактичні витрати на 2 000 більші за базовий мінімум"),
        (0, "Фактичні витрати на 3 000 більші за базовий мінімум"),
    ],
)
def test_comparison_texts(qtbot, services, minimum, text):
    services.expenses.create("Продукти", None, Money(300_000), REMAINDER)
    services.base_minimums.set(services.months.current_month(), Money(minimum))
    page = window(qtbot, services).month
    assert plain(page.comparison_label.text()) == text
    assert page.comparison_label.objectName() not in ("Notice", "ErrorNotice")


def test_past_month_summary_has_no_actions(qtbot, services, clock):
    services.base_minimums.set(services.months.current_month(), Money(200_000))
    clock.set(datetime(2026, 11, 3, 9, 0, tzinfo=UTC))
    page = window(qtbot, services).month
    assert page.base_minimum_button.text() == "Задати"  # листопад — поточний, не задано
    page.previous_button.click()
    assert page.base_minimum_button is None
    assert plain(page.base_minimum_label.text()) == "2 000"


def test_empty_movement_message(qtbot, services):
    page = window(qtbot, services).month
    assert plain(page.movement_rows.itemAt(0).widget().text()) == (
        "У цьому місяці накопичення не змінювалися."
    )


# Огляд: «Поточний місяць» ----------------------------------------------------------------


def test_overview_current_month_section(qtbot, services, monkeypatch):
    populate(services)
    main = window(qtbot, services)
    overview = main.overview
    amounts = {k: plain(v.text()) for k, v in overview.month_amounts.items()}
    assert amounts == {"Доходи місяця": "0", "Фактичні витрати": "4 000"}
    assert plain(overview.base_minimum_label.text()) == "не задано"
    assert overview.comparison_label.isHidden()
    total_before = plain(overview.total_label.text())

    def save_value(self):
        self.amount.field.setText("4 000")
        self.save()
        return 1

    monkeypatch.setattr(BaseMinimumDialog, "exec", save_value)
    overview.base_minimum_button.click()
    assert plain(overview.base_minimum_label.text()) == "4 000"
    assert overview.base_minimum_button.text() == "Змінити"
    assert (
        plain(overview.comparison_label.text()) == "Фактичні витрати дорівнюють базовому мінімуму"
    )
    # Базовий мінімум не є фінансовою сумою: загальна доступна сума та сама.
    assert plain(overview.total_label.text()) == total_before
    # Місяць оновлено разом з Оглядом.
    assert plain(main.month.base_minimum_label.text()) == "4 000"


def test_overview_open_month_link(qtbot, services):
    main = window(qtbot, services)
    main.month.show_month(main.month.month.previous())
    (link,) = [
        b
        for b in main.overview.findChildren(type(main.overview.base_minimum_button))
        if b.text() == "Відкрити місяць"
    ]
    link.click()
    assert main.current_route() == "month"
    assert main.month.is_current()
