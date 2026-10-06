"""Єдине місце грошових розрахунків залишків (ADR 0003, ADR 0009, ADR 0013, ADR 0018).

Залишки доходів і накопичень похідні від операцій; збережено лише загальний
нерозподілений залишок. Сервіси й інтерфейс отримують суми лише звідси.
"""

import sqlite3
from dataclasses import dataclass

from budget.domain.models import Accumulation, Income, IncomeStatus, income_status
from budget.domain.money import Money
from budget.storage.repositories import (
    AccumulationRepository,
    GeneralRemainderRepository,
    IncomeRepository,
)


@dataclass(frozen=True, slots=True)
class IncomeView:
    income: Income
    balance: Money

    @property
    def status(self) -> IncomeStatus:
        return income_status(self.balance)


@dataclass(frozen=True, slots=True)
class AvailableFunds:
    """Загальна доступна сума та її склад (ADR 0003, п. 10; ADR 0013, Q187; ADR 0018)."""

    general_remainder: Money
    active_incomes: Money
    accumulations: Money

    @property
    def total(self) -> Money:
        return self.general_remainder + self.active_incomes + self.accumulations


class BalanceService:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._incomes = IncomeRepository(connection)
        self._accumulations = AccumulationRepository(connection)
        self._remainder = GeneralRemainderRepository(connection)

    def general_remainder(self) -> Money:
        return self._remainder.get()

    def income_balance(self, income: Income) -> Money:
        if income.id is None:
            raise ValueError("Дохід ще не збережено")
        return income.amount - self._incomes.charged_total(income.id)

    def income_view(self, income: Income) -> IncomeView:
        return IncomeView(income, self.income_balance(income))

    def accumulation_balance(self, accumulation: Accumulation) -> Money:
        if accumulation.id is None:
            raise ValueError("Накопичення ще не збережено")
        return accumulation.initial_balance + self._accumulations.movement_total(accumulation.id)

    def available_funds(self) -> AvailableFunds:
        """Борги й базовий мінімум не віднімаються; архівовані доходи не входять;
        залишки архівованих накопичень входять."""
        active = Money.zero()
        for income in self._incomes.list_unarchived():
            balance = self.income_balance(income)
            if balance.is_positive:
                active = active + balance
        accumulations = Money.zero()
        for accumulation in self._accumulations.list_all():
            accumulations = accumulations + self.accumulation_balance(accumulation)
        return AvailableFunds(self._remainder.get(), active, accumulations)
