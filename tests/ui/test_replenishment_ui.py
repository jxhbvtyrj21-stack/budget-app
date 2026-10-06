"""Інтерфейс поповнень: форма з рядками джерел, Місяць, Огляд і картка накопичення."""

from datetime import UTC, datetime

import pytest
from PySide6.QtWidgets import QLabel, QMessageBox, QPushButton

from budget.app import open_application_database
from budget.domain.calendar import FixedClock
from budget.domain.models import AccumulationStatus, ReplenishmentPart, SourceKind, SourceRef
from budget.domain.money import Money
from budget.platform.paths import DataPaths
from budget.services.facade import AppServices
from budget.services.setup import InitialAccumulation, SetupDraft
from budget.ui.dialogs.replenishment_dialog import ReplenishmentDialog
from budget.ui.main_window import MainWindow
from budget.ui.screens.accumulations import AccumulationsPage

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
            accumulations=(InitialAccumulation("На паркан", None, Money(500_000)),),
        )
    )
    yield services
    connection.close()


def plain(text: str) -> str:
    return text.replace(" ", " ").replace(" ", " ")


def fence(services) -> int:
    return services.accumulations.list_working()[0].accumulation.id


def income(services, amount=400_000, name="Аванс") -> SourceRef:
    created = services.incomes.create(name, None, Money(amount)).income
    return SourceRef(SourceKind.INCOME, income_id=created.id)


def names(row) -> list[str]:
    return [row.source_combo.itemText(i) for i in range(row.source_combo.count())]


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


def menu_texts(row) -> list[str]:
    (more,) = [b for b in row.findChildren(QPushButton) if b.text() == "⋯"]
    return [action.text() for action in more.menu().actions()]


# Форма ------------------------------------------------------------------------------------


def test_dynamic_rows_with_unique_sources(qtbot, services):
    income(services)
    dialog = ReplenishmentDialog(services.replenishments)
    qtbot.addWidget(dialog)
    assert len(dialog.rows) == 1
    assert names(dialog.rows[0]) == ["Аванс", "Загальний нерозподілений залишок"]
    dialog.add_button.click()
    first, second = dialog.rows
    # Обране в одному рядку джерело не пропонується в іншому.
    assert names(first) == ["Аванс"]
    assert names(second) == ["Загальний нерозподілений залишок"]
    assert not dialog.add_button.isEnabled()  # усі джерела вже обрано
    second.remove_button.click()
    assert len(dialog.rows) == 1
    assert names(dialog.rows[0]) == ["Аванс", "Загальний нерозподілений залишок"]
    assert dialog.add_button.isEnabled()


def test_available_required_total_and_limit(qtbot, services):
    income(services)
    dialog = ReplenishmentDialog(services.replenishments)
    qtbot.addWidget(dialog)
    dialog.name.field.setText("Відкладаю")
    first = dialog.rows[0]
    assert plain(first.available.text()) == "доступно 4 000"
    first.amount.setText("5 000")
    assert not dialog.save_button.isEnabled() and not dialog.limit.isHidden()
    assert plain(dialog.limit.body.text()) == (
        "У джерелі «Аванс» доступно 4 000, а потрібно 5 000. Зменште суму або оберіть інше джерело."
    )
    first.amount.setText("3 000")
    dialog.add_button.click()
    dialog.rows[1].amount.setText("1 500")
    assert plain(dialog.total.text()) == "Разом: 4 500"
    assert dialog.save_button.isEnabled() and dialog.limit.isHidden()
    # Джерело жодного рядка не змінилося автоматично.
    assert [r.selected().name for r in dialog.rows] == ["Аванс", "Загальний нерозподілений залишок"]
    dialog.save()
    (view,) = services.replenishments.list_for_month(services.months.current_month())
    assert view.replenishment.parts == (
        ReplenishmentPart(view.replenishment.parts[0].source, Money(300_000)),
        ReplenishmentPart(REMAINDER, Money(150_000)),
    )
    assert view.recipient_name == "На паркан"


def test_name_required_in_form(qtbot, services):
    dialog = ReplenishmentDialog(services.replenishments)
    qtbot.addWidget(dialog)
    dialog.rows[0].amount.setText("100")
    dialog.save()
    assert dialog.name.error.text() == "Вкажіть назву."
    assert services.replenishments.list_for_month(services.months.current_month()) == []


def test_locked_replenishment_allows_only_metadata(qtbot, services):
    acc_id = services.accumulations.create("Ремонт", None, None).accumulation.id
    view = services.replenishments.create(
        "Ремонт", None, acc_id, [ReplenishmentPart(REMAINDER, Money(100))]
    )
    services.expenses.create(
        "Фарба", None, Money(100), SourceRef(SourceKind.ACCUMULATION, accumulation_id=acc_id)
    )
    services.accumulations.change_status(acc_id, AccumulationStatus.CLOSED)
    services.accumulations.archive(acc_id)
    dialog = ReplenishmentDialog(
        services.replenishments, editing=services.replenishments.get(view.replenishment.id)
    )
    qtbot.addWidget(dialog)
    assert (
        not dialog.recipient_combo.isEnabled() and dialog.recipient_combo.currentText() == "Ремонт"
    )
    assert not dialog.rows[0].amount.isEnabled() and not dialog.rows[0].source_combo.isEnabled()
    assert dialog.add_button.isHidden() and not dialog.limit.isHidden()
    dialog.name.field.setText("Ремонт кухні")
    dialog.save()
    assert services.replenishments.get(view.replenishment.id).replenishment.name == "Ремонт кухні"


