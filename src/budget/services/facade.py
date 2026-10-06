"""Набір сервісів для інтерфейсу. Збирається в ``app.py``; UI не бачить SQLite."""

import sqlite3
from dataclasses import dataclass

from budget.domain.calendar import Clock
from budget.services.balances import BalanceService
from budget.services.income import IncomeService
from budget.services.month import MonthService, MonthTransitionService
from budget.services.setup import InitialSetupService


@dataclass(frozen=True, slots=True)
class AppServices:
    setup: InitialSetupService
    incomes: IncomeService
    balances: BalanceService
    months: MonthService
    transitions: MonthTransitionService

    @classmethod
    def create(cls, connection: sqlite3.Connection, clock: Clock) -> "AppServices":
        return cls(
            setup=InitialSetupService(connection, clock),
            incomes=IncomeService(connection, clock),
            balances=BalanceService(connection),
            months=MonthService(clock),
            transitions=MonthTransitionService(connection, clock),
        )
