"""Наявність фінансових записів для порожнього стану «Огляду» (IA 12).

Джерело — ``FinancialRecordRepository.months_with_records()``; сервіс лише читає.
"""

from datetime import UTC, datetime

import pytest

from budget.domain.calendar import CalendarMonth
from budget.domain.models import ReplenishmentPart, SourceKind, SourceRef
from budget.domain.money import Money
from budget.services.base_minimum import BaseMinimumService
from budget.services.debt import DebtService
from budget.services.expense import ExpenseService
from budget.services.income import IncomeService
from budget.services.month_analysis import MonthAnalysisService
from budget.services.replenishment import ReplenishmentService
from budget.services.setup import (
    InitialAccumulation,
    InitialDebt,
    InitialSetupService,
    SetupDraft,
)
from budget.storage.repositories import AccumulationRepository

REMAINDER = SourceRef(SourceKind.GENERAL_REMAINDER)


@pytest.fixture
def setup_db(db, clock):
    """Налаштування зі стартовим станом: залишок, накопичення й наявний борг."""
    InitialSetupService(db, clock).complete(
        SetupDraft(
            general_remainder=Money(100_000),
            accumulations=(InitialAccumulation("Подорож", None, Money(30_000)),),
            debts=(InitialDebt("Позика", None, Money(15_000)),),
        )
    )
    return db


def total_changes(db) -> int:
    return db.execute("SELECT total_changes()").fetchone()[0]


def test_initial_state_is_not_a_financial_record(setup_db):
    analysis = MonthAnalysisService(setup_db)
    before = total_changes(setup_db)
    assert analysis.has_financial_records() is False
    assert total_changes(setup_db) == before  # лише читання


def test_base_minimum_alone_is_not_a_financial_record(setup_db, clock):
    BaseMinimumService(setup_db, clock).set(CalendarMonth(2026, 10), Money(20_000))
    assert MonthAnalysisService(setup_db).has_financial_records() is False


def _income(db, clock):
    IncomeService(db, clock).create("Зарплата", None, Money(5_000))


def _expense(db, clock):
    ExpenseService(db, clock).create("Продукти", None, Money(1_000), REMAINDER)


def _replenishment(db, clock):
    trip = AccumulationRepository(db).list_all()[0].id
    ReplenishmentService(db, clock).create(
        "Відкладаю", None, trip, [ReplenishmentPart(REMAINDER, Money(2_000))]
    )


def _loan(db, clock):
    DebtService(db, clock).receive_loan("Картка", None, Money(9_000))


def _repayment(db, clock):
    debts = DebtService(db, clock)
    debts.repay(debts.list_active()[0].debt.id, Money(3_000), REMAINDER)


@pytest.mark.parametrize(
    "record", [_income, _expense, _replenishment, _loan, _repayment], ids=lambda f: f.__name__
)
def test_any_financial_record_is_reported(setup_db, clock, record):
    record(setup_db, clock)
    analysis = MonthAnalysisService(setup_db)
    before = total_changes(setup_db)
    assert analysis.has_financial_records() is True
    assert total_changes(setup_db) == before


def test_records_of_a_past_month_count_with_zero_balance(db, clock):
    """Записи минулого місяця й нульовий баланс — записи все одно є."""
    InitialSetupService(db, clock).complete(SetupDraft())
    income = IncomeService(db, clock).create("Жовтень", None, Money(5_000)).income
    ExpenseService(db, clock).create(
        "Усе", None, Money(5_000), SourceRef(SourceKind.INCOME, income_id=income.id)
    )
    clock.set(datetime(2026, 11, 3, 9, 0, tzinfo=UTC))
    assert MonthAnalysisService(db).has_financial_records() is True


def test_deleting_the_only_record_leaves_no_records(setup_db, clock):
    expenses = ExpenseService(setup_db, clock)
    expense = expenses.create("Продукти", None, Money(1_000), REMAINDER)
    expenses.delete(expense.expense.id)
    assert MonthAnalysisService(setup_db).has_financial_records() is False
