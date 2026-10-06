"""Джерела коштів: перевірка достатності й зміна залишків у межах транзакції.

Джерело операції завжди обирає користувач (ADR 0003): тут немає автоматичного
вибору, перемикання чи поділу. Методи ``SourceLedger`` працюють лише всередині
транзакції, відкритої сервісом операції, щоб перевірка й запис були атомарними.
"""

import sqlite3

from budget.domain.calendar import Clock, current_month
from budget.domain.models import SourceKind, SourceRef
from budget.domain.money import Money, require_positive
from budget.errors import DomainRuleError
from budget.services.balances import BalanceService
from budget.storage.repositories import (
    AccumulationRepository,
    GeneralRemainderRepository,
    IncomeRepository,
)
from budget.storage.transaction import transaction


def _require_transaction(connection: sqlite3.Connection) -> None:
    if not connection.in_transaction:
        raise RuntimeError("Зміна джерела коштів можлива лише всередині транзакції сервісу")


class InsufficientFundsError(DomainRuleError):
    """Сума перевищує доступний залишок обраного джерела (ADR 0003, п. 7)."""

    def __init__(self, source_name: str, available: Money, required: Money) -> None:
        # Суми форматує лише інтерфейс (budget.ui.formatting); тут — структуровані дані.
        self.source_name = source_name
        self.available = available
        self.required = required
        super().__init__(
            f"Недостатньо коштів у джерелі «{source_name}». Зменште суму або оберіть інше джерело."
        )


GENERAL_REMAINDER_NAME = "Загальний нерозподілений залишок"


class SourceLedger:
    def __init__(self, connection: sqlite3.Connection, clock: Clock) -> None:
        self._connection = connection
        self._clock = clock
        self._balances = BalanceService(connection)
        self._incomes = IncomeRepository(connection)
        self._accumulations = AccumulationRepository(connection)
        self._remainder = GeneralRemainderRepository(connection)

    def require_available(self, source: SourceRef, amount: Money) -> None:
        """Перевіряє, що обране джерело може дати ``amount``. Нічого не змінює."""
        require_positive(amount)
        if source.kind is SourceKind.GENERAL_REMAINDER:
            available = self._remainder.get()
            name = GENERAL_REMAINDER_NAME
        elif source.kind is SourceKind.INCOME:
            income = self._incomes.get(source.income_id)
            if income is None:
                raise DomainRuleError("Дохід не знайдено.")
            if income.archived:
                raise DomainRuleError(f"Дохід «{income.name}» архівовано; він не є джерелом.")
            if income.month != current_month(self._clock):
                raise DomainRuleError(
                    f"Дохід «{income.name}» є джерелом лише у своєму календарному місяці."
                )
            available = self._balances.income_balance(income)
            name = income.name
        else:
            accumulation = self._accumulations.get(source.accumulation_id)
            if accumulation is None:
                raise DomainRuleError("Накопичення не знайдено.")
            if accumulation.archived:
                raise DomainRuleError(
                    f"Накопичення «{accumulation.name}» в архіві; "
                    "воно не є джерелом нових операцій."
                )
            available = self._balances.accumulation_balance(accumulation)
            name = accumulation.name
        if amount > available:
            raise InsufficientFundsError(name, available, amount)

    def debit_general_remainder(self, amount: Money) -> None:
        _require_transaction(self._connection)
        self.require_available(SourceRef(SourceKind.GENERAL_REMAINDER), amount)
        self._remainder.set(self._remainder.get() - amount)

    def credit_general_remainder(self, amount: Money) -> None:
        _require_transaction(self._connection)
        require_positive(amount)
        self._remainder.set(self._remainder.get() + amount)

    def settle_income(self, income_id: int) -> None:
        """Після операції з доходом: нульовий залишок — автоматичне архівування (ADR 0009)."""
        _require_transaction(self._connection)
        income = self._incomes.get(income_id)
        if income is None or income.archived:
            return
        balance = self._balances.income_balance(income)
        if balance.is_negative:
            raise DomainRuleError("Залишок доходу не може бути від'ємним.")
        if balance.is_zero:
            self._incomes.set_archived(income_id)

    def require_income_not_revived(self, income_id: int) -> None:
        """Q168, Q174: зміна, що повернула б кошти архівованому доходу, заблокована."""
        income = self._incomes.get(income_id)
        if income is not None and income.archived:
            raise DomainRuleError(
                f"Зміну не можна зберегти: дохід «{income.name}» уже архівовано, "
                "а ця зміна повернула б йому кошти."
            )


class GeneralRemainderService:
    """Публічні атомарні операції із загальним нерозподіленим залишком."""

    def __init__(self, connection: sqlite3.Connection, clock: Clock) -> None:
        self._connection = connection
        self._ledger = SourceLedger(connection, clock)

    def current(self) -> Money:
        return GeneralRemainderRepository(self._connection).get()

    def require_available(self, amount: Money) -> None:
        self._ledger.require_available(SourceRef(SourceKind.GENERAL_REMAINDER), amount)

    def increase(self, amount: Money) -> None:
        with transaction(self._connection):
            self._ledger.credit_general_remainder(amount)

    def decrease(self, amount: Money) -> None:
        with transaction(self._connection):
            self._ledger.debit_general_remainder(amount)
