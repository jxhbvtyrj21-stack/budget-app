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
from dataclasses import dataclass

from budget.domain.calendar import CalendarMonth, Clock, current_month
from budget.domain.models import (
    Debt,
    DebtOrigin,
    DebtRepayment,
    DebtStatus,
    SourceKind,
    SourceRef,
    optional_description,
    require_name,
)
from budget.domain.money import Money, require_positive
from budget.errors import DomainRuleError
from budget.services.balances import BalanceService, DebtView
from budget.services.expense import HISTORICAL_READ_ONLY, SourceOption
from budget.services.setup import require_normal_operation
from budget.services.sources import GENERAL_REMAINDER_NAME, SourceLedger
from budget.storage.repositories import AccumulationRepository, DebtRepository, IncomeRepository
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


class DebtOverpaymentError(DomainRuleError):
    """Погашення більше за залишок боргу (ADR 0018, п. 3). Суми форматує інтерфейс."""

    def __init__(self, name: str, remaining: Money, required: Money) -> None:
        self.name = name
        self.remaining = remaining
        self.required = required
        super().__init__(f"Погашення перевищує залишок боргу «{name}».")


@dataclass(frozen=True, slots=True)
class RepaymentView:
    repayment: DebtRepayment
    debt_name: str
    source_name: str


