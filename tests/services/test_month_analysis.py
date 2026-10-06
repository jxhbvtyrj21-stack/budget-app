"""Аналіз місяця: точні суми за ADR 0020, рух накопичень (варіант А), базовий мінімум."""

from datetime import UTC, datetime

import pytest

from budget.domain.calendar import CalendarMonth
from budget.domain.models import (
    AccumulationStatus,
    MinimumComparison,
    ReplenishmentPart,
    SourceKind,
    SourceRef,
)
from budget.domain.money import Money
from budget.services.accumulation import AccumulationService
from budget.services.balances import BalanceService
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
from budget.storage.repositories import AccumulationRepository, DebtRepository

OCTOBER = CalendarMonth(2026, 10)
NOVEMBER = CalendarMonth(2026, 11)
REMAINDER = SourceRef(SourceKind.GENERAL_REMAINDER)


@pytest.fixture
def setup(db, clock):
    """Залишок 100 000; «Подорож» 50 000 (початковий баланс); початковий борг 10 000."""
    InitialSetupService(db, clock).complete(
        SetupDraft(
            general_remainder=Money(100_000),
            accumulations=(InitialAccumulation("Подорож", None, Money(50_000)),),
            debts=(InitialDebt("Позика", None, Money(10_000)),),
        )
    )
    (trip,) = AccumulationRepository(db).list_all()
    (debt,) = DebtRepository(db).list_all()
    return trip.id, debt.id


def acc(accumulation_id):
    return SourceRef(SourceKind.ACCUMULATION, accumulation_id=accumulation_id)


def analyse(db, month=OCTOBER):
    return MonthAnalysisService(db).analyse(month)


def test_empty_month(db, clock, setup):
    analysis = analyse(db)
    assert analysis.incomes == Money.zero() and analysis.actual_expenses == Money.zero()
    assert analysis.replenishments == Money.zero()
    assert analysis.debt_repayments == Money.zero() and analysis.loan_receipts == Money.zero()
    assert analysis.accumulation_movements == ()
    assert set(analysis.expenses_by_source.values()) == {Money.zero()}
    # Базового мінімуму немає — порівняння немає. Початкові баланси — не операції.
    assert analysis.base_minimum is None and analysis.comparison is None


def test_full_month_exact_totals(db, clock, setup):
    trip, debt = setup
    income = IncomeService(db, clock).create("Зарплата", None, Money(40_000)).income
    income_source = SourceRef(SourceKind.INCOME, income_id=income.id)
    expenses = ExpenseService(db, clock)
    expenses.create("Продукти", None, Money(7_000), income_source)
    expenses.create("Комунальні", None, Money(3_000), REMAINDER)
    expenses.create("Квитки", None, Money(4_000), acc(trip))
    ReplenishmentService(db, clock).create(
        "Відкладаю",
        None,
        trip,
        [
            ReplenishmentPart(income_source, Money(10_000)),
            ReplenishmentPart(REMAINDER, Money(5_000)),
        ],
    )
    debts = DebtService(db, clock)
    debts.receive_loan("Кредитна картка", None, Money(8_000))
    debts.repay(debt, Money(2_000), REMAINDER)
    debts.repay(debt, Money(1_500), acc(trip))

    analysis = analyse(db)
    assert analysis.incomes == Money(40_000)  # без початкового балансу й позикових коштів
    assert analysis.actual_expenses == Money(14_000)  # без поповнень і погашень
    assert analysis.expenses_by_source == {
        SourceKind.INCOME: Money(7_000),
        SourceKind.GENERAL_REMAINDER: Money(3_000),
        SourceKind.ACCUMULATION: Money(4_000),
    }
    assert analysis.replenishments == Money(15_000)
    assert analysis.debt_repayments == Money(3_500)
    assert analysis.loan_receipts == Money(8_000)
    (movement,) = analysis.accumulation_movements
    assert (movement.name, movement.replenished, movement.spent, movement.repaid) == (
        "Подорож",
        Money(15_000),
        Money(4_000),
        Money(1_500),
    )
    assert movement.net_change == Money(9_500)


def test_net_change_matches_actual_balance_change(db, clock, setup):
    """Чиста зміна = фактична зміна залишку за місяць (початковий баланс не входить)."""
    trip, debt = setup
    accumulation = AccumulationRepository(db).get(trip)
    before = BalanceService(db).accumulation_balance(accumulation)
    ReplenishmentService(db, clock).create(
        "Відкладаю", None, trip, [ReplenishmentPart(REMAINDER, Money(6_000))]
    )
    ExpenseService(db, clock).create("Квитки", None, Money(9_000), acc(trip))
    DebtService(db, clock).repay(debt, Money(2_500), acc(trip))
    after = BalanceService(db).accumulation_balance(AccumulationRepository(db).get(trip))
    (movement,) = analyse(db).accumulation_movements
    assert movement.net_change == after - before == Money(-5_500)


def test_movement_only_for_accumulations_with_operations(db, clock, setup):
    """«Подорож» без операцій у місяці в русі не показується."""
    other = AccumulationService(db, clock).create("Ремонт", None, None).accumulation.id
    ReplenishmentService(db, clock).create(
        "Ремонт", None, other, [ReplenishmentPart(REMAINDER, Money(1_000))]
    )
    assert [m.accumulation_id for m in analyse(db).accumulation_movements] == [other]


def test_archived_accumulation_history_stays_in_its_month(db, clock, setup):
    trip, _ = setup
    accumulations = AccumulationService(db, clock)
    ExpenseService(db, clock).create("Усе", None, Money(50_000), acc(trip))
    accumulations.change_status(trip, AccumulationStatus.CLOSED)
    accumulations.archive(trip)
    analysis = analyse(db)
    assert analysis.actual_expenses == Money(50_000)
    (movement,) = analysis.accumulation_movements
    assert movement.archived and movement.spent == Money(50_000)
    assert movement.net_change == Money(-50_000)


def test_historical_month_analysis(db, clock, setup):
    ExpenseService(db, clock).create("Жовтень", None, Money(1_000), REMAINDER)
    BaseMinimumService(db, clock).set(OCTOBER, Money(2_000))
    clock.set(datetime(2026, 11, 2, 9, 0, tzinfo=UTC))
    ExpenseService(db, clock).create("Листопад", None, Money(500), REMAINDER)
    october, november = analyse(db, OCTOBER), analyse(db, NOVEMBER)
    assert october.actual_expenses == Money(1_000) and october.base_minimum == Money(2_000)
    assert november.actual_expenses == Money(500) and november.base_minimum is None


@pytest.mark.parametrize(
    ("minimum", "outcome", "difference"),
    [
        (0, MinimumComparison.GREATER, 3_000),
        (3_000, MinimumComparison.EQUAL, 0),
        (2_000, MinimumComparison.GREATER, 1_000),
        (5_000, MinimumComparison.LESS, 2_000),
    ],
)
def test_base_minimum_comparison(db, clock, setup, minimum, outcome, difference):
    ExpenseService(db, clock).create("Продукти", None, Money(3_000), REMAINDER)
    ReplenishmentService(db, clock).create(
        "Не витрата", None, setup[0], [ReplenishmentPart(REMAINDER, Money(9_000))]
    )
    BaseMinimumService(db, clock).set(OCTOBER, Money(minimum))
    analysis = analyse(db)
    assert analysis.base_minimum == Money(minimum)
    assert analysis.comparison.outcome is outcome
    assert analysis.comparison.difference == Money(difference)
