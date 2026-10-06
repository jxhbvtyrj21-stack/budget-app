"""Межі сервісів фінансових операцій, які реалізуються на наступних етапах.

Доходи — ``budget.services.income``, звичайні витрати — ``budget.services.expense``,
накопичення — ``budget.services.accumulation``.

Кожен сервіс перед зміною даних викликає ``require_normal_operation`` (Q190), сам
відкриває транзакцію й застосовує правила поточного місяця через ``MonthService``.
"""

import sqlite3

from budget.domain.calendar import Clock
from budget.services.month import MonthService


class _OperationService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock) -> None:
        self._connection = connection
        self._months = MonthService(clock)


class DebtService(_OperationService):
    """Отримання позикових коштів і погашення боргу; початковий борг не є
    отриманням позикових коштів (ADR 0018, ADR 0022)."""
