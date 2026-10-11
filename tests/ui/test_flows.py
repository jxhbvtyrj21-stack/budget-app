"""Поведінкові тести мінімального інтерфейсу: майстер, дохід, огляд, діалог перерви."""

from datetime import UTC, datetime

import pytest
from PySide6.QtWidgets import QDialog, QLabel, QMessageBox

from budget.app import open_application_database
from budget.domain.calendar import FixedClock
from budget.domain.money import Money
from budget.errors import BudgetError
from budget.platform.paths import DataPaths
from budget.services.facade import AppServices
from budget.services.month import LongGapChoice
from budget.services.setup import InitialSetupService, SetupDraft
from budget.ui.dialogs.income_dialog import IncomeDialog
from budget.ui.dialogs.long_gap_dialog import LongGapDialog
from budget.ui.main_window import MainWindow


@pytest.fixture
def clock():
    return FixedClock(datetime(2026, 10, 6, 9, 0, tzinfo=UTC))


@pytest.fixture
def connection(tmp_path, clock):
    connection = open_application_database(DataPaths(tmp_path / "data"), clock)
    yield connection
    connection.close()


def text(label) -> str:
    return label.text().replace(" ", " ").replace(" ", " ")


def test_wizard_completes_into_normal_mode(qtbot, connection, clock):
    services = AppServices.create(connection, clock)
    window = MainWindow("Budget", services)
    qtbot.addWidget(window)
    wizard = window.wizard
    wizard.next_button.click()  # вітання → наявні кошти
    wizard.remainder_input.setText("15 000")
    wizard.next_button.click()  # → накопичення
    wizard.acc_name.field.setText("На паркан")
    wizard.acc_balance.field.setText("50 000")
    wizard.acc_target.field.setText("50 000")
    wizard.add_accumulation_button.click()
    wizard.acc_name.field.setText("Подорож")
    wizard.add_accumulation_button.click()  # баланс 0
    wizard.next_button.click()  # → борги
    wizard.debt_name.field.setText("Позика")
    wizard.debt_balance.field.setText("3 000")
    wizard.add_debt_button.click()
    wizard.next_button.click()  # → перевірка
    assert wizard.finish_button.isVisible() or wizard.draft.step == 5
    wizard.finish_button.click()

    assert services.setup.is_completed()
    assert window.sidebar is not None and window.current_route() == "overview"
    funds = services.balances.available_funds()
    assert funds.general_remainder == Money(1_500_000)
    assert funds.accumulations == Money(5_000_000)
    assert text(window.overview.total_label) == "65 000"


def test_wizard_draft_resumes_and_resets(qtbot, connection, clock, monkeypatch):
    services = AppServices.create(connection, clock)
    window = MainWindow("Budget", services)
    qtbot.addWidget(window)
    window.wizard.next_button.click()
    window.wizard.remainder_input.setText("700")
    window.close()  # закриття зберігає чернетку
    resumed = MainWindow("Budget", AppServices.create(connection, clock))
    qtbot.addWidget(resumed)
    assert resumed.wizard.draft.general_remainder == Money(70_000)
    assert resumed.wizard.steps.currentIndex() == 1
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Reset)
    )
    resumed.wizard.reset_button.click()
    assert resumed.wizard.draft == SetupDraft()
    assert services.setup.state().draft is None


def test_invalid_input_shows_error_and_keeps_step(qtbot, connection, clock):
    window = MainWindow("Budget", AppServices.create(connection, clock))
    qtbot.addWidget(window)
    window.wizard.next_button.click()
    window.wizard.remainder_input.setText("12,345")
    window.wizard.next_button.click()
    assert window.wizard.steps.currentIndex() == 1
    assert window.wizard.remainder_field.error.text()


def completed_window(qtbot, connection, clock) -> MainWindow:
    services = AppServices.create(connection, clock)
    services.setup.complete(SetupDraft(general_remainder=Money(100_000)))
    window = MainWindow("Budget", services)
    qtbot.addWidget(window)
    return window


