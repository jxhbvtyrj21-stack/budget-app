"""Набір сервісів для інтерфейсу. Збирається в ``app.py``; UI не бачить SQLite."""

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from budget.domain.calendar import Clock
from budget.services.accumulation import AccumulationService
from budget.services.backup import BackupService
from budget.services.balances import BalanceService
from budget.services.base_minimum import BaseMinimumService
from budget.services.debt import DebtService
from budget.services.expense import ExpenseService
from budget.services.income import IncomeService
from budget.services.month import MonthService, MonthTransitionService
from budget.services.month_analysis import MonthAnalysisService
from budget.services.replenishment import ReplenishmentService
from budget.services.setup import InitialSetupService


@dataclass(frozen=True, slots=True)
class AppServices:
    setup: InitialSetupService
    incomes: IncomeService
    expenses: ExpenseService
    accumulations: AccumulationService
    replenishments: ReplenishmentService
    debts: DebtService
    base_minimums: BaseMinimumService
    analysis: MonthAnalysisService
    balances: BalanceService
    months: MonthService
    transitions: MonthTransitionService
    backups: BackupService | None = None  # лише коли відома тека копій (екран «Сервіс»)

    @classmethod
    def create(
        cls, connection: sqlite3.Connection, clock: Clock, backups_dir: Path | None = None
    ) -> "AppServices":
        return cls(
            setup=InitialSetupService(connection, clock),
            incomes=IncomeService(connection, clock),
            expenses=ExpenseService(connection, clock),
            accumulations=AccumulationService(connection, clock),
            replenishments=ReplenishmentService(connection, clock),
            debts=DebtService(connection, clock),
            base_minimums=BaseMinimumService(connection, clock),
            analysis=MonthAnalysisService(connection),
            balances=BalanceService(connection),
            months=MonthService(clock),
            transitions=MonthTransitionService(connection, clock),
            backups=None if backups_dir is None else BackupService(connection, backups_dir, clock),
        )
