"""Борги: отримання позикових коштів, погашення й метадані (ADR 0018, ADR 0022, ADR 0002).

Кожне отримання позикових коштів створює окремий борг; його сума одразу
зараховується до загального нерозподіленого залишку й не є доходом. Початковий борг
із майстра не є отриманням коштів і як «початковий» не змінюється (Q172). Залишок
і статус боргу похідні — їх рахує ``BalanceService``. Назва й опис боргу — метадані,
їх можна змінити будь-коли. Фінансові поля операцій — лише в поточному місяці.

Погашений борг повторно не відкривається: зміна поточного місяця, після якої в
боргу з нульовим залишком знову з'явився б залишок, не зберігається.
"""

import sqlite3

from budget.domain.calendar import CalendarMonth, Clock, current_month
from budget.domain.models import Debt, DebtOrigin, DebtStatus, optional_description, require_name
from budget.domain.money import Money, require_positive
from budget.errors import DomainRuleError
from budget.services.balances import BalanceService, DebtView
from budget.services.expense import HISTORICAL_READ_ONLY
from budget.services.setup import require_normal_operation
from budget.services.sources import SourceLedger
from budget.storage.repositories import DebtRepository
from budget.storage.transaction import transaction

INITIAL_DEBT_FIXED = (
    "Початковий борг із первинного налаштування не змінюється й не видаляється; "
    "його залишок зменшують погашення."
)


class DebtRepaidFloorError(DomainRuleError):
    """Суму боргу не можна зробити меншою за вже погашене, а борг із погашеннями —
    видалити (ADR 0018, п. 5). Суми форматує інтерфейс."""

    def __init__(self, name: str, repaid: Money) -> None:
        self.name = name
        self.repaid = repaid
        super().__init__(f"Борг «{name}» має погашення: його суму не можна зменшити чи видалити.")


class DebtReopenError(DomainRuleError):
    """Погашений борг не відкривається повторно (ADR 0018, п. 4; рішення власниці продукту)."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(
            f"Борг «{name}» уже погашено. Зміну, після якої в нього знову з'явився б "
            "залишок, не можна зберегти. Нові позикові кошти оформіть новим боргом."
        )


class DebtService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock) -> None:
        self._connection = connection
        self._clock = clock
        self._debts = DebtRepository(connection)
        self._balances = BalanceService(connection)
        self._ledger = SourceLedger(connection, clock)

    # Читання ---------------------------------------------------------------------------

    def get(self, debt_id: int) -> DebtView:
        return self._balances.debt_view(self._require(debt_id))

    def list_active(self) -> list[DebtView]:
        return [v for v in self._views() if v.status is DebtStatus.ACTIVE]

    def list_paid(self) -> list[DebtView]:
        return [v for v in self._views() if v.status is DebtStatus.PAID]

    def list_for_month(self, month: CalendarMonth) -> list[DebtView]:
        """Отримання позикових коштів місяця (від нових до старих)."""
        return [self._balances.debt_view(d) for d in self._debts.list_for_month(month)]

    def active_total(self) -> Money:
        return self._balances.active_debts_total()

    def loan_lock_reason(self, debt: Debt) -> str | None:
        """Чому суму отримання не можна змінити чи видалити зараз; ``None`` — можна."""
        if debt.origin is DebtOrigin.INITIAL:
            return INITIAL_DEBT_FIXED
        if debt.month != current_month(self._clock):
            return HISTORICAL_READ_ONLY
        return None

    # Отримання позикових коштів -------------------------------------------------------

    def receive_loan(self, name: str, description: str | None, amount: Money) -> DebtView:
        """Новий борг поточного місяця; сума — до загального нерозподіленого залишку."""
        require_normal_operation(self._connection)
        debt = Debt(
            None, DebtOrigin.LOAN_RECEIPT, current_month(self._clock), name, description, amount
        )
        with transaction(self._connection):
            debt_id = self._debts.insert(debt)
            self._ledger.credit_general_remainder(debt.amount)
        return self.get(debt_id)

    def update_loan_amount(self, debt_id: int, amount: Money) -> DebtView:
        """Змінює суму отримання поточного місяця (ADR 0018, п. 5).

        Не нижче вже погашеного; зменшення не робить нерозподілений залишок
        від'ємним; погашений борг таким збільшенням не відкривається повторно.
        """
        require_normal_operation(self._connection)
        require_positive(amount)
        with transaction(self._connection):
            view = self._balances.debt_view(self._require_changeable_loan(debt_id))
            debt = view.debt
            if amount < view.repaid:
                raise DebtRepaidFloorError(debt.name, view.repaid)
            if view.remaining.is_zero and amount > view.repaid:
                raise DebtReopenError(debt.name)
            if amount > debt.amount:
                self._ledger.credit_general_remainder(amount - debt.amount)
            elif amount < debt.amount:
                self._ledger.debit_general_remainder(debt.amount - amount)
            self._debts.update_amount(debt_id, amount)
        return self.get(debt_id)

    def delete_loan(self, debt_id: int) -> None:
        """Видаляє отримання поточного місяця разом із боргом — лише без погашень."""
        require_normal_operation(self._connection)
        with transaction(self._connection):
            view = self._balances.debt_view(self._require_changeable_loan(debt_id))
            if view.repaid.is_positive:
                raise DebtRepaidFloorError(view.debt.name, view.repaid)
            self._ledger.debit_general_remainder(view.debt.amount)
            self._debts.delete(debt_id)

    # Метадані --------------------------------------------------------------------------

    def update_metadata(self, debt_id: int, name: str, description: str | None) -> DebtView:
        """Назва й опис боргу змінюються будь-коли (ADR 0022): для активного,
        погашеного, початкового й створеного в минулому місяці; суми не змінюються."""
        require_normal_operation(self._connection)
        cleaned_name, cleaned_description = require_name(name), optional_description(description)
        with transaction(self._connection):
            self._require(debt_id)
            self._debts.update_metadata(debt_id, cleaned_name, cleaned_description)
        return self.get(debt_id)

    # Допоміжне -------------------------------------------------------------------------

    def _require(self, debt_id: int) -> Debt:
        debt = self._debts.get(debt_id)
        if debt is None:
            raise DomainRuleError("Борг не знайдено.")
        return debt

    def _require_changeable_loan(self, debt_id: int) -> Debt:
        debt = self._require(debt_id)
        reason = self.loan_lock_reason(debt)
        if reason is not None:
            raise DomainRuleError(reason)
        return debt

    def _views(self) -> list[DebtView]:
        return [self._balances.debt_view(d) for d in self._debts.list_all()]
