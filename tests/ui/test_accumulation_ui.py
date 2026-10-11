"""Екран «Накопичення»: перелік, архів, картка, дозволені дії й форма (ADR 0021, 6)."""

import sqlite3
from datetime import UTC, datetime

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel

from budget.app import open_application_database
from budget.domain.calendar import FixedClock
from budget.domain.models import AccumulationStatus, SourceKind, SourceRef
from budget.domain.money import Money
from budget.platform.paths import DataPaths
from budget.services.facade import AppServices
from budget.services.month import MonthTransitionService
from budget.services.setup import InitialAccumulation, SetupDraft
from budget.ui.dialogs.accumulation_dialog import AccumulationDialog
from budget.ui.formatting import format_money
from budget.ui.main_window import MAIN_ROUTES, MainWindow
from budget.ui.screens.accumulations import AccumulationsPage

ACTIVE, REACHED, CLOSED = (
    AccumulationStatus.ACTIVE,
    AccumulationStatus.REACHED,
    AccumulationStatus.CLOSED,
)


@pytest.fixture
def clock():
    return FixedClock(datetime(2026, 10, 6, 9, 0, tzinfo=UTC))


@pytest.fixture
def services(tmp_path, clock):
    connection = open_application_database(DataPaths(tmp_path / "data"), clock)
    services = AppServices.create(connection, clock)
    services.setup.complete(
        SetupDraft(
            accumulations=(InitialAccumulation("На паркан", None, Money(7_000_000), Money(10**7)),)
        )
    )
    yield services
    connection.close()


def plain(text: str) -> str:
    return text.replace("\u00a0", " ").replace("\u202f", " ")


def make(services, name, status=ACTIVE, archived=False):
    acc_id = services.accumulations.create(name, None, None).accumulation.id
    if status is not ACTIVE:
        services.accumulations.change_status(acc_id, status)
    if archived:
        services.accumulations.archive(acc_id)
    return acc_id


def fence_id(services):
    return next(
        v.accumulation.id
        for v in services.accumulations.list_working()
        if v.accumulation.name == "На паркан"
    )


def menu_texts(detail):
    return [(a.text(), a.isEnabled()) for a in detail.status_menu.actions()]


@pytest.fixture
def page(qtbot, services):
    widget = AccumulationsPage(services)
    qtbot.addWidget(widget)
    return widget


def test_route_is_a_real_screen_without_new_sidebar_item(qtbot, services):
    window = MainWindow("Бюджет", services)
    qtbot.addWidget(window)
    assert isinstance(window.accumulations, AccumulationsPage)
    assert [route for route, _ in MAIN_ROUTES] == ["overview", "month", "accumulations", "debts"]
    window.navigate("accumulations")
    assert window.current_route() == "accumulations"


def test_working_list_groups_by_status_and_hides_archive(page, services):
    make(services, "Подорож", REACHED)
    make(services, "Ремонт", CLOSED)
    make(services, "Старий ноутбук", CLOSED, archived=True)
    page.refresh()
    lists = page.list_page
    assert list(lists.group_panels) == [ACTIVE, REACHED, CLOSED]
    names = [row.view.accumulation.name for row in lists.rows]
    assert names == ["На паркан", "Подорож", "Ремонт"]
    lists.show_archive(True)
    assert [row.view.accumulation.name for row in lists.rows] == ["Старий ноутбук"]
    archived = lists.rows[0].view.accumulation
    assert archived.status is CLOSED and archived.archived


def test_empty_archive_message(page):
    page.list_page.show_archive(True)
    assert page.list_page.rows == []


def test_row_shows_progress_only_with_target(page, services):
    make(services, "Без цілі")
    page.refresh()
    with_target, without = page.list_page.rows
    assert with_target.view.progress.percent == 70
    assert without.view.progress is None


def test_new_accumulation_dialog(qtbot, services):
    dialog = AccumulationDialog(services.accumulations)
    qtbot.addWidget(dialog)
    dialog.save()
    assert dialog.name.error.text() == "Вкажіть назву."
    dialog.name.field.setText("Ремонт")
    dialog.target.field.setText("abc")
    dialog.save()
    assert dialog.target.error.isVisibleTo(dialog)
    dialog.target.field.setText("20 000")
    dialog.save()
    created = services.accumulations.list_working()[-1]
    assert created.accumulation.name == "Ремонт"
    assert created.accumulation.status is ACTIVE and created.balance == Money.zero()
    assert created.accumulation.target == Money(2_000_000)


def test_active_card_with_balance_explains_blocked_close(page, services):
    page.open_detail(fence_id(services))
    detail = page.detail_page
    assert page.showing_detail()
    assert plain(detail.breadcrumb.text()) == "Накопичення / На паркан"
    assert menu_texts(detail) == [
        ("Позначити досягнутим", True),
        ("Закрити — лише при залишку 0", False),
    ]
    assert plain(detail.close_hint.text()) == (
        "Закрити накопичення можна лише при залишку 0. Зараз залишок 70 000."
    )
    assert not detail.close_hint.isHidden()
    assert detail.archive_button.isHidden() and detail.unarchive_button.isHidden()
    assert detail.archived_notice.isHidden()