class DebtService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock) -> None:
        self._connection = connection
        self._clock = clock
        self._debts = DebtRepository(connection)
        self._balances = BalanceService(connection)
        self._ledger = SourceLedger(connection, clock)
        self._incomes = IncomeRepository(connection)
        self._accumulations = AccumulationRepository(connection)

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

    # Погашення ---------------------------------------------------------------------------

    def get_repayment(self, repayment_id: int) -> RepaymentView:
        return self._repayment_view(self._require_repayment(repayment_id))

    def repayments_for_month(self, month: CalendarMonth) -> list[RepaymentView]:
        return [self._repayment_view(r) for r in self._debts.list_repayments_for_month(month)]

    def history(self, debt_id: int) -> list[RepaymentView]:
        """Погашення боргу від нових до старих (для картки боргу)."""
        self._require(debt_id)
        return [self._repayment_view(r) for r in self._debts.list_repayments_for_debt(debt_id)]

    def source_options(self, editing: DebtRepayment | None = None) -> list[SourceOption]:
        """Джерела погашення: активні доходи поточного місяця, загальний нерозподілений
        залишок, неархівовані накопичення (ADR 0018, п. 3). Борг джерелом не буває.

        Під час зміни погашення його сума рахується доступною в тому самому джерелі.
        Залишки — з ``BalanceService``; достатність перевіряє ``SourceLedger``.
        """
        month = current_month(self._clock)
        options = []
        for income in reversed(self._incomes.list_for_month(month)):
            balance = self._balances.income_balance(income)
            if not income.archived and balance.is_positive:
                source = SourceRef(SourceKind.INCOME, income_id=income.id)
                options.append(SourceOption(source, income.name, balance))
        options.append(
            SourceOption(
                SourceRef(SourceKind.GENERAL_REMAINDER),
                GENERAL_REMAINDER_NAME,
                self._balances.general_remainder(),
            )
        )
        for accumulation in self._accumulations.list_by_archived(False):
            source = SourceRef(SourceKind.ACCUMULATION, accumulation_id=accumulation.id)
            balance = self._balances.accumulation_balance(accumulation)
            options.append(SourceOption(source, accumulation.name, balance))
        if editing is None:
            return options
        return [
            SourceOption(o.source, o.name, o.available + editing.amount)
            if o.source == editing.source
            else o
            for o in options
        ]

    def repayment_lock_reason(self, repayment: DebtRepayment) -> str | None:
        """Чому суму й джерело погашення змінити чи видалити не можна; ``None`` — можна.

        Минулий місяць — лише перегляд; архівований дохід не відновлюється (Q168);
        погашення з архівованого накопичення фінансово не змінюється (Q189, Q191 —
        опис змінювати можна). Для погашеного боргу додатково діє
        ``repayment_delete_block_reason``: зменшувати й видаляти не можна, а змінити
        джерело за тієї самої суми можна.
        """
        if repayment.month != current_month(self._clock):
            return HISTORICAL_READ_ONLY
        try:
            self._ledger.require_financially_changeable(repayment.source)
        except DomainRuleError as error:
            return error.user_message
        return None

    def repayment_delete_block_reason(self, repayment: DebtRepayment) -> str | None:
        """Чому погашення не можна видалити: загальні блокування або погашений борг,
        який видалення відкрило б повторно."""
        reason = self.repayment_lock_reason(repayment)
        if reason is not None:
            return reason
        debt = self._balances.debt_view(self._require(repayment.debt_id))
        if debt.remaining.is_zero:
            return DebtReopenError(debt.debt.name).user_message
        return None

    def repay(
        self, debt_id: int, amount: Money, source: SourceRef, description: str | None = None
    ) -> RepaymentView:
        """Погашення поточного місяця з одного вручну обраного джерела (ADR 0018, п. 3)."""
        require_normal_operation(self._connection)
        with transaction(self._connection):
            view = self._balances.debt_view(self._require(debt_id))
            repayment = DebtRepayment(
                None, debt_id, current_month(self._clock), description, amount, source
            )
            if amount > view.remaining:
                raise DebtOverpaymentError(view.debt.name, view.remaining, amount)
            self._ledger.require_available(source, amount)
            repayment_id = self._debts.insert_repayment(repayment)
            self._draw(repayment)
        return self.get_repayment(repayment_id)

    def update_repayment(
        self,
        repayment_id: int,
        amount: Money,
        source: SourceRef,
        description: str | None,
    ) -> RepaymentView:
        """Змінює суму, джерело чи опис погашення поточного місяця однією транзакцією.

        Зміна лише опису фінансового стану не змінює (Q191). Зміна суми чи джерела
        перевіряється до запису: залишок джерела (з урахуванням суми, яку погашення
        вже бере з того самого джерела), залишок боргу, Q168, Q189 і правило
        «погашений борг не відкривається повторно».
        """
        require_normal_operation(self._connection)
        with transaction(self._connection):
            old = self._current_month_repayment(repayment_id)
            new = DebtRepayment(old.id, old.debt_id, old.month, description, amount, source)
            if new.amount == old.amount and new.source == old.source:
                self._debts.update_repayment_description(old.id, new.description)
            else:
                self._ledger.require_financially_changeable(old.source)
                view = self._balances.debt_view(self._require(old.debt_id))
                if view.remaining.is_zero and new.amount < old.amount:
                    raise DebtReopenError(view.debt.name)
                if new.amount > view.remaining + old.amount:
                    raise DebtOverpaymentError(
                        view.debt.name, view.remaining + old.amount, new.amount
                    )
                released = old.amount if new.source == old.source else None
                self._ledger.require_available(new.source, new.amount, released=released)
                self._release(old)
                self._debts.replace_repayment(new)
                self._draw(new)
        return self.get_repayment(repayment_id)

    def delete_repayment(self, repayment_id: int) -> None:
        """Видаляє погашення поточного місяця й повертає кошти джерелу (ADR 0018, п. 5)."""
        require_normal_operation(self._connection)
        with transaction(self._connection):
            old = self._current_month_repayment(repayment_id)
            self._ledger.require_financially_changeable(old.source)
            view = self._balances.debt_view(self._require(old.debt_id))
            if view.remaining.is_zero:
                raise DebtReopenError(view.debt.name)
            self._release(old)
            self._debts.delete_repayment(old.id)

    # Допоміжне -------------------------------------------------------------------------

    def _require_repayment(self, repayment_id: int) -> DebtRepayment:
        repayment = self._debts.get_repayment(repayment_id)
        if repayment is None:
            raise DomainRuleError("Погашення не знайдено.")
        return repayment

    def _current_month_repayment(self, repayment_id: int) -> DebtRepayment:
        repayment = self._require_repayment(repayment_id)
        if repayment.month != current_month(self._clock):
            raise DomainRuleError(HISTORICAL_READ_ONLY)
        return repayment

    def _draw(self, repayment: DebtRepayment) -> None:
        """Застосовує записане погашення до джерела: нерозподілений залишок зменшується,
        дохід із нульовим залишком архівується (ADR 0009). Залишки доходу й накопичення
        зменшуються через сам запис погашення (їх рахує ``BalanceService``)."""
        if repayment.source.kind is SourceKind.GENERAL_REMAINDER:
            self._ledger.debit_general_remainder(repayment.amount)
        elif repayment.source.kind is SourceKind.INCOME:
            self._ledger.settle_income(repayment.source.income_id)

    def _release(self, repayment: DebtRepayment) -> None:
        """Повертає списання погашення з нерозподіленого залишку перед заміною чи видаленням."""
        if repayment.source.kind is SourceKind.GENERAL_REMAINDER:
            self._ledger.credit_general_remainder(repayment.amount)

    def _repayment_view(self, repayment: DebtRepayment) -> RepaymentView:
        debt = self._debts.get(repayment.debt_id)
        return RepaymentView(
            repayment,
            debt.name if debt else "Борг",
            self._ledger.source_name(repayment.source),
        )

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
