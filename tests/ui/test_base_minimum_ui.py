"""Форма базового мінімуму й текст порівняння (ADR 0002, ADR 0020, ADR 0022)."""

from datetime import UTC, datetime

import pytest

from budget.app import open_application_database
from budget.domain.calendar import CalendarMonth, FixedClock
from budget.domain.models import compare_with_base_minimum
from budget.domain.money import Money
from budget.platform.paths import DataPaths
from budget.services.facade import AppServices
from budget.services.setup import SetupDraft
from budget.ui.dialogs.base_minimum_dialog import BaseMinimumDialog
from budget.ui.messages import comparison_text

OCTOBER = CalendarMonth(2026, 10)


@pytest.fixture
def clock():
    return FixedClock(datetime(2026, 10, 6, 9, 0, tzinfo=UTC))


@pytest.fixture
def services(tmp_path, clock):
    connection = open_application_database(DataPaths(tmp_path / "data"), clock)
    services = AppServices.create(connection, clock)
    services.setup.complete(SetupDraft(general_remainder=Money(1_000_000)))
    yield services
    connection.close()


def plain(text: str) -> str:
    return text.replace(" ", " ").replace(" ", " ")


def test_set_then_change_title_and_save(qtbot, services):
    dialog = BaseMinimumDialog(services.base_minimums, OCTOBER)
    qtbot.addWidget(dialog)
    assert dialog.windowTitle() == "Задати базовий мінімум"
    dialog.amount.field.setText("30 000")
    dialog.save()
    assert services.base_minimums.get(OCTOBER) == Money(3_000_000)
    again = BaseMinimumDialog(services.base_minimums, OCTOBER)
    qtbot.addWidget(again)
    assert again.windowTitle() == "Змінити базовий мінімум"
    assert plain(again.amount.field.text()) == "30 000"
    # Збереження не змінює залишків і не створює фінансового запису.
    assert services.balances.general_remainder() == Money(1_000_000)


def test_zero_valid_negative_and_empty_rejected(qtbot, services):
    dialog = BaseMinimumDialog(services.base_minimums, OCTOBER)
    qtbot.addWidget(dialog)
    dialog.amount.field.setText("-5")
    dialog.save()
    assert dialog.amount.error.isVisibleTo(dialog)
    dialog.amount.field.setText("")
    dialog.save()
    assert dialog.amount.error.text() == "Вкажіть суму."
    dialog.amount.field.setText("0")
    dialog.save()
    assert services.base_minimums.get(OCTOBER) == Money.zero()


def test_past_month_rejected_without_change(qtbot, services, clock):
    clock.set(datetime(2026, 11, 2, 9, 0, tzinfo=UTC))
    dialog = BaseMinimumDialog(services.base_minimums, OCTOBER)
    qtbot.addWidget(dialog)
    dialog.amount.field.setText("100")
    dialog.save()
    assert not dialog.failure.isHidden()
    assert services.base_minimums.get(OCTOBER) is None


@pytest.mark.parametrize(
    ("actual", "minimum", "text"),
    [
        (3_200_000, 3_000_000, "Фактичні витрати на 2 000 більші за базовий мінімум"),
        (2_800_000, 3_000_000, "Фактичні витрати на 2 000 менші за базовий мінімум"),
        (3_000_000, 3_000_000, "Фактичні витрати дорівнюють базовому мінімуму"),
    ],
)
def test_comparison_text(qtbot, actual, minimum, text):
    comparison = compare_with_base_minimum(Money(actual), Money(minimum))
    assert plain(comparison_text(comparison)) == text