def test_reached_card_actions(page, services):
    acc_id = make(services, "Подорож", REACHED)
    page.open_detail(acc_id)
    detail = page.detail_page
    assert menu_texts(detail) == [("Зробити активним", True), ("Закрити", True)]
    assert detail.close_hint.isHidden()


def test_status_changes_from_card(page, services):
    acc_id = make(services, "Ремонт")
    page.open_detail(acc_id)
    detail = page.detail_page
    detail.status_menu.actions()[1].trigger()  # «Закрити» при залишку 0
    assert services.accumulations.get(acc_id).accumulation.status is CLOSED
    assert menu_texts(detail) == [("Зробити активним", True)]
    assert not detail.archive_button.isHidden()


def test_archive_and_unarchive_from_card(page, services):
    acc_id = make(services, "Ремонт", CLOSED)
    page.open_detail(acc_id)
    detail = page.detail_page
    detail.archive_button.click()
    view = services.accumulations.get(acc_id)
    assert view.accumulation.archived and view.accumulation.status is CLOSED
    assert detail.status_button.isHidden() and detail.archive_button.isHidden()
    assert not detail.unarchive_button.isHidden() and not detail.archived_notice.isHidden()
    detail.unarchive_button.click()
    view = services.accumulations.get(acc_id)
    assert not view.accumulation.archived and view.accumulation.status is CLOSED
    assert menu_texts(detail) == [("Зробити активним", True)]


def test_archived_metadata_and_target_editable(qtbot, page, services):
    acc_id = make(services, "Ремонт", CLOSED, archived=True)
    page.open_detail(acc_id)
    dialog = AccumulationDialog(services.accumulations, editing=page.detail_page.view)
    qtbot.addWidget(dialog)
    dialog.name.field.setText("Ремонт кухні")
    dialog.target.field.setText("5 000")
    dialog.save()
    page.detail_page.refresh()
    view = services.accumulations.get(acc_id)
    assert view.accumulation.name == "Ремонт кухні" and view.accumulation.target == Money(500_000)
    assert view.accumulation.archived and view.accumulation.status is CLOSED
    assert page.detail_page.title_label.text() == "Ремонт кухні"


def test_remove_target_in_dialog(qtbot, page, services):
    page.open_detail(fence_id(services))
    dialog = AccumulationDialog(services.accumulations, editing=page.detail_page.view)
    qtbot.addWidget(dialog)
    dialog.target.field.clear()
    dialog.save()
    view = services.accumulations.get(fence_id(services))
    assert view.accumulation.target is None and view.accumulation.status is ACTIVE


def test_escape_and_back_return_to_list(qtbot, page, services):
    page.show()
    page.open_detail(fence_id(services))
    qtbot.keyClick(page.detail_page, Qt.Key.Key_Escape)
    assert not page.showing_detail()
    page.open_detail(fence_id(services))
    qtbot.keyClick(page.detail_page, Qt.Key.Key_Left, Qt.KeyboardModifier.AltModifier)
    assert not page.showing_detail()
    page.open_detail(fence_id(services))
    page.detail_page.back_button.click()
    assert not page.showing_detail()


def test_card_history_shows_initial_balance_and_expenses(page, services):
    acc_id = fence_id(services)
    source = SourceRef(SourceKind.ACCUMULATION, accumulation_id=acc_id)
    services.expenses.create("Стовпчики", None, Money(450_000), source)
    page.open_detail(acc_id)
    texts = {label.text() for label in page.detail_page.findChildren(QLabel)}
    assert {"Початковий баланс (первинне налаштування)", "Жовтень 2026"} <= texts
    assert "Стовпчики" in texts and "Витрата з накопичення" in texts


# «Огляд»: «з них в архіві: X» (IA 4.1, 12) -------------------------------------------------


def overview_texts(window) -> list[str]:
    return [plain(label.text()) for label in window.overview.findChildren(QLabel)]


def test_overview_shows_archived_share_of_accumulations(qtbot, services, monkeypatch):
    make(services, "Стара ціль", CLOSED, archived=True)
    archived = services.accumulations.list_archived()
    expected = sum((v.balance for v in archived), Money.zero())
    connects, transitions = [], []
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: connects.append(a))
    monkeypatch.setattr(
        MonthTransitionService, "run_on_startup", lambda self: transitions.append(self)
    )
    window = MainWindow("Budget", services)
    qtbot.addWidget(window)
    window.refresh()
    assert f"з них в архіві: {plain(format_money(expected))}" in overview_texts(window)
    # Лише показ наявних даних: без нових з'єднань і без переходу між місяцями.
    assert connects == [] and transitions == []


def test_overview_without_archived_accumulations_has_no_archive_line(qtbot, services):
    assert services.accumulations.list_archived() == []
    window = MainWindow("Budget", services)
    qtbot.addWidget(window)
    assert not [t for t in overview_texts(window) if t.startswith("з них в архіві")]
