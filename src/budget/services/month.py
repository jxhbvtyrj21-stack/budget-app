"""Календарний місяць і перехід між місяцями для доходів (ADR 0009, ADR 0010).

Поточний місяць визначає лише годинник сервісного шару (Europe/Kyiv). Окремого
стану місяця, ознаки «оброблено» чи операції переказу немає: результат переходу —
архівовані доходи й змінений загальний нерозподілений залишок.
"""

import sqlite3
from dataclasses import dataclass
from enum import StrEnum

from budget.domain.calendar import CalendarMonth, Clock, current_month
from budget.domain.models import Income
from budget.domain.money import Money
from budget.services.balances import BalanceService
from budget.services.setup import InitialSetupService
from budget.services.sources import SourceLedger
from budget.storage.repositories import FinancialRecordRepository, IncomeRepository
from budget.storage.transaction import transaction


class MonthService:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def current_month(self) -> CalendarMonth:
        return current_month(self._clock)

    def is_current(self, month: CalendarMonth) -> bool:
        """Редагувати можна лише операції поточного місяця; минулі — лише перегляд."""
        return month == self.current_month()


class LongGapChoice(StrEnum):
    """Вибір у спеціальному діалозі після тривалої перерви (ADR 0009, п. 7–9)."""

    TRANSFER = "transfer"  # «Перенести»
    REMOVE = "remove"  # «Вилучити»


@dataclass(frozen=True, slots=True)
class PendingLongGap:
    """Невирішені позитивні залишки доходів минулих місяців — одна загальна сума."""

    total: Money


class MonthTransitionService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock) -> None:
        self._connection = connection
        self._clock = clock
        self._incomes = IncomeRepository(connection)
        self._records = FinancialRecordRepository(connection)
        self._balances = BalanceService(connection)
        self._ledger = SourceLedger(connection, clock)

    def run_on_startup(self) -> PendingLongGap | None:
        """Обробляє минулі місяці під час запуску.

        Звичайний перехід (останній місяць із записами — попередній): позитивні залишки
        доходів консолідуються в загальний нерозподілений залишок, доходи архівуються.
        Після тривалої перерви (між останнім місяцем із записами й поточним є порожні
        місяці): доходи з нульовим залишком архівуються, а для позитивних залишків
        повертається один загальний запит на рішення. До завершення налаштування
        перехід не застосовується (Q176).
        """
        if not InitialSetupService(self._connection).is_completed():
            return None
        current = current_month(self._clock)
        with transaction(self._connection):
            pending = self._past_unarchived(current)
            if pending:
                past_record_months = [m for m in self._records.months_with_records() if m < current]
                normal = max(past_record_months) == current.previous()
                for income, balance in pending:
                    if normal:
                        if balance.is_positive:
                            self._ledger.credit_general_remainder(balance)
                        self._incomes.set_archived(income.id)
                    elif balance.is_zero:
                        self._incomes.set_archived(income.id)
        return self.pending_long_gap()

    def pending_long_gap(self) -> PendingLongGap | None:
        positives = [b for _, b in self._past_unarchived(current_month(self._clock))]
        total = sum((b for b in positives if b.is_positive), Money.zero())
        return PendingLongGap(total) if total.is_positive else None

    def resolve_long_gap(self, choice: LongGapChoice) -> None:
        """Одне рішення для всіх відповідних місяців і доходів (ADR 0009, п. 7).

        «Перенести» додає позитивні залишки до загального нерозподіленого залишку;
        «Вилучити» прибирає їх із доступних коштів. В обох випадках доходи
        архівуються; накопичення й базовий мінімум не змінюються; окремих записів,
        ознак чи операцій не створюється.
        """
        choice = LongGapChoice(choice)
        current = current_month(self._clock)
        with transaction(self._connection):
            for income, balance in self._past_unarchived(current):
                if choice is LongGapChoice.TRANSFER and balance.is_positive:
                    self._ledger.credit_general_remainder(balance)
                self._incomes.set_archived(income.id)

    def _past_unarchived(self, current: CalendarMonth) -> list[tuple[Income, Money]]:
        return [
            (income, self._balances.income_balance(income))
            for income in self._incomes.list_unarchived()
            if income.month < current
        ]
