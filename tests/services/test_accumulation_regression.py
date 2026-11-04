"""Регресія: накопичення як джерело звичайної витрати й атомарність змін накопичення.

ADR 0003, ADR 0007, ADR 0011 (Q170), ADR 0013 (Q187, Q189), ADR 0014 (Q191).
"""

from datetime import UTC, datetime

import pytest

from budget.domain.models import AccumulationStatus, SourceKind, SourceRef
from budget.domain.money import Money
from budget.errors import DomainRuleError
from budget.services.accumulation import AccumulationService
from budget.services.balances import BalanceService
from budget.services.sources import InsufficientFundsError
from budget.storage.repositories import AccumulationRepository
from tests.services.expense_fixtures import REMAINDER, complete_setup, expenses

ACTIVE, REACHED, CLOSED = (
    AccumulationStatus.ACTIVE,
    AccumulationStatus.REACHED,
    AccumulationStatus.CLOSED,
)


class Interrupted(Exception):
    pass


@pytest.fixture
def trip(db, clock):
    return complete_setup(db, clock)  # «Подорож» з початковим балансом 5 000


@pytest.fixture
def service(db, clock, trip):
    return AccumulationService(db, clock)


def rows(db):
    return db.execute("SELECT * FROM accumulations ORDER BY id").fetchall()


def acc_source(accumulation_id):
    return SourceRef(SourceKind.ACCUMULATION, accumulation_id=accumulation_id)


# Накопичення як джерело -------------------------------------------------------------------


@pytest.mark.parametrize("status", [ACTIVE, REACHED])
def test_active_and_reached_are_sources_without_status_change(db, clock, service, trip, status):
    if status is REACHED:
        service.change_status(trip.accumulation_id, REACHED)
    expenses(db, clock).create("Квитки", None, Money(1_500), trip)
    view = service.get(trip.accumulation_id)
    assert view.balance == Money(3_500) and view.accumulation.status is status


def test_closed_unarchived_source_then_archive_blocks_it(db, clock, service):
    """Закрите із залишком 0 → архів → не джерело; розархівоване — знову джерело."""
    acc_id = service.create("Ремонт", None, None).accumulation.id
    service.change_status(acc_id, CLOSED)
    with pytest.raises(InsufficientFundsError):
        expenses(db, clock).create("Фарба", None, Money(1), acc_source(acc_id))
    service.archive(acc_id)
    with pytest.raises(DomainRuleError) as error:
        expenses(db, clock).create("Фарба", None, Money(1), acc_source(acc_id))
    assert "архів" in error.value.user_message
    service.unarchive(acc_id)
    assert service.get(acc_id).accumulation.status is CLOSED


def test_expense_cannot_drive_accumulation_negative(db, clock, service, trip):
    with pytest.raises(InsufficientFundsError) as error:
        expenses(db, clock).create("Готель", None, Money(5_001), trip)
    assert (error.value.available, error.value.required) == (Money(5_000), Money(5_001))
    assert service.get(trip.accumulation_id).balance == Money(5_000)


def test_existing_expense_follows_archive_and_unarchive(db, clock, service, trip):
    """Q189/Q191 на реальному ланцюжку: витрата → закриття → архів → розархівування."""
    expense_service = expenses(db, clock)
    view = expense_service.create("Квитки", None, Money(5_000), trip)
    acc_id = trip.accumulation_id
    service.change_status(acc_id, CLOSED)  # залишок 0 після витрати
    service.archive(acc_id)
    expense_id = view.expense.id
    for amount, source in ((Money(4_000), trip), (Money(5_000), REMAINDER)):
        with pytest.raises(DomainRuleError):
            expense_service.update(expense_id, "Квитки", None, amount, source)
    with pytest.raises(DomainRuleError):
        expense_service.delete(expense_id)
    renamed = expense_service.update(expense_id, "Квитки на потяг", "Київ", Money(5_000), trip)
    assert renamed.expense.name == "Квитки на потяг"
    after = service.get(acc_id)
    assert after.accumulation.archived and after.accumulation.status is CLOSED
    assert after.balance == Money.zero()
    # Після розархівування фінансові зміни поточного місяця знову доступні.
    service.unarchive(acc_id)
    expense_service.update(expense_id, "Квитки на потяг", "Київ", Money(4_000), trip)
    assert service.get(acc_id).balance == Money(1_000)
    assert service.get(acc_id).accumulation.status is CLOSED  # без автоматичних переходів


