"""Аналіз календарного місяця (ADR 0020; ADR 0018, ADR 0019, ADR 0002).

Лише читання й агрегування фактичних операцій місяця — не розрахунок поточних
залишків. Окремих збережених підсумків чи сутностей місяця немає.

- Доходи місяця — суми доходів, створених у місяці (без початкового балансу й
  без отриманих позикових коштів).
- Фактичні витрати — усі звичайні витрати місяця з розбивкою за типом джерела;
  поповнення накопичень і погашення боргів до них не входять.
- Окремо: поповнення накопичень, погашення боргів, отримані позикові кошти.
- Рух накопичень: для кожного накопичення з операціями в місяці — поповнення,
  звичайні витрати з нього й погашення боргів із нього (рішення власниці
  продукту, варіант А); чиста зміна дорівнює фактичній зміні його залишку за місяць.
  Архівність не приховує історичних операцій.
- Базовий мінімум і порівняння — інформаційний орієнтир, окремо від фінансових сум.
"""

import sqlite3
from dataclasses import dataclass

from budget.domain.calendar import CalendarMonth
from budget.domain.models import BaseMinimumComparison, SourceKind, compare_with_base_minimum
from budget.domain.money import Money
from budget.storage.repositories import (
    AccumulationRepository,
    BaseMinimumRepository,
    DebtRepository,
    ExpenseRepository,
    FinancialRecordRepository,
    IncomeRepository,
    ReplenishmentRepository,
)


@dataclass(frozen=True, slots=True)
class AccumulationMovement:
    accumulation_id: int
    name: str
    archived: bool
    replenished: Money
    spent: Money  # звичайні витрати з накопичення
    repaid: Money  # погашення боргів із накопичення

    @property
    def net_change(self) -> Money:
        """Чиста зміна залишку за місяць: надходження мінус обидва види відтоку."""
        return self.replenished - self.spent - self.repaid


@dataclass(frozen=True, slots=True)
class MonthAnalysis:
    month: CalendarMonth
    incomes: Money
    actual_expenses: Money
    expenses_by_source: dict[SourceKind, Money]
    replenishments: Money
    debt_repayments: Money
    loan_receipts: Money
    accumulation_movements: tuple[AccumulationMovement, ...]
    base_minimum: Money | None
    comparison: BaseMinimumComparison | None


def _sum(amounts) -> Money:
    total = Money.zero()
    for amount in amounts:
        total = total + amount
    return total


class MonthAnalysisService:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._incomes = IncomeRepository(connection)
        self._expenses = ExpenseRepository(connection)
        self._replenishments = ReplenishmentRepository(connection)
        self._debts = DebtRepository(connection)
        self._accumulations = AccumulationRepository(connection)
        self._base_minimums = BaseMinimumRepository(connection)
        self._records = FinancialRecordRepository(connection)

    def has_financial_records(self) -> bool:
        """Чи є хоч один фінансовий запис у будь-якому місяці (лише читання).

        Записи — доходи, витрати, поповнення, отримання позикових коштів і погашення
        (``months_with_records``); стартовий стан майстра записом не є. Баланс і
        поточний місяць на відповідь не впливають (порожній «Огляд», IA 12).
        """
        return bool(self._records.months_with_records())

    def analyse(self, month: CalendarMonth) -> MonthAnalysis:
        expenses = self._expenses.list_for_month(month)
        replenishments = self._replenishments.list_for_month(month)
        repayments = self._debts.list_repayments_for_month(month)

        by_source = {kind: Money.zero() for kind in SourceKind}
        for expense in expenses:
            by_source[expense.source.kind] = by_source[expense.source.kind] + expense.amount
        actual = _sum(e.amount for e in expenses)

        base = self._base_minimums.get(month)
        base_amount = base.amount if base is not None else None
        return MonthAnalysis(
            month=month,
            incomes=_sum(i.amount for i in self._incomes.list_for_month(month)),
            actual_expenses=actual,
            expenses_by_source=by_source,
            replenishments=_sum(r.total for r in replenishments),
            debt_repayments=_sum(r.amount for r in repayments),
            loan_receipts=_sum(d.amount for d in self._debts.list_for_month(month)),
            accumulation_movements=self._movements(expenses, replenishments, repayments),
            base_minimum=base_amount,
            comparison=(
                compare_with_base_minimum(actual, base_amount) if base_amount is not None else None
            ),
        )

    def _movements(self, expenses, replenishments, repayments) -> tuple[AccumulationMovement, ...]:
        totals: dict[int, list[Money]] = {}

        def add(accumulation_id: int, index: int, amount: Money) -> None:
            entry = totals.setdefault(accumulation_id, [Money.zero()] * 3)
            entry[index] = entry[index] + amount

        for replenishment in replenishments:
            add(replenishment.accumulation_id, 0, replenishment.total)
        for expense in expenses:
            if expense.source.kind is SourceKind.ACCUMULATION:
                add(expense.source.accumulation_id, 1, expense.amount)
        for repayment in repayments:
            if repayment.source.kind is SourceKind.ACCUMULATION:
                add(repayment.source.accumulation_id, 2, repayment.amount)

        movements = []
        for accumulation_id in sorted(totals):
            accumulation = self._accumulations.get(accumulation_id)
            replenished, spent, repaid = totals[accumulation_id]
            movements.append(
                AccumulationMovement(
                    accumulation_id,
                    accumulation.name,
                    accumulation.archived,
                    replenished,
                    spent,
                    repaid,
                )
            )
        return tuple(movements)
