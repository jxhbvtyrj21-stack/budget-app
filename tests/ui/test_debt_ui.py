"""Інтерфейс боргів: екран і картка, форми, секція Місяця, «Зобов'язання» на Огляді."""

from datetime import UTC, datetime

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QMessageBox, QPushButton

from budget.app import open_application_database
from budget.domain.calendar import FixedClock
from budget.domain.models import DebtStatus, SourceKind, SourceRef
from budget.domain.money import Money
from budget.platform.paths import DataPaths
from budget.services.facade import AppServices
from budget.services.setup import InitialDebt, SetupDraft
from budget.ui.dialogs.debt_dialogs import DebtMetadataDialog, LoanReceiptDialog, RepaymentDialog
from budget.ui.main_window import MAIN_ROUTES, MainWindow
from budget.ui.screens.debts import DebtsPage

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
            debts=(InitialDebt("Позика в Олени", None, Money(300_000)),),
        )
    )
    yield services
    connection.close()


def plain(text: str) -> str:
    return text.replace(" ", " ").replace(" ", " ")


def initial_id(services) -> int:
    return services.debts.list_active()[0].debt.id


def labels(widget) -> set[str]:
    return {plain(label.text()) for label in widget.findChildren(QLabel)}


def menu_texts(row) -> list[str]:
    buttons = [b for b in row.findChildren(QPushButton) if b.text() == "⋯"]
    return [a.text() for a in buttons[0].menu().actions()] if buttons else []


def confirm_deletion(monkeypatch):
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
def page(qtbot, services):
    widget = DebtsPage(services)
    qtbot.addWidget(widget)
    return widget


# Екран і картка ---------------------------------------------------------------------------


def test_route_is_a_real_screen_without_new_sidebar_item(qtbot, services):
    window = MainWindow("Budget", services)
    qtbot.addWidget(window)
    assert isinstance(window.debts, DebtsPage)
    assert [route for route, _ in MAIN_ROUTES] == ["overview", "month", "accumulations", "debts"]
    (all_debts,) = [b for b in window.overview.findChildren(QPushButton) if b.text() == "Усі борги"]
    all_debts.click()
    assert window.current_route() == "debts"


def test_active_and_paid_groups(page, services):
    paid = services.debts.receive_loan("Позика в брата", None, Money(200_000)).debt.id
    services.debts.repay(paid, Money(200_000), REMAINDER)
    page.refresh()
    assert list(page.list_page.group_panels) == [DebtStatus.ACTIVE, DebtStatus.PAID]
    names = [row.view.debt.name for row in page.list_page.rows]
    assert names == ["Позика в Олени", "Позика в брата"]


def test_card_of_initial_debt(page, services):
    page.open_detail(initial_id(services))
    detail = page.detail_page
    texts = labels(detail)
    assert "Початковий борг (первинне налаштування)" in texts
    assert "Сума боргу 3 000 · погашено 0" in texts
    assert plain(detail.remaining.text()) == "3 000"
    assert not detail.repay_button.isHidden()


def test_card_history_and_paid_debt_actions(page, services):
    debt_id = services.debts.receive_loan("Картка", "Ноутбук", Money(150_000)).debt.id
    services.debts.repay(debt_id, Money(150_000), REMAINDER, "Усе")
    page.open_detail(debt_id)
    detail = page.detail_page
    texts = labels(detail)
    assert {"Отримання позикових коштів", "Усе", "Жовтень 2026"} <= texts
    assert "Погашення · з: Загальний нерозподілений залишок" in texts
    # Погашений борг: «Погасити» немає, метадані змінювати можна.
    assert detail.repay_button.isHidden() and not detail.edit_button.isHidden()


def test_escape_returns_to_list(qtbot, page, services):
    page.show()
    page.open_detail(initial_id(services))
    qtbot.keyClick(page.detail_page, Qt.Key.Key_Escape)
    assert not page.showing_detail()


# Форми ------------------------------------------------------------------------------------


def test_loan_receipt_dialog(qtbot, services):
    dialog = LoanReceiptDialog(services.debts)
    qtbot.addWidget(dialog)
    dialog.amount.field.setText("5 000")
    dialog.save()
    assert dialog.name.error.text() == "Вкажіть назву."
    dialog.name.field.setText("Кредитна картка")
    dialog.save()
    created = services.debts.list_for_month(services.months.current_month())[0]
    assert created.debt.name == "Кредитна картка" and created.debt.amount == Money(500_000)
    assert services.balances.general_remainder() == Money(1_500_000)


