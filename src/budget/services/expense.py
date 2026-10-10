"""Звичайні витрати (ADR 0003, ADR 0010, ADR 0011, ADR 0013, ADR 0014, ADR 0022).

Рівно одне джерело, яке обирає лише користувач: активний дохід поточного місяця,
загальний нерозподілений залишок або неархіване накопичення. Немає автоматичного
вибору, перемикання, поділу чи добору коштів. Витрата з накопичення — звичайна
витрата (Q170). Створення, зміна й видалення — лише в поточному місяці, кожне однією
транзакцією; минулі місяці — лише перегляд. Базовий мінімум на витрати не впливає.
"""

import sqlite3
from dataclasses import dataclass

from budget.domain.calendar import CalendarMonth, Clock, current_month
from budget.domain.models import Expense, SourceKind, SourceRef
from budget.domain.money import Money
from budget.errors import DomainRuleError
from budget.services.balances import BalanceService
from budget.services.setup import require_normal_operation
from budget.services.sources import GENERAL_REMAINDER_NAME, SourceLedger
from budget.storage.repositories import (
    AccumulationRepository,
    ExpenseRepository,
    IncomeRepository,
)
from budget.storage.transaction import transaction

HISTORICAL_READ_ONLY = "Операції минулих місяців доступні лише для перегляду."


@dataclass(frozen=True, slots=True)
class ExpenseView:
    expense: Expense
    source_name: str


@dataclass(frozen=True, slots=True)
class SourceOption:
    """Джерело для вибору користувачем і його доступний залишок."""

    source: SourceRef
    name: str
    available: Money


class ExpenseService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock) -> None:
        self._connection = connection
        self._clock = clock
        self._expenses = ExpenseRepository(connection)
        self._incomes = IncomeRepository(connection)
        self._accumulations = AccumulationRepository(connection)
        self._balances = BalanceService(connection)
        self._ledger = SourceLedger(connection, clock)

    # Читання ---------------------------------------------------------------------------

    def get(self, expense_id: int) -> ExpenseView:
        expense = self._expenses.get(expense_id)
        if expense is None:
            raise DomainRuleError("Витрату не знайдено.")
        return ExpenseView(expense, self._ledger.source_name(expense.source))

    def list_for_month(self, month: CalendarMonth) -> list[ExpenseView]:
        return [
            ExpenseView(e, self._ledger.source_name(e.source))
            for e in self._expenses.list_for_month(month)
        ]

    def source_options(self, editing: Expense | None = None) -> list[SourceOption]:
        """Джерела, які користувач може обрати зараз, з доступним залишком.

        Під час зміни витрати її поточна сума враховується як доступна в тому самому
        джерелі, бо старий фінансовий ефект знімається перед застосуванням нового.
        """
        options = [
            SourceOption(
                SourceRef(SourceKind.INCOME, income_id=v.income.id), v.income.name, v.balance
            )
            for v in reversed(self._income_sources())
        ]
        options.append(
            SourceOption(
                SourceRef(SourceKind.GENERAL_REMAINDER),
                GENERAL_REMAINDER_NAME,
                self._balances.general_remainder(),
            )
        )
        for accumulation in self._accumulations.list_all():
            if not accumulation.archived:
                options.append(
                    SourceOption(
                        SourceRef(SourceKind.ACCUMULATION, accumulation_id=accumulation.id),
                        accumulation.name,
                        self._balances.accumulation_balance(accumulation),
                    )
                )
        if editing is not None:
            options = [
                SourceOption(o.source, o.name, o.available + editing.amount)
                if o.source == editing.source
                else o
                for o in options
            ]
        return options

    def financial_lock_reason(self, expense: Expense) -> str | None:
        """Причина, з якої суму й джерело витрати змінити не можна; ``None`` — можна."""
        if expense.month != current_month(self._clock):
            return HISTORICAL_READ_ONLY
        try:
            self._ledger.require_financially_changeable(expense.source)
        except DomainRuleError as error:
            return error.user_message
        return None

    # Зміни -----------------------------------------------------------------------------

    def create(
        self, name: str, description: str | None, amount: Money, source: SourceRef
    ) -> ExpenseView:
        require_normal_operation(self._connection)
        expense = Expense(None, current_month(self._clock), name, description, amount, source)
        with transaction(self._connection):
            expense_id = self._apply(expense)
        return self.get(expense_id)

    def update(
        self,
        expense_id: int,
        name: str,
        description: str | None,
        amount: Money,
        source: SourceRef,
    ) -> ExpenseView:
        """Змінює витрату поточного місяця: суму, назву, опис, джерело (Q169).

        Назва й опис — метадані: їх можна змінити й тоді, коли фінансово операція
        заблокована (Q191). Зміна суми чи джерела знімає старий фінансовий ефект і
        застосовує новий в одній транзакції; недопустима нова конфігурація не
        зберігається, а стара витрата лишається без змін.
        """
        require_normal_operation(self._connection)
        with transaction(self._connection):
            old = self._current_month_expense(expense_id)
            new = Expense(old.id, old.month, name, description, amount, source)
            if new.amount == old.amount and new.source == old.source:
                self._expenses.update_metadata(old.id, new.name, new.description)
            else:
                self._ledger.require_financially_changeable(old.source)
                self._revert(old)
                self._apply(new)
        return self.get(expense_id)

    def delete(self, expense_id: int) -> None:
        """Видаляє витрату поточного місяця й повертає кошти джерелу (ADR 0010, Q154).

        Видалення, що повернуло б кошти архівованому доходу, заблоковане (Q168);
        витрату з архівованого накопичення до розархівування не видаляють (Q189).
        """
        require_normal_operation(self._connection)
        with transaction(self._connection):
            old = self._current_month_expense(expense_id)
            self._ledger.require_financially_changeable(old.source)
            self._revert(old)

    def _current_month_expense(self, expense_id: int) -> Expense:
        expense = self._expenses.get(expense_id)
        if expense is None:
            raise DomainRuleError("Витрату не знайдено.")
        if expense.month != current_month(self._clock):
            raise DomainRuleError(HISTORICAL_READ_ONLY)
        return expense

    def _revert(self, expense: Expense) -> None:
        """Знімає фінансовий ефект витрати в поточній транзакції (лише повертає кошти)."""
        self._expenses.delete(expense.id)
        if expense.source.kind is SourceKind.GENERAL_REMAINDER:
            self._ledger.credit_general_remainder(expense.amount)

    def _apply(self, expense: Expense) -> int:
        """Перевіряє обране джерело й записує витрату в поточній транзакції."""
        self._ledger.require_available(expense.source, expense.amount)
        expense_id = self._expenses.insert(expense)
        if expense.source.kind is SourceKind.GENERAL_REMAINDER:
            self._ledger.debit_general_remainder(expense.amount)
        elif expense.source.kind is SourceKind.INCOME:
            self._ledger.settle_income(expense.source.income_id)
        return expense_id

    def _income_sources(self):
        month = current_month(self._clock)
        views = [self._balances.income_view(i) for i in self._incomes.list_for_month(month)]
        return [v for v in views if not v.income.archived and v.balance.is_positive]
