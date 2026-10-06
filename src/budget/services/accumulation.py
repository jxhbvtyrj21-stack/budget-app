"""Накопичення як сутність: створення й ручний життєвий цикл (ADR 0007, ADR 0011–0013).

Залишок накопичення не зберігається окремо: його рахує лише ``BalanceService``
(початковий баланс + поповнення − витрати − погашення). Статус змінює тільки
користувач; автоматичних переходів за ціллю чи нульовим залишком немає. Фізичного
видалення накопичення немає (Q185).
"""

import sqlite3
from dataclasses import dataclass

from budget.domain.calendar import Clock
from budget.domain.models import (
    ACCUMULATION_TRANSITIONS,
    Accumulation,
    AccumulationStatus,
    TargetProgress,
    target_progress,
)
from budget.domain.money import Money
from budget.errors import DomainRuleError
from budget.services.balances import BalanceService
from budget.services.setup import require_normal_operation
from budget.storage.repositories import AccumulationRepository
from budget.storage.transaction import transaction

STATUS_LOCKED_IN_ARCHIVE = (
    "Накопичення в архіві: статус не змінюється. Щоб змінити статус, розархівуйте накопичення."
)


class CloseBlockedError(DomainRuleError):
    """Закрити накопичення можна лише при залишку 0 (ADR 0007, п. 11).

    Суму форматує інтерфейс (``budget.ui.messages``); тут — структуровані дані.
    """

    def __init__(self, name: str, balance: Money) -> None:
        self.name = name
        self.balance = balance
        super().__init__("Закрити накопичення можна лише при залишку 0.")


@dataclass(frozen=True, slots=True)
class AccumulationView:
    accumulation: Accumulation
    balance: Money

    @property
    def progress(self) -> TargetProgress | None:
        return target_progress(self.balance, self.accumulation.target)


class AccumulationService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock) -> None:
        self._connection = connection
        self._clock = clock
        self._accumulations = AccumulationRepository(connection)
        self._balances = BalanceService(connection)

    # Читання ---------------------------------------------------------------------------

    def get(self, accumulation_id: int) -> AccumulationView:
        return self._view(self._require(accumulation_id))

    def list_working(self) -> list[AccumulationView]:
        """Робочий перелік: неархівовані накопичення будь-якого статусу."""
        return [self._view(a) for a in self._accumulations.list_by_archived(False)]

    def list_archived(self) -> list[AccumulationView]:
        return [self._view(a) for a in self._accumulations.list_by_archived(True)]

    def status_transitions(self, view: AccumulationView) -> tuple[AccumulationStatus, ...]:
        """Переходи, дозволені графом ADR 0007; в архіві статус не змінюється (Q188).

        «Закрите» лишається в переліку й за ненульового залишку — інтерфейс показує
        його вимкненим із поясненням (``close_blocker``).
        """
        if view.accumulation.archived:
            return ()
        return ACCUMULATION_TRANSITIONS[view.accumulation.status]

    def close_blocker(self, view: AccumulationView) -> CloseBlockedError | None:
        if view.balance.is_zero:
            return None
        return CloseBlockedError(view.accumulation.name, view.balance)

    # Зміни -----------------------------------------------------------------------------

    def create(self, name: str, description: str | None, target: Money | None) -> AccumulationView:
        """Нове накопичення поза майстром: без початкового балансу (Q172), «Активне» (Q173)."""
        require_normal_operation(self._connection)
        accumulation = Accumulation(
            None, name, description, target, AccumulationStatus.ACTIVE, False, Money.zero()
        )
        with transaction(self._connection):
            accumulation_id = self._accumulations.insert(accumulation)
        return self.get(accumulation_id)

    def change_status(self, accumulation_id: int, status: AccumulationStatus) -> AccumulationView:
        """Ручна зміна статусу за графом ADR 0007, п. 14; без фінансового ефекту."""
        require_normal_operation(self._connection)
        with transaction(self._connection):
            view = self._view(self._require(accumulation_id))
            current = view.accumulation.status
            if view.accumulation.archived:
                raise DomainRuleError(STATUS_LOCKED_IN_ARCHIVE)
            if status not in ACCUMULATION_TRANSITIONS[current]:
                raise DomainRuleError(
                    f"Перехід «{status_label(current)}» → «{status_label(status)}» недоступний."
                )
            if status is AccumulationStatus.CLOSED:
                blocker = self.close_blocker(view)
                if blocker is not None:
                    raise blocker
            self._accumulations.set_status(accumulation_id, status)
        return self.get(accumulation_id)

    # Допоміжне -------------------------------------------------------------------------

    def _require(self, accumulation_id: int) -> Accumulation:
        accumulation = self._accumulations.get(accumulation_id)
        if accumulation is None:
            raise DomainRuleError("Накопичення не знайдено.")
        return accumulation

    def _view(self, accumulation: Accumulation) -> AccumulationView:
        return AccumulationView(accumulation, self._balances.accumulation_balance(accumulation))


STATUS_LABELS = {
    AccumulationStatus.ACTIVE: "Активне",
    AccumulationStatus.REACHED: "Досягнуте",
    AccumulationStatus.CLOSED: "Закрите",
}


def status_label(status: AccumulationStatus) -> str:
    return STATUS_LABELS[status]
