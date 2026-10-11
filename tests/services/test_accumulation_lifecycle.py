"""Створення накопичення й ручний життєвий цикл (ADR 0007, ADR 0011 Q172–Q173, ADR 0013)."""

import pytest

from budget.domain.models import AccumulationStatus, SourceKind, SourceRef
from budget.domain.money import Money
from budget.errors import DomainRuleError, ValidationError
from budget.services.accumulation import AccumulationService, CloseBlockedError
from budget.services.balances import BalanceService
from tests.services.expense_fixtures import (
    complete_setup,
    expenses,
    set_accumulation_state,
)

ACTIVE, REACHED, CLOSED = (
    AccumulationStatus.ACTIVE,
    AccumulationStatus.REACHED,
    AccumulationStatus.CLOSED,
)


@pytest.fixture
def service(db, clock):
    complete_setup(db, clock)  # «Подорож» з початковим балансом 5 000
    return AccumulationService(db, clock)


def financial_snapshot(db):
    funds = BalanceService(db).available_funds()
    tables = ("incomes", "expenses", "replenishments", "debt_repayments")
    counts = tuple(db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables)
    return funds, counts


# Створення --------------------------------------------------------------------------------


def test_create_requires_name(service):
    with pytest.raises(ValidationError):
        service.create("  ", None, None)
    assert len(service.list_working()) == 1


def test_create_with_optional_description_and_target(service):
    plain = service.create("Ремонт", None, None)
    full = service.create("Ноутбук", "Для роботи", Money(4_000_000))
    assert (plain.accumulation.description, plain.accumulation.target) == (None, None)
    assert full.accumulation.description == "Для роботи"
    assert full.accumulation.target == Money(4_000_000)


def test_new_accumulation_is_active_with_zero_balance_and_no_initial_balance(service, db):
    before = financial_snapshot(db)
    view = service.create("Ремонт", None, Money(100_000))
    assert view.accumulation.status is ACTIVE and not view.accumulation.archived
    assert view.balance == Money.zero()
    assert view.accumulation.initial_balance == Money.zero()
    # Створення не є фінансовою операцією й не змінює загальної доступної суми.
    assert financial_snapshot(db) == before


def test_zero_target_is_active_and_has_no_progress(service):
    view = service.create("Ремонт", None, Money.zero())
    assert view.accumulation.status is ACTIVE
    assert view.progress is None


def test_duplicate_names_allowed(service):
    service.create("Подорож", None, None)
    names = [v.accumulation.name for v in service.list_working()]
    assert names == ["Подорож", "Подорож"]


def test_create_blocked_before_setup(db, clock):
    with pytest.raises(DomainRuleError):
        AccumulationService(db, clock).create("Ремонт", None, None)


# Життєвий цикл ----------------------------------------------------------------------------


def test_active_reached_round_trip(service):
    (trip,) = service.list_working()
    acc_id = trip.accumulation.id
    assert service.change_status(acc_id, REACHED).accumulation.status is REACHED
    assert service.change_status(acc_id, ACTIVE).accumulation.status is ACTIVE


@pytest.mark.parametrize("start", [ACTIVE, REACHED])
def test_close_with_zero_balance(service, start):
    view = service.create("Ремонт", None, None)
    if start is REACHED:
        service.change_status(view.accumulation.id, REACHED)
    closed = service.change_status(view.accumulation.id, CLOSED)
    assert closed.accumulation.status is CLOSED and not closed.accumulation.archived


@pytest.mark.parametrize("start", [ACTIVE, REACHED])
def test_close_with_positive_balance_blocked(service, db, start):
    (trip,) = service.list_working()
    acc_id = trip.accumulation.id
    if start is REACHED:
        service.change_status(acc_id, REACHED)
    with pytest.raises(CloseBlockedError) as error:
        service.change_status(acc_id, CLOSED)
    assert error.value.balance == Money(5_000)
    after = service.get(acc_id)
    assert after.accumulation.status is start and after.balance == Money(5_000)


def test_closed_back_to_active_but_not_to_reached(service):
    view = service.create("Ремонт", None, None)
    acc_id = view.accumulation.id
    service.change_status(acc_id, CLOSED)
    with pytest.raises(DomainRuleError):
        service.change_status(acc_id, REACHED)
    assert service.get(acc_id).accumulation.status is CLOSED
    assert service.change_status(acc_id, ACTIVE).accumulation.status is ACTIVE


@pytest.mark.parametrize("status", [ACTIVE, REACHED, CLOSED])
def test_same_status_is_not_a_transition(service, status):
    view = service.create("Ремонт", None, None)
    acc_id = view.accumulation.id
    if status is not ACTIVE:
        service.change_status(acc_id, status)
    with pytest.raises(DomainRuleError):
        service.change_status(acc_id, status)


def test_status_change_has_no_financial_effect(service, db):
    view = service.create("Ремонт", None, None)
    before = financial_snapshot(db)
    for status in (REACHED, ACTIVE, CLOSED, ACTIVE):
        service.change_status(view.accumulation.id, status)
        assert financial_snapshot(db) == before


def test_status_transitions_follow_graph_and_close_blocker(service):
    (trip,) = service.list_working()
    assert service.status_transitions(trip) == (REACHED, CLOSED)
    assert service.close_blocker(trip).balance == Money(5_000)
    empty = service.create("Ремонт", None, None)
    assert service.close_blocker(empty) is None


def test_no_automatic_transitions_from_balance_or_target(service, db, clock):
    """Залишок, що досяг цілі чи нуля, статусу не змінює (ADR 0007, п. 9)."""
    (trip,) = service.list_working()
    acc_id = trip.accumulation.id
    expenses(db, clock).create("Квитки", None, Money(5_000), trip_source(acc_id))
    after = service.get(acc_id)
    assert after.balance == Money.zero() and after.accumulation.status is ACTIVE


def test_expense_from_closed_accumulation_keeps_status(service, db, clock):
    """Закрите неархівоване накопичення з залишком > 0 — джерело; статус не змінюється."""
    (trip,) = service.list_working()
    acc_id = trip.accumulation.id
    set_accumulation_state(db, trip_source(acc_id), "closed", False)  # закрите з залишком
    expenses(db, clock).create("Квитки", None, Money(2_000), trip_source(acc_id))
    after = service.get(acc_id)
    assert after.balance == Money(3_000)
    assert after.accumulation.status is CLOSED and not after.accumulation.archived
    assert BalanceService(db).general_remainder() == Money(10_000)


def trip_source(accumulation_id):
    return SourceRef(SourceKind.ACCUMULATION, accumulation_id=accumulation_id)