def test_income_dialog_creates_income(qtbot, connection, clock):
    window = completed_window(qtbot, connection, clock)
    dialog = IncomeDialog(window._services.incomes, window)
    qtbot.addWidget(dialog)
    dialog.amount.field.setText("2 500,50")
    dialog.save()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.name.error.text()  # назва обов'язкова
    dialog.name.field.setText("Замовлення №1")
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted
    window.refresh()
    (view,) = window._services.incomes.list_for_month(window._services.months.current_month())
    assert view.balance == Money(250_050)
    assert text(window.overview.total_label) == "3 500,50"
    assert window.month.income_rows.count() == 1


def test_long_gap_dialog_requires_explicit_choice(qtbot, connection, clock):
    services = AppServices.create(connection, clock)
    services.setup.complete(SetupDraft())
    services.incomes.create("Жовтневий", None, Money(40_000))
    clock.set(datetime(2027, 1, 5, 9, 0, tzinfo=UTC))
    assert services.transitions.run_on_startup().total == Money(40_000)
    window = MainWindow("Budget", services)
    qtbot.addWidget(window)
    assert "Залишки минулих місяців, що очікують рішення" in window.overview.panel_amounts

    dialog = LongGapDialog(Money(40_000), window)
    qtbot.addWidget(dialog)
    dialog.reject()  # закриття без вибору
    assert dialog.choice is None
    assert services.transitions.pending_long_gap() is not None

    dialog = LongGapDialog(Money(40_000), window)
    qtbot.addWidget(dialog)
    dialog._choose(LongGapChoice.TRANSFER)
    assert dialog.choice is LongGapChoice.TRANSFER
    services.transitions.resolve_long_gap(dialog.choice)
    window.refresh()
    assert services.transitions.pending_long_gap() is None
    assert services.balances.general_remainder() == Money(40_000)


def test_refresh_leaves_no_detached_panels(qtbot, connection, clock):
    from budget.ui.components.basic import Panel

    window = completed_window(qtbot, connection, clock)
    for _ in range(3):
        window.refresh()
    # Підсумок + три панелі складу; старі панелі від'єднано одразу.
    assert len(window.overview.findChildren(Panel)) == 4


# Майстер: «Можна пропустити» і «Помилка» (IA 11, 12) ----------------------------------------


def list_texts(layout) -> list[str]:
    widgets = [layout.itemAt(i).widget() for i in range(layout.count())]
    return [text(w) for w in widgets if isinstance(w, QLabel)]


def test_empty_wizard_lists_can_be_skipped(qtbot, connection, clock):
    window = MainWindow("Budget", AppServices.create(connection, clock))
    qtbot.addWidget(window)
    wizard = window.wizard
    wizard.next_button.click()  # → наявні кошти
    wizard.next_button.click()  # → накопичення
    assert list_texts(wizard.accumulation_list) == ["Можна пропустити."]
    wizard.acc_name.field.setText("Подорож")
    wizard.add_accumulation_button.click()
    assert "Можна пропустити." not in list_texts(wizard.accumulation_list)
    wizard.next_button.click()  # → борги
    assert list_texts(wizard.debt_list) == ["Можна пропустити."]
    wizard.debt_name.field.setText("Позика")
    wizard.debt_balance.field.setText("3 000")
    wizard.add_debt_button.click()
    assert "Можна пропустити." not in list_texts(wizard.debt_list)


def test_failed_finish_shows_error_and_keeps_wizard(qtbot, connection, clock, monkeypatch):
    window = MainWindow("Budget", AppServices.create(connection, clock))
    qtbot.addWidget(window)
    wizard = window.wizard
    wizard.next_button.click()
    wizard.remainder_input.setText("700")
    for _ in range(3):
        wizard.next_button.click()  # → перевірка
    shown = []
    monkeypatch.setattr(
        QMessageBox, "warning", lambda parent, title, message: shown.append((title, message))
    )

    def failing(service, draft):
        raise BudgetError("Не вдалося завершити налаштування.")

    monkeypatch.setattr(InitialSetupService, "complete", failing)
    draft = wizard.draft
    wizard.finish_button.click()
    assert shown == [("Помилка", "Не вдалося завершити налаштування.")]
    assert window.wizard is wizard and wizard.draft == draft  # дані майстра не втрачено
