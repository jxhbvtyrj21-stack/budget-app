"""Доходи (ADR 0009, ADR 0022).

Дохід створюється лише в поточному календарному місяці за Europe/Kyiv; дату
користувач не вводить. Після створення сума, місяць, назва й опис не змінюються;
видалення, скасування й відновлення немає — помилка виправляється новим доходом.
Залишок похідний від операцій; статус визначає система; при нульовому залишку
дохід архівується автоматично (``SourceLedger.settle_income``).
"""

import sqlite3

from budget.domain.calendar import CalendarMonth, Clock, current_month
from budget.domain.models import Income
from budget.domain.money import Money
from budget.errors import DomainRuleError
from budget.services.balances import BalanceService, IncomeView
from budget.services.setup import require_normal_operation
from budget.storage.repositories import IncomeRepository
from budget.storage.transaction import transaction


class IncomeService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock) -> None:
        self._connection = connection
        self._clock = clock
        self._incomes = IncomeRepository(connection)
        self._balances = BalanceService(connection)

    def create(self, name: str, description: str | None, amount: Money) -> IncomeView:
        require_normal_operation(self._connection)
        income = Income(None, current_month(self._clock), name, description, amount)
        with transaction(self._connection):
            income_id = self._incomes.insert(income)
        return self.get(income_id)

    def get(self, income_id: int) -> IncomeView:
        income = self._incomes.get(income_id)
        if income is None:
            raise DomainRuleError("Дохід не знайдено.")
        return self._balances.income_view(income)

    def list_for_month(self, month: CalendarMonth) -> list[IncomeView]:
        return [self._balances.income_view(i) for i in self._incomes.list_for_month(month)]

    def current_sources(self) -> list[IncomeView]:
        """Доходи, які можна обрати джерелом зараз: поточний місяць, не архівовані,
        із позитивним залишком (ADR 0009, п. 4)."""
        views = self.list_for_month(current_month(self._clock))
        return [v for v in views if not v.income.archived and v.balance.is_positive]
