"""Регресія аналізу місяця й базового мінімуму: узгодженість сум, історія, перехід місяця.

ADR 0002, ADR 0009, ADR 0010 (Q153, Q155), ADR 0018, ADR 0020, ADR 0022.
"""

from datetime import UTC, datetime

import pytest

from budget.domain.calendar import CalendarMonth
from budget.domain.models import AccumulationStatus, ReplenishmentPart, SourceKind, SourceRef
from budget.domain.money import Money
from budget.errors import DomainRuleError
from budget.services.accumulation import AccumulationService
from budget.services.balances import BalanceService
from budget.services.base_minimum import BaseMinimumService
from budget.services.debt import DebtService
from budget.services.expense import ExpenseService
from budget.services.income import IncomeService
from budget.services.month import MonthTransitionService
from budget.services.month_analysis import MonthAnalysisService
from budget.services.replenishment import ReplenishmentService
from budget.services.setup import (
    InitialAccumulation,
    InitialDebt,
    InitialSetupService,
    SetupDraft,
)
from budget.storage.repositories import AccumulationRepository, FinancialRecordRepository

OCTOBER = CalendarMonth(2026, 10)
NOVEMBER = CalendarMonth(2026, 11)
REMAINDER = SourceRef(SourceKind.GENERAL_REMAINDER)


@pytest.fixture
def ids(db, clock):
    InitialSetupService(db, clock).complete(
        SetupDraft(
            general_remainder=Money(100_000),
            accumulations=(
                InitialAccumulation("Подорож", None, Money(30_000)),
                InitialAccumulation("Ремонт", None, Money(20_000)),
            ),
            debts=(InitialDebt("Позика", None, Money(15_000)),),
        )
    )
    trip, repair = (a.id for a in AccumulationRepository(db).list_all())
    debt = DebtService(db, clock).list_active()[0].debt.id
    return trip, repair, debt


def acc(accumulation_id):
    return SourceRef(SourceKind.ACCUMULATION, accumulation_id=accumulation_id)


def busy_month(db, clock, ids):
    trip, repair, debt = ids
    income = IncomeService(db, clock).create("Зарплата", None, Money(50_000)).income
    income_source = SourceRef(SourceKind.INCOME, income_id=income.id)
    expenses = ExpenseService(db, clock)
    expenses.create("Продукти", None, Money(12_000), income_source)
    expenses.create("Комунальні", None, Money(4_000), REMAINDER)
    expenses.create("Квитки", None, Money(6_000), acc(trip))
    ReplenishmentService(db, clock).create(
        "Відкладаю",
        None,
        repair,
        [
            ReplenishmentPart(income_source, Money(8_000)),
            ReplenishmentPart(REMAINDER, Money(2_000)),
        ],
    )
    debts = DebtService(db, clock)
    debts.receive_loan("Картка", None, Money(9_000))
    debts.repay(debt, Money(3_000), REMAINDER)
    debts.repay(debt, Money(1_000), acc(trip))


def test_totals_explain_change_of_available_funds(db, clock, ids):
    """Доходи + позикові кошти − фактичні витрати − погашення = зміна доступної суми;
    поповнення — переміщення всередині неї, базовий мінімум не впливає."""
    balances = BalanceService(db)
    before = balances.available_funds().total
    busy_month(db, clock, ids)
    BaseMinimumService(db, clock).set(OCTOBER, Money(1_000_000))
    analysis = MonthAnalysisService(db).analyse(OCTOBER)
    change = (
        analysis.incomes
        + analysis.loan_receipts
        - analysis.actual_expenses
        - analysis.debt_repayments
    )
    assert balances.available_funds().total - before == change == Money(33_000)
    assert analysis.actual_expenses == Money(22_000)
    assert analysis.replenishments == Money(10_000) and analysis.debt_repayments == Money(4_000)
    assert analysis.loan_receipts == Money(9_000)
    by_source = analysis.expenses_by_source
    assert sum((v for v in by_source.values()), Money.zero()) == analysis.actual_expenses