def test_loan_amount_edit_shows_floor_message(qtbot, services):
    debt_id = services.debts.receive_loan("Картка", None, Money(500_000)).debt.id
    services.debts.repay(debt_id, Money(200_000), REMAINDER)
    dialog = LoanReceiptDialog(services.debts, editing=services.debts.get(debt_id))
    qtbot.addWidget(dialog)
    dialog.amount.field.setText("1 000")
    dialog.save()
    assert plain(dialog.failure.body.text()) == (
        "Борг має погашення на суму 2 000. Суму боргу не можна зробити меншою за 2 000, "
        "а борг — видалити."
    )
    assert services.debts.get(debt_id).debt.amount == Money(500_000)


def test_repayment_dialog_single_source_and_limits(qtbot, services):
    dialog = RepaymentDialog(services.debts, debt_id=initial_id(services))
    qtbot.addWidget(dialog)
    assert plain(dialog.debt_remaining.text()) == "Залишок боргу: 3 000"
    assert dialog.source_combo.count() == 1  # лише нерозподілений залишок; боргу серед джерел немає
    assert plain(dialog.available.text()) == "Доступно в джерелі: 10 000"
    dialog.amount.field.setText("4 000")
    assert not dialog.save_button.isEnabled()
    assert plain(dialog.limit.body.text()) == (
        "Залишок боргу «Позика в Олени» — 3 000, а погашення — 4 000. Зменште суму погашення."
    )
    dialog.amount.field.setText("1 000")
    dialog.description.field.setText("Частина")
    assert dialog.save_button.isEnabled() and dialog.limit.isHidden()
    dialog.save()
    assert services.debts.get(initial_id(services)).remaining == Money(200_000)


def test_locked_repayment_allows_only_description(qtbot, services):
    income = services.incomes.create("Аванс", None, Money(100_000)).income
    source = SourceRef(SourceKind.INCOME, income_id=income.id)
    view = services.debts.repay(initial_id(services), Money(100_000), source)
    dialog = RepaymentDialog(services.debts, editing=view)
    qtbot.addWidget(dialog)
    assert not dialog.amount.field.isEnabled() and not dialog.source_combo.isEnabled()
    assert not dialog.limit.isHidden()
    dialog.description.field.setText("Від аванса")
    dialog.save()
    assert services.debts.get_repayment(view.repayment.id).repayment.description == "Від аванса"


def test_metadata_dialog_for_paid_debt(qtbot, services):
    debt_id = services.debts.receive_loan("Картка", None, Money(100_000)).debt.id
    services.debts.repay(debt_id, Money(100_000), REMAINDER)
    dialog = DebtMetadataDialog(services.debts, services.debts.get(debt_id))
    qtbot.addWidget(dialog)
    dialog.name.field.setText("Картка банку")
    dialog.save()
    view = services.debts.get(debt_id)
    assert view.debt.name == "Картка банку" and view.status is DebtStatus.PAID


# Місяць і Огляд ---------------------------------------------------------------------------


def test_month_section_menus_and_delete(qtbot, services, monkeypatch):
    services.debts.receive_loan("Без погашень", None, Money(100_000))
    paid = services.debts.receive_loan("Погашений", None, Money(50_000)).debt.id
    services.debts.repay(paid, Money(50_000), REMAINDER, "Усе")
    window = MainWindow("Budget", services)
    qtbot.addWidget(window)
    rows = [
        window.month.debt_rows.itemAt(i).widget() for i in range(window.month.debt_rows.count())
    ]
    assert [menu_texts(r) for r in rows] == [
        ["Змінити суму"],  # «Погашений» має погашення — видаляти не можна
        ["Змінити суму", "Видалити"],
        ["Редагувати"],  # останнє погашення погашеного боргу — не видаляється
    ]
    # Поповнення й погашення не з'являються серед витрат.
    assert window.month.expense_rows.count() == 1
    confirm_deletion(monkeypatch)
    free = services.debts.list_for_month(services.months.current_month())[1]
    window.month.delete_loan(free)
    assert [
        v.debt.name for v in services.debts.list_for_month(services.months.current_month())
    ] == ["Погашений"]


def test_month_past_is_read_only(qtbot, services, clock):
    services.debts.receive_loan("Жовтневий", None, Money(10_000))
    clock.set(datetime(2026, 11, 3, 9, 0, tzinfo=UTC))
    window = MainWindow("Budget", services)
    qtbot.addWidget(window)
    window.month.previous_button.click()
    row = window.month.debt_rows.itemAt(0).widget()
    assert menu_texts(row) == []


def test_overview_obligations_are_separate(qtbot, services):
    window = MainWindow("Budget", services)
    qtbot.addWidget(window)
    overview = window.overview
    assert plain(overview.debts_label.text()) == "3 000"
    assert plain(overview.total_label.text()) == "10 000"  # борг не віднімається
    services.debts.receive_loan("Картка", None, Money(500_000))
    window.refresh()
    assert plain(overview.debts_label.text()) == "8 000"
    assert plain(overview.total_label.text()) == "15 000"
    assert "Активних боргів: 2" in labels(overview.obligations)
