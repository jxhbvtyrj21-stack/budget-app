"""Сховище боргів і погашень; похідні залишок і статус боргу в BalanceService."""

import sqlite3

import pytest

from budget.domain.calendar import CalendarMonth
from budget.domain.models import (
    Debt,
    DebtOrigin,
    DebtRepayment,
    DebtStatus,
    Income,
    SourceKind,
    SourceRef,
)
from budget.domain.money import Money
from budget.services.balances import BalanceService
from budget.storage.repositories import DebtRepository, IncomeRepository
from budget.storage.transaction import transaction

OCTOBER = CalendarMonth(2026, 10)
NOVEMBER = CalendarMonth(2026, 11)
REMAINDER = SourceRef(SourceKind.GENERAL_REMAINDER)


@pytest.fixture
def repository(connection):
    return DebtRepository(connection)


def loan(name="Кредитна картка", amount=5_000, month=OCTOBER):
    return Debt(None, DebtOrigin.LOAN_RECEIPT, month, name, None, Money(amount))


def repayment(debt_id, amount, source=REMAINDER, month=OCTOBER, repayment_id=None):
    return DebtRepayment(repayment_id, debt_id, month, None, Money(amount), source)


def test_debt_insert_get_and_list_for_month(connection, repository):
    with transaction(connection):
        initial = repository.insert(
            Debt(None, DebtOrigin.INITIAL, None, "Позика", None, Money(300))
        )
        first = repository.insert(loan("Картка"))
        second = repository.insert(loan("Позика в Олени", 1_000))
        repository.insert(loan("Листопад", month=NOVEMBER))
    assert repository.get(first) == Debt(
        first, DebtOrigin.LOAN_RECEIPT, OCTOBER, "Картка", None, Money(5_000)
    )
    assert repository.get(initial).month is None
    assert repository.get(999) is None
    assert [d.id for d in repository.list_for_month(OCTOBER)] == [second, first]


def test_debt_metadata_and_amount_update(connection, repository):
    with transaction(connection):
        debt_id = repository.insert(loan())
        repository.update_metadata(debt_id, "Картка банку", "Покупка техніки")
        repository.update_amount(debt_id, Money(7_000))
    stored = repository.get(debt_id)
    assert (stored.name, stored.description, stored.amount) == (
        "Картка банку",
        "Покупка техніки",
        Money(7_000),
    )


def test_repayment_insert_get_and_list_for_debt(connection, repository):
    with transaction(connection):
        debt_id = repository.insert(loan())
        other = repository.insert(loan("Інший"))
        first = repository.insert_repayment(repayment(debt_id, 1_000))
        second = repository.insert_repayment(repayment(debt_id, 500, month=NOVEMBER))
        repository.insert_repayment(repayment(other, 200))
    assert repository.get_repayment(first) == repayment(debt_id, 1_000, repayment_id=first)
    assert repository.get_repayment(999) is None
    assert [r.id for r in repository.list_repayments_for_debt(debt_id)] == [second, first]
    assert [r.debt_id for r in repository.list_repayments_for_month(OCTOBER)] == [other, debt_id]
    assert repository.repaid_total(debt_id) == Money(1_500)


def test_repayment_keeps_given_id(connection, repository):
    with transaction(connection):
        debt_id = repository.insert(loan())
        repayment_id = repository.insert_repayment(repayment(debt_id, 100, repayment_id=42))
    assert repayment_id == 42 and repository.get_repayment(42).amount == Money(100)


def test_repayment_replace_description_and_delete(connection, repository):
    with transaction(connection):
        income_id = IncomeRepository(connection).insert(
            Income(None, OCTOBER, "Аванс", None, Money(10_000))
        )
        debt_id = repository.insert(loan())
        repayment_id = repository.insert_repayment(repayment(debt_id, 1_000))
    income = SourceRef(SourceKind.INCOME, income_id=income_id)
    replacement = DebtRepayment(repayment_id, debt_id, OCTOBER, "Частина", Money(700), income)
    with transaction(connection):
        repository.replace_repayment(replacement)
    assert repository.get_repayment(repayment_id) == replacement
    with transaction(connection):
        repository.update_repayment_description(repayment_id, "Перша частина")
    assert repository.get_repayment(repayment_id).description == "Перша частина"
    with transaction(connection):
        repository.delete_repayment(repayment_id)
    assert repository.get_repayment(repayment_id) is None


def test_repayment_replace_requires_transaction(connection, repository):
    with transaction(connection):
        debt_id = repository.insert(loan())
        repayment_id = repository.insert_repayment(repayment(debt_id, 100))
    with pytest.raises(RuntimeError):
        repository.replace_repayment(repayment(debt_id, 50, repayment_id=repayment_id))


def test_debt_with_repayments_cannot_be_deleted(connection, repository):
    with transaction(connection):
        debt_id = repository.insert(loan())
        free = repository.insert(loan("Без погашень"))
        repository.insert_repayment(repayment(debt_id, 100))
    with pytest.raises(sqlite3.IntegrityError), transaction(connection):
        repository.delete(debt_id)  # ON DELETE RESTRICT
    assert repository.get(debt_id) is not None
    with transaction(connection):
        repository.delete(free)
    assert repository.get(free) is None


def test_debt_remaining_status_and_active_total(connection, repository):
    with transaction(connection):
        active = repository.insert(loan("Картка", 5_000))
        paid = repository.insert(loan("Позика", 1_000))
        initial = repository.insert(Debt(None, DebtOrigin.INITIAL, None, "Стара", None, Money(300)))
        repository.insert_repayment(repayment(active, 2_000))
        repository.insert_repayment(repayment(paid, 1_000))
    balances = BalanceService(connection)
    views = {d.id: balances.debt_view(d) for d in repository.list_all()}
    assert (views[active].repaid, views[active].remaining) == (Money(2_000), Money(3_000))
    assert views[active].status is DebtStatus.ACTIVE
    assert views[paid].remaining == Money.zero() and views[paid].status is DebtStatus.PAID
    assert views[initial].remaining == Money(300)
    assert balances.active_debts_total() == Money(3_300)
    # Борг не є частиною загальної доступної суми.
    assert balances.available_funds().total == Money.zero()