def test_accumulation_net_changes_explain_total_accumulation_change(db, clock, ids):
    balances = BalanceService(db)
    before = balances.available_funds().accumulations
    busy_month(db, clock, ids)
    movements = MonthAnalysisService(db).analyse(OCTOBER).accumulation_movements
    net = sum((m.net_change for m in movements), Money.zero())
    assert balances.available_funds().accumulations - before == net == Money(3_000)
    by_name = {m.name: m for m in movements}
    assert by_name["Подорож"].repaid == Money(1_000)  # погашення з накопичення — відтік
    assert by_name["Подорож"].net_change == Money(-7_000)
    assert by_name["Ремонт"].net_change == Money(10_000)


def test_base_minimum_alone_does_not_make_a_month_non_empty(db, clock, ids):
    """Базовий мінімум не є фінансовим записом: тривала перерва визначається як раніше."""
    IncomeService(db, clock).create("Жовтень", None, Money(5_000))
    clock.set(datetime(2026, 11, 3, 9, 0, tzinfo=UTC))
    MonthTransitionService(db, clock).run_on_startup()
    BaseMinimumService(db, clock).set(NOVEMBER, Money(20_000))
    assert NOVEMBER not in FinancialRecordRepository(db).months_with_records()
    IncomeService(db, clock).create("Листопад", None, Money(7_000))
    clock.set(datetime(2027, 1, 5, 9, 0, tzinfo=UTC))
    pending = MonthTransitionService(db, clock).run_on_startup()
    # Грудень порожній: між листопадом і січнем перерва — запит на рішення.
    assert pending is not None and pending.total == Money(7_000)
    assert BaseMinimumService(db, clock).get(CalendarMonth(2027, 1)) is None


def test_historical_analysis_and_read_only(db, clock, ids):
    trip, _, _ = ids
    busy_month(db, clock, ids)
    BaseMinimumService(db, clock).set(OCTOBER, Money(30_000))
    october = MonthAnalysisService(db).analyse(OCTOBER)
    accumulations = AccumulationService(db, clock)
    clock.set(datetime(2026, 11, 3, 9, 0, tzinfo=UTC))
    MonthTransitionService(db, clock).run_on_startup()
    # Витратити залишок «Подорожі», закрити й архівувати.
    remaining = BalanceService(db).accumulation_balance(AccumulationRepository(db).get(trip))
    ExpenseService(db, clock).create("Решта", None, remaining, acc(trip))
    accumulations.change_status(trip, AccumulationStatus.CLOSED)
    accumulations.archive(trip)
    again = MonthAnalysisService(db).analyse(OCTOBER)
    assert again.actual_expenses == october.actual_expenses
    assert [m.net_change for m in again.accumulation_movements] == [
        m.net_change for m in october.accumulation_movements
    ]
    assert any(m.archived for m in again.accumulation_movements)
    with pytest.raises(DomainRuleError):
        BaseMinimumService(db, clock).set(OCTOBER, Money(1))
    assert again.base_minimum == Money(30_000)


def test_analysis_is_read_only(db, clock, ids):
    busy_month(db, clock, ids)
    snapshot = [
        db.execute(f"SELECT * FROM {t}").fetchall()
        for t in (
            "incomes",
            "expenses",
            "replenishments",
            "replenishment_parts",
            "debts",
            "debt_repayments",
            "general_remainder",
            "accumulations",
            "base_minimums",
        )
    ]
    MonthAnalysisService(db).analyse(OCTOBER)
    MonthAnalysisService(db).analyse(NOVEMBER)
    assert snapshot == [
        db.execute(f"SELECT * FROM {t}").fetchall()
        for t in (
            "incomes",
            "expenses",
            "replenishments",
            "replenishment_parts",
            "debts",
            "debt_repayments",
            "general_remainder",
            "accumulations",
            "base_minimums",
        )
    ]