def test_historical_expense_from_accumulation_is_fully_read_only(db, clock, service, trip):
    expense_service = expenses(db, clock)
    view = expense_service.create("Квитки", None, Money(1_000), trip)
    clock.set(datetime(2026, 11, 3, 9, 0, tzinfo=UTC))
    with pytest.raises(DomainRuleError):
        expense_service.update(view.expense.id, "Інша назва", None, Money(1_000), trip)
    with pytest.raises(DomainRuleError):
        expense_service.delete(view.expense.id)
    assert expense_service.get(view.expense.id).expense.name == "Квитки"
    # Метадані самого накопичення змінюються будь-коли (Q184).
    service.update_metadata(trip.accumulation_id, "Подорож 2026", None, None)
    assert service.get(trip.accumulation_id).balance == Money(4_000)


def test_archived_balance_stays_in_total_available(db, clock, service, trip):
    acc_id = trip.accumulation_id
    total = BalanceService(db).available_funds().total
    expenses(db, clock).create("Квитки", None, Money(5_000), trip)
    service.change_status(acc_id, CLOSED)
    service.archive(acc_id)
    funds = BalanceService(db).available_funds()
    assert funds.total == total - Money(5_000)
    assert funds.accumulations == Money.zero()


# Атомарність ------------------------------------------------------------------------------


def test_interrupted_status_change_rolls_back(db, service, trip, monkeypatch):
    original = AccumulationRepository.set_status

    def write_then_fail(self, accumulation_id, status):
        original(self, accumulation_id, status)
        raise Interrupted

    before = rows(db)
    monkeypatch.setattr(AccumulationRepository, "set_status", write_then_fail)
    with pytest.raises(Interrupted):
        service.change_status(trip.accumulation_id, REACHED)
    assert rows(db) == before


@pytest.mark.parametrize("operation", ["archive", "unarchive"])
def test_interrupted_archive_change_rolls_back(db, service, monkeypatch, operation):
    acc_id = service.create("Ремонт", None, None).accumulation.id
    service.change_status(acc_id, CLOSED)
    if operation == "unarchive":
        service.archive(acc_id)
    original = AccumulationRepository.set_archived

    def write_then_fail(self, accumulation_id, archived):
        original(self, accumulation_id, archived)
        raise Interrupted

    before = (rows(db), BalanceService(db).available_funds())
    monkeypatch.setattr(AccumulationRepository, "set_archived", write_then_fail)
    with pytest.raises(Interrupted):
        getattr(service, operation)(acc_id)
    assert (rows(db), BalanceService(db).available_funds()) == before


def test_interrupted_metadata_update_rolls_back(db, service, trip, monkeypatch):
    original = AccumulationRepository.update_metadata

    def write_then_fail(self, *args):
        original(self, *args)
        raise Interrupted

    before = rows(db)
    monkeypatch.setattr(AccumulationRepository, "update_metadata", write_then_fail)
    with pytest.raises(Interrupted):
        service.update_metadata(trip.accumulation_id, "Інше", "Опис", Money(1))
    assert rows(db) == before


def test_no_accumulation_balance_becomes_negative(db, clock, service, trip):
    expense_service = expenses(db, clock)
    second = acc_source(service.create("Ремонт", None, None).accumulation.id)
    for name, amount, source in (
        ("А", Money(3_000), trip),
        ("Б", Money(2_001), trip),
        ("В", Money(1), second),
        ("Г", Money(2_000), trip),
    ):
        try:
            expense_service.create(name, None, amount, source)
        except DomainRuleError:
            pass
        for view in service.list_working() + service.list_archived():
            assert not view.balance.is_negative
    assert service.get(trip.accumulation_id).balance == Money.zero()
