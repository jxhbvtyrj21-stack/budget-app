"""Регресія поповнень: взаємодія з витратами, доходами, переходом місяця й накопиченнями.

ADR 0003, ADR 0007, ADR 0009, ADR 0010 (Q153, Q155), ADR 0011 (Q170), ADR 0020.
"""

from datetime import UTC, datetime

import pytest

from budget.domain.calendar import CalendarMonth
from budget.domain.models import AccumulationStatus
from budget.domain.money import Money
from budget.errors import DomainRuleError
from budget.services.accumulation import AccumulationService
from budget.services.balances import BalanceService
from budget.services.expense import ExpenseService
from budget.services.income import IncomeService
from budget.services.month import MonthTransitionService
from budget.services.replenishment import RecipientBalanceError
from budget.services.sources import InsufficientFundsError
from budget.storage.repositories import FinancialRecordRepository
from tests.services.replenishment_fixtures import (
    REMAINDER,
    acc_balance,
    acc_source,
    complete_setup,
    income_balance,
    income_source,
    part,
    remainder,
    replenishments,
)

OCTOBER = CalendarMonth(2026, 10)


@pytest.fixture
def trip(db, clock):
    """«Подорож» (5 000); загальний нерозподілений залишок 10 000."""
    return complete_setup(db, clock).accumulation_id


def test_replenishment_is_not_an_ordinary_expense(db, clock, trip):
    replenishments(db, clock).create("Відкладаю", None, trip, [part(REMAINDER, 3_000)])
    assert ExpenseService(db, clock).list_for_month(OCTOBER) == []
    assert db.execute("SELECT COUNT(*) FROM expenses").fetchone()[0] == 0


def test_replenished_funds_can_then_be_spent_as_ordinary_expense(db, clock, trip):
    replenishments(db, clock).create("Відкладаю", None, trip, [part(REMAINDER, 3_000)])
    ExpenseService(db, clock).create("Квитки", None, Money(8_000), acc_source(trip))
    assert acc_balance(db, trip) == Money.zero()
    with pytest.raises(InsufficientFundsError):
        ExpenseService(db, clock).create("Ще", None, Money(1), acc_source(trip))


def test_income_balance_is_shared_between_expenses_and_replenishments(db, clock, trip):
    income = income_source(db, clock, 5_000)
    ExpenseService(db, clock).create("Продукти", None, Money(3_000), income)
    with pytest.raises(InsufficientFundsError) as error:
        replenishments(db, clock).create("Відкладаю", None, trip, [part(income, 2_001)])
    assert error.value.available == Money(2_000)
    replenishments(db, clock).create("Відкладаю", None, trip, [part(income, 2_000)])
    assert income_balance(db, clock, income) == Money.zero()
    assert IncomeService(db, clock).get(income.income_id).income.archived
    with pytest.raises(DomainRuleError):
        ExpenseService(db, clock).create("Ще", None, Money(1), income)


def test_expense_edit_cannot_revive_income_archived_by_replenishment(db, clock, trip):
    income = income_source(db, clock, 5_000)
    expense = ExpenseService(db, clock).create("Продукти", None, Money(1_000), income)
    replenishments(db, clock).create("Решта", None, trip, [part(income, 4_000)])
    assert IncomeService(db, clock).get(income.income_id).income.archived
    with pytest.raises(DomainRuleError):
        ExpenseService(db, clock).delete(expense.expense.id)


def test_total_available_is_unchanged_by_replenishment_lifecycle(db, clock, trip):
    income = income_source(db, clock, 4_000)
    service = replenishments(db, clock)
    total = BalanceService(db).available_funds().total
    view = service.create("Відкладаю", None, trip, [part(income, 1_000), part(REMAINDER, 2_000)])
    assert BalanceService(db).available_funds().total == total
    service.update(view.replenishment.id, "Відкладаю", None, trip, [part(REMAINDER, 500)])
    assert BalanceService(db).available_funds().total == total
    service.delete(view.replenishment.id)
    assert BalanceService(db).available_funds().total == total


def test_month_with_only_a_replenishment_has_records(db, clock, trip):
    records = FinancialRecordRepository(db)
    assert OCTOBER not in records.months_with_records()
    view = replenishments(db, clock).create("Відкладаю", None, trip, [part(REMAINDER, 100)])
    assert OCTOBER in records.months_with_records()
    replenishments(db, clock).delete(view.replenishment.id)
    # Місяць без фінансових записів знову порожній (Q155).
    assert OCTOBER not in records.months_with_records()


def test_month_transition_consolidates_income_net_of_replenishment(db, clock, trip):
    income = income_source(db, clock, 6_000)
    replenishments(db, clock).create("Відкладаю", None, trip, [part(income, 2_500)])
    clock.set(datetime(2026, 11, 2, 9, 0, tzinfo=UTC))
    assert MonthTransitionService(db, clock).run_on_startup() is None
    # До загального нерозподіленого залишку переходить лише невикористана частина доходу.
    assert remainder(db) == Money(10_000 + 3_500)
    assert acc_balance(db, trip) == Money(7_500)
    assert IncomeService(db, clock).get(income.income_id).income.archived


def test_replenishment_never_changes_recipient_status(db, clock, trip):
    accumulations = AccumulationService(db, clock)
    accumulations.update_metadata(trip, "Подорож", None, Money(6_000))
    replenishments(db, clock).create("Відкладаю", None, trip, [part(REMAINDER, 2_000)])
    view = accumulations.get(trip)
    assert view.progress.percent == 116 and view.accumulation.status is AccumulationStatus.ACTIVE


def test_closed_recipient_with_spent_funds_cannot_lose_replenishment(db, clock, trip):
    accumulations = AccumulationService(db, clock)
    other = accumulations.create("Ремонт", None, None).accumulation.id
    view = replenishments(db, clock).create("Ремонт", None, other, [part(REMAINDER, 1_000)])
    ExpenseService(db, clock).create("Фарба", None, Money(1_000), acc_source(other))
    accumulations.change_status(other, AccumulationStatus.CLOSED)  # залишок 0
    with pytest.raises(RecipientBalanceError):
        replenishments(db, clock).delete(view.replenishment.id)
    assert acc_balance(db, other) == Money.zero()


def test_no_accumulation_balance_becomes_negative_across_operations(db, clock, trip):
    accumulations = AccumulationService(db, clock)
    other = accumulations.create("Ремонт", None, None).accumulation.id
    service = replenishments(db, clock)
    expenses = ExpenseService(db, clock)
    first = service.create("А", None, other, [part(REMAINDER, 3_000)])
    expenses.create("Б", None, Money(2_000), acc_source(other))
    attempts = [
        lambda: service.update(first.replenishment.id, "А", None, other, [part(REMAINDER, 1_500)]),
        lambda: service.update(first.replenishment.id, "А", None, trip, [part(REMAINDER, 3_000)]),
        lambda: service.delete(first.replenishment.id),
        lambda: expenses.create("В", None, Money(1_001), acc_source(other)),
        lambda: service.update(first.replenishment.id, "А", None, other, [part(REMAINDER, 2_000)]),
    ]
    for attempt in attempts:
        try:
            attempt()
        except DomainRuleError:
            pass
        for view in accumulations.list_working() + accumulations.list_archived():
            assert not view.balance.is_negative
    assert acc_balance(db, other) == Money.zero()
