"""Цільова сума, метадані, архівування й розархівування накопичення.

ADR 0007, ADR 0012 (Q180–Q185), ADR 0013 (Q186–Q189), ADR 0014 (Q191), ADR 0022.
"""

import sqlite3

import pytest

from budget.domain.models import AccumulationStatus, SourceKind, SourceRef
from budget.domain.money import Money
from budget.errors import DomainRuleError, ValidationError
from budget.services.accumulation import AccumulationService
from budget.services.balances import BalanceService
from budget.storage.repositories import AccumulationRepository
from tests.services.expense_fixtures import complete_setup, expenses, set_accumulation_state

ACTIVE, REACHED, CLOSED = (
    AccumulationStatus.ACTIVE,
    AccumulationStatus.REACHED,
    AccumulationStatus.CLOSED,
)


@pytest.fixture
def service(db, clock):
    complete_setup(db, clock)  # «Подорож» з початковим балансом 5 000
    return AccumulationService(db, clock)


def source(accumulation_id):
    return SourceRef(SourceKind.ACCUMULATION, accumulation_id=accumulation_id)


def state(db):
    """Повний фінансовий стан і рядки накопичень (для перевірки «нічого не змінилося»)."""
    funds = BalanceService(db).available_funds()
    rows = db.execute("SELECT * FROM accumulations ORDER BY id").fetchall()
    tables = ("incomes", "expenses", "replenishments", "debt_repayments")
    counts = tuple(db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables)
    return funds, tuple(rows), counts


def closed_archived(service):
    view = service.create("Ремонт", None, Money(1_000))
    acc_id = view.accumulation.id
    service.change_status(acc_id, CLOSED)
    service.archive(acc_id)
    return acc_id


def trip_id(service):
    (trip,) = service.list_working()
    return trip.accumulation.id


# Цільова сума -----------------------------------------------------------------------------


def test_set_change_and_remove_target(service):
    acc_id = trip_id(service)
    assert service.get(acc_id).progress is None
    view = service.update_metadata(acc_id, "Подорож", None, Money(10_000))
    assert view.accumulation.target == Money(10_000) and view.progress.percent == 50
    view = service.update_metadata(acc_id, "Подорож", None, Money(4_000))
    assert view.progress.percent == 125 and view.progress.excess == Money(1_000)
    view = service.update_metadata(acc_id, "Подорож", None, None)
    assert view.accumulation.target is None and view.progress is None


@pytest.mark.parametrize("status", [ACTIVE, REACHED, CLOSED, "archived"])
def test_target_change_in_every_status_keeps_everything_else(service, db, status):
    if status == "archived":
        acc_id = closed_archived(service)
    else:
        acc_id = service.create("Ремонт", "Кухня", None).accumulation.id
        if status is not ACTIVE:
            service.change_status(acc_id, status)
    before = service.get(acc_id)
    funds = BalanceService(db).available_funds()
    after = service.update_metadata(acc_id, "Ремонт", "Кухня", Money(50_000))
    assert after.accumulation.target == Money(50_000)
    assert after.accumulation.status is before.accumulation.status
    assert after.accumulation.archived is before.accumulation.archived
    assert after.balance == before.balance
    assert BalanceService(db).available_funds() == funds


def test_target_reached_by_balance_does_not_change_status(service):
    acc_id = trip_id(service)
    view = service.update_metadata(acc_id, "Подорож", None, Money(5_000))
    assert view.progress.percent == 100 and view.accumulation.status is ACTIVE
    service.change_status(acc_id, REACHED)
    view = service.update_metadata(acc_id, "Подорож", None, Money(50_000))
    assert view.accumulation.status is REACHED  # ціль вище залишку — не «Активне»


def test_target_is_not_a_limit_for_expenses(service, db, clock):
    acc_id = trip_id(service)
    service.update_metadata(acc_id, "Подорож", None, Money(1_000))
    expenses(db, clock).create("Квитки", None, Money(4_500), source(acc_id))
    assert service.get(acc_id).balance == Money(500)


def test_negative_target_rejected_atomically(service, db):
    acc_id = trip_id(service)
    before = state(db)
    with pytest.raises(ValidationError):
        service.update_metadata(acc_id, "Інша назва", None, Money(-1))
    assert state(db) == before


# Метадані ---------------------------------------------------------------------------------


def test_name_and_description_editable(service, db):
    acc_id = trip_id(service)
    before_funds = BalanceService(db).available_funds()
    view = service.update_metadata(acc_id, "Подорож до моря", "Серпень", None)
    assert (view.accumulation.name, view.accumulation.description) == ("Подорож до моря", "Серпень")
    view = service.update_metadata(acc_id, "Подорож до моря", "  ", None)
    assert view.accumulation.description is None
    assert BalanceService(db).available_funds() == before_funds