def test_part_from_archived_income_is_locked(qtbot, services):
    drained = income(services, 100_000, "Замовлення")
    view = services.replenishments.create(
        "Усе",
        None,
        fence(services),
        [ReplenishmentPart(drained, Money(100_000)), ReplenishmentPart(REMAINDER, Money(500))],
    )
    dialog = ReplenishmentDialog(services.replenishments, editing=view)
    qtbot.addWidget(dialog)
    locked, free = dialog.rows
    assert locked.locked and not locked.amount.isEnabled() and locked.remove_button.isHidden()
    assert names(locked) == ["Замовлення"]
    assert free.amount.isEnabled()
    assert "архівованого доходу" in dialog.limit.body.text()
    free.amount.setText("200")
    dialog.save()
    assert services.balances.general_remainder() == Money(1_000_000 - 20_000)


# Місяць -----------------------------------------------------------------------------------


def test_month_section_menu_and_delete(qtbot, services, monkeypatch):
    view = services.replenishments.create(
        "Відкладаю", None, fence(services), [ReplenishmentPart(REMAINDER, Money(200_000))]
    )
    window = MainWindow("Budget", services)
    qtbot.addWidget(window)
    page = window.month
    assert page.replenishment_rows.count() == 1
    assert page.new_replenishment_button.isVisibleTo(page)
    # Поповнення не є звичайною витратою.
    assert plain(page.expense_rows.itemAt(0).widget().text()) == "У цьому місяці немає витрат."
    row = page.replenishment_rows.itemAt(0).widget()
    assert menu_texts(row) == ["Редагувати", "Видалити"]
    confirm_deletion(monkeypatch)
    page.delete_replenishment(services.replenishments.get(view.replenishment.id))
    assert services.replenishments.list_for_month(services.months.current_month()) == []
    assert services.balances.general_remainder() == Money(1_000_000)


def test_month_menu_hides_delete_for_q174(qtbot, services):
    drained = income(services, 100_000)
    services.replenishments.create(
        "Усе", None, fence(services), [ReplenishmentPart(drained, Money(100_000))]
    )
    window = MainWindow("Budget", services)
    qtbot.addWidget(window)
    row = window.month.replenishment_rows.itemAt(0).widget()
    assert menu_texts(row) == ["Редагувати"]


def test_past_month_replenishments_read_only(qtbot, services, clock):
    services.replenishments.create(
        "Жовтневе", None, fence(services), [ReplenishmentPart(REMAINDER, Money(100))]
    )
    clock.set(datetime(2026, 11, 3, 9, 0, tzinfo=UTC))
    window = MainWindow("Budget", services)
    qtbot.addWidget(window)
    page = window.month
    page.previous_button.click()
    assert not page.new_replenishment_button.isVisibleTo(page)
    row = page.replenishment_rows.itemAt(0).widget()
    assert not [b for b in row.findChildren(QPushButton) if b.text() == "⋯"]


def test_overview_button_opens_replenishment_form(qtbot, services, monkeypatch):
    opened = []
    monkeypatch.setattr(ReplenishmentDialog, "exec", lambda self: opened.append(self) or 0)
    window = MainWindow("Budget", services)
    qtbot.addWidget(window)
    window.overview.new_replenishment_button.click()
    assert len(opened) == 1 and opened[0].selected_recipient() == fence(services)


# Картка накопичення -----------------------------------------------------------------------


def test_card_replenish_button_presets_recipient(qtbot, services, monkeypatch):
    other = services.accumulations.create("Подорож", None, None).accumulation.id
    page = AccumulationsPage(services)
    qtbot.addWidget(page)
    page.open_detail(other)
    detail = page.detail_page
    assert not detail.replenish_button.isHidden()
    opened = []
    monkeypatch.setattr(ReplenishmentDialog, "exec", lambda self: opened.append(self) or 0)
    detail.replenish_button.click()
    (dialog,) = opened
    assert dialog.selected_recipient() == other and not dialog.recipient_combo.isEnabled()


def test_card_replenish_hidden_for_archive(qtbot, services):
    acc_id = services.accumulations.create("Старе", None, None).accumulation.id
    services.accumulations.change_status(acc_id, AccumulationStatus.CLOSED)
    services.accumulations.archive(acc_id)
    page = AccumulationsPage(services)
    qtbot.addWidget(page)
    page.open_detail(acc_id)
    assert page.detail_page.replenish_button.isHidden()


def test_card_history_shows_replenishments(qtbot, services):
    services.replenishments.create(
        "Відкладаю", None, fence(services), [ReplenishmentPart(REMAINDER, Money(1_000))]
    )
    page = AccumulationsPage(services)
    qtbot.addWidget(page)
    page.open_detail(fence(services))
    texts = {label.text() for label in page.detail_page.findChildren(QLabel)}
    assert "Відкладаю" in texts
    assert "+ Поповнення · з: Загальний нерозподілений залишок" in texts
