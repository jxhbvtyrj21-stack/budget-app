"""Базовий мінімум місяця (ADR 0002, ADR 0019, ADR 0022).

Інформаційний орієнтир для календарного місяця, а не план чи ліміт: він не є
фінансовим записом, не змінює залишків, не створює боргу й не робить місяць
непорожнім. Задати чи змінити його можна лише для поточного місяця й лише після
завершення первинного налаштування (Q190); минулі місяці — лише перегляд.
Автоматичного перенесення на наступний місяць немає (ADR 0002, п. 10).
"""

import sqlite3

from budget.domain.calendar import CalendarMonth, Clock, current_month
from budget.domain.models import BaseMinimum
from budget.domain.money import Money
from budget.errors import DomainRuleError
from budget.services.expense import HISTORICAL_READ_ONLY
from budget.services.setup import require_normal_operation
from budget.storage.repositories import BaseMinimumRepository
from budget.storage.transaction import transaction


class BaseMinimumService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock) -> None:
        self._connection = connection
        self._clock = clock
        self._repository = BaseMinimumRepository(connection)

    def get(self, month: CalendarMonth) -> Money | None:
        """Значення будь-якого місяця; ``None`` — не задано."""
        base_minimum = self._repository.get(month)
        return base_minimum.amount if base_minimum is not None else None

    def is_editable(self, month: CalendarMonth) -> bool:
        return month == current_month(self._clock)

    def set(self, month: CalendarMonth, amount: Money) -> Money:
        """Задає або змінює базовий мінімум поточного місяця."""
        require_normal_operation(self._connection)
        if not self.is_editable(month):
            raise DomainRuleError(HISTORICAL_READ_ONLY)
        base_minimum = BaseMinimum(month, amount)
        with transaction(self._connection):
            self._repository.set(base_minimum)
        return base_minimum.amount
