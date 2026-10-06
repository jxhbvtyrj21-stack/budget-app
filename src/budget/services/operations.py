"""Межі сервісів фінансових операцій. Сценарії реалізуються на наступних етапах;
доходи — ``budget.services.income``.

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


class ExpenseService(_OperationService):
    """Звичайні витрати: одне вручну обране джерело, блокування за нестачі коштів,
    редагування й видалення лише в поточному місяці, без відновлення архівованого
    доходу (ADR 0003, ADR 0010, ADR 0011, ADR 0013, ADR 0014)."""


class AccumulationService(_OperationService):
    """Накопичення й поповнення: кілька джерел в одному поповненні, ручні статуси,
    архівування лише закритого, метадані (ADR 0007, ADR 0011–0014, ADR 0022)."""


class DebtService(_OperationService):
    """Отримання позикових коштів і погашення боргу; початковий борг не є
    отриманням позикових коштів (ADR 0018, ADR 0022)."""
