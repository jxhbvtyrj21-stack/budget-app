"""Інтерфейс звичайних витрат: форма, обмеження джерела, дії рядків, минулі місяці."""

from datetime import UTC, datetime

import pytest
from PySide6.QtWidgets import QDialog, QMessageBox

from budget.app import open_application_database
from budget.domain.calendar import FixedClock
from budget.domain.models import SourceKind, SourceRef
from budget.domain.money import Money
from budget.platform.paths import DataPaths
from budget.services.facade import AppServices
from budget.services.setup import SetupDraft
from budget.ui.dialogs.expense_dialog import ExpenseDialog
from budget.ui.main_window import MainWindow


@pytest.fixture
def clock():
    return FixedClock(datetime(2026, 10, 6, 9, 0, tzinfo=UTC))


@pytest.fixture
def services(tmp_path, clock):
    connection = open_application_database(DataPaths(tmp_path / "data"), clock)
    services = AppServices.create(connection, clock)
    services.setup.complete(SetupDraft(general_remainder=Money(10_000)))
    yield services
    connection.close()


def plain(text: str) -> str:
    return text.replace(" ", " ").replace(" ", " ")


def select(dialog: ExpenseDialog, name: str) -> None:
    dialog.source_combo.setCurrentIndex(
        next(i for i, o in enumerate(dialog._options) if o.name == name)
    )


def test_dialog_shows_available_and_blocks_insufficient_source(qtbot, services):
    services.incomes.create("Аванс", None, Money(400_000))
    dialog = ExpenseDialog(services.expenses)
    qtbot.addWidget(dialog)
    select(dialog, "Аванс")
    assert plain(dialog.available.text()) == "Доступно в джерелі: 4 000"
    dialog.name.field.setText("Ноутбук")
    dialog.amount.field.setText("5 000")
    assert not dialog.save_button.isEnabled()
    assert not dialog.limit.isHidden()
    assert plain(dialog.limit.body.text()) == (
        "У джерелі «Аванс» доступно 4 000, а потрібно 5 000. Зменште суму або оберіть інше джерело."
    )
    # Обране джерело не змінюється автоматично.
    assert dialog.selected().name == "Аванс"
    dialog.amount.field.setText("4 000")
    assert dialog.save_button.isEnabled() and dialog.limit.isHidden()
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted
    (view,) = services.expenses.list_for_month(services.months.current_month())
    assert view.source_name == "Аванс" and view.expense.amount == Money(400_000)


def test_name_required_in_form(qtbot, services):
    dialog = ExpenseDialog(services.expenses)
    qtbot.addWidget(dialog)
    select(dialog, "Загальний нерозподілений залишок")
    dialog.amount.field.setText("10")
    dialog.save()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.name.error.text()


def test_locked_expense_allows_only_metadata(qtbot, services):
    income = services.incomes.create("Аванс", None, Money(1_000)).income
    view = services.expenses.create(
        "Усе", None, Money(1_000), SourceRef(SourceKind.INCOME, income_id=income.id)
    )
    dialog = ExpenseDialog(services.expenses, editing=view)
    qtbot.addWidget(dialog)
    assert not dialog.amount.field.isEnabled() and not dialog.source_combo.isEnabled()
    assert "архівовано" in dialog.limit.body.text()
    dialog.name.field.setText("Усе за аванс")
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert services.expenses.get(view.expense.id).expense.name == "Усе за аванс"


def test_month_rows_actions_and_delete(qtbot, services, monkeypatch):
    remainder = SourceRef(SourceKind.GENERAL_REMAINDER)
    view = services.expenses.create("Кава", None, Money(100), remainder)
    window = MainWindow("Budget", services)
    qtbot.addWidget(window)
    page = window.month
    assert page.expense_rows.count() == 1
    assert page.new_expense_button.isVisibleTo(page)

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
    page.delete_expense(services.expenses.get(view.expense.id))
    assert services.expenses.list_for_month(services.months.current_month()) == []
    assert services.balances.general_remainder() == Money(10_000)
    assert plain(window.overview.total_label.text()) == "100"


def test_past_month_is_read_only(qtbot, services, clock):
    services.expenses.create("Жовтнева", None, Money(100), SourceRef(SourceKind.GENERAL_REMAINDER))
    clock.set(datetime(2026, 11, 3, 9, 0, tzinfo=UTC))
    window = MainWindow("Budget", services)
    qtbot.addWidget(window)
    page = window.month
    assert page.expense_rows.count() == 1  # листопад: «немає витрат»
    page.previous_button.click()
    assert str(page.month) == "2026-10"
    assert not page.read_only_banner.isHidden()
    assert not page.new_expense_button.isVisibleTo(page)
    assert not page.new_income_button.isVisibleTo(page)
    row = page.expense_rows.itemAt(0).widget()
    assert not [b for b in row.findChildren(type(page.new_expense_button)) if b.text() == "⋯"]
    assert not page.previous_button.isEnabled()  # раніше за місяць налаштування — нічого