def test_empty_name_rejected_without_partial_save(service, db):
    acc_id = trip_id(service)
    before = state(db)
    with pytest.raises(ValidationError):
        service.update_metadata(acc_id, "   ", "Новий опис", Money(1))
    assert state(db) == before


def test_archived_metadata_editable(service, db):
    acc_id = closed_archived(service)
    view = service.update_metadata(acc_id, "Ремонт кухні", "Готово", None)
    assert view.accumulation.name == "Ремонт кухні" and view.accumulation.archived
    assert view.accumulation.status is CLOSED


def test_metadata_change_creates_no_financial_operation(service, db):
    acc_id = trip_id(service)
    funds, _, counts = state(db)
    service.update_metadata(acc_id, "Інше", "Опис", Money(7))
    assert state(db)[0] == funds and state(db)[2] == counts


# Архівування ------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [ACTIVE, REACHED])
def test_only_closed_can_be_archived(service, db, status):
    acc_id = service.create("Ремонт", None, None).accumulation.id
    if status is REACHED:
        service.change_status(acc_id, REACHED)
    before = state(db)
    with pytest.raises(DomainRuleError):
        service.archive(acc_id)
    assert state(db) == before


def test_archive_closed_keeps_status_balance_and_total(service, db):
    acc_id = trip_id(service)
    set_accumulation_state(db, source(acc_id), "closed", False)  # закрите із залишком 5 000
    funds = BalanceService(db).available_funds()
    view = service.archive(acc_id)
    assert view.accumulation.archived and view.accumulation.status is CLOSED
    assert view.balance == Money(5_000)
    # Залишок архівованого накопичення входить до загальної доступної суми (Q187).
    assert BalanceService(db).available_funds() == funds
    assert funds.accumulations == Money(5_000)
    assert service.list_working() == [] and [
        v.accumulation.id for v in service.list_archived()
    ] == [acc_id]


def test_archive_twice_is_rejected(service, db):
    acc_id = closed_archived(service)
    before = state(db)
    with pytest.raises(DomainRuleError):
        service.archive(acc_id)
    assert state(db) == before


def test_archived_status_cannot_change(service, db):
    acc_id = closed_archived(service)
    before = state(db)
    for status in AccumulationStatus:
        with pytest.raises(DomainRuleError):
            service.change_status(acc_id, status)
    assert state(db) == before
    assert service.status_transitions(service.get(acc_id)) == ()


def test_unarchive_leaves_closed_and_changes_no_financial_values(service, db):
    acc_id = closed_archived(service)
    funds = BalanceService(db).available_funds()
    view = service.unarchive(acc_id)
    assert not view.accumulation.archived and view.accumulation.status is CLOSED
    assert BalanceService(db).available_funds() == funds
    assert service.status_transitions(view) == (ACTIVE,)


def test_unarchive_of_not_archived_is_rejected(service, db):
    acc_id = trip_id(service)
    before = state(db)
    with pytest.raises(DomainRuleError):
        service.unarchive(acc_id)
    assert state(db) == before


def test_archived_accumulation_is_not_an_expense_source(service, db, clock):
    acc_id = closed_archived(service)
    with pytest.raises(DomainRuleError):
        expenses(db, clock).create("Фарба", None, Money(1), source(acc_id))
    options = expenses(db, clock).source_options()
    assert all(o.source.accumulation_id != acc_id for o in options)


# Видалення --------------------------------------------------------------------------------


def test_no_physical_delete_and_no_deleted_status(service):
    assert not any("delete" in name for name in dir(AccumulationService))
    assert not any("delete" in name for name in dir(AccumulationRepository))
    assert {s.value for s in AccumulationStatus} == {"active", "reached", "closed"}


def test_database_refuses_inconsistent_archive_combination(service, db):
    """Схема не допускає «Активне + архівоване» навіть в обхід сервісу (Q186)."""
    acc_id = trip_id(service)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE accumulations SET archived = 1 WHERE id = ?", (acc_id,))


# Історія ----------------------------------------------------------------------------------


def test_expense_history_lists_only_this_accumulation(service, db, clock):
    acc_id = trip_id(service)
    other = service.create("Ремонт", None, None).accumulation.id
    expense_service = expenses(db, clock)
    expense_service.create("Квитки", None, Money(1_000), source(acc_id))
    expense_service.create("Готель", None, Money(2_000), source(acc_id))
    history = service.expense_history(acc_id)
    assert [v.expense.name for v in history] == ["Готель", "Квитки"]
    assert all(v.source_name == "Подорож" for v in history)
    assert service.expense_history(other) == []
