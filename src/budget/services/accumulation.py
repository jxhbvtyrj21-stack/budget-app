"""Накопичення як сутність: створення, життєвий цикл, архів і метадані.

ADR 0007, ADR 0011–0014, ADR 0022.

Залишок накопичення не зберігається окремо: його рахує лише ``BalanceService``
(початковий баланс + поповнення − витрати − погашення). Статус змінює тільки
користувач; автоматичних переходів за ціллю чи нульовим залишком немає. Фізичного
видалення накопичення немає (Q185).
"""

import sqlite3
from dataclasses import dataclass, replace

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
from budget.services.expense import ExpenseView
from budget.services.setup import require_normal_operation
from budget.storage.repositories import AccumulationRepository, ExpenseRepository
from budget.storage.transaction import transaction

STATUS_LOCKED_IN_ARCHIVE = (
    "Накопичення в архіві: статус не змінюється. Щоб змінити статус, розархівуйте накопичення."
)


ARCHIVE_ONLY_CLOSED = "Архівувати можна лише закрите накопичення."
ARCHIVED_NOTICE = (
    "Накопичення в архіві. Нові поповнення й витрати недоступні, статус не змінюється, "
    "суми операцій поточного місяця не редагуються. Щоб змінити їх, розархівуйте накопичення."
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
        self._expenses = ExpenseRepository(connection)

    # Читання ---------------------------------------------------------------------------

    def get(self, accumulation_id: int) -> AccumulationView:
        return self._view(self._require(accumulation_id))

    def list_working(self) -> list[AccumulationView]:
        """Робочий перелік: неархівовані накопичення будь-якого статусу."""
        return [self._view(a) for a in self._accumulations.list_by_archived(False)]

    def list_archived(self) -> list[AccumulationView]:
        return [self._view(a) for a in self._accumulations.list_by_archived(True)]

    def expense_history(self, accumulation_id: int) -> list[ExpenseView]:
        """Звичайні витрати з цього накопичення, від нових до старих (Q170).

        Початковий баланс — не операція й не має місяця; інтерфейс показує його
        окремим рядком із ``accumulation.initial_balance``.
        """
        name = self._require(accumulation_id).name
        return [ExpenseView(e, name) for e in self._expenses.list_for_accumulation(accumulation_id)]

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

    def update_metadata(
        self,
        accumulation_id: int,
        name: str,
        description: str | None,
        target: Money | None,
    ) -> AccumulationView:
        """Назва, опис і цільова сума — метадані (Q184, ADR 0022).

        Дозволено в будь-якому статусі й в архіві. Не змінює залишку, статусу,
        архівності й не створює фінансових записів; історія змін не ведеться.
        """
        require_normal_operation(self._connection)
        with transaction(self._connection):
            current = self._require(accumulation_id)
            # Перевірка через доменну сутність: порожня назва, від'ємна ціль.
            updated = replace(current, name=name, description=description, target=target)
            self._accumulations.update_metadata(
                accumulation_id, updated.name, updated.description, updated.target
            )
        return self.get(accumulation_id)

    def archive(self, accumulation_id: int) -> AccumulationView:
        """«Закрите» → «Закрите + архівоване» (Q186); залишок не змінюється (Q187)."""
        require_normal_operation(self._connection)
        with transaction(self._connection):
            accumulation = self._require(accumulation_id)
            if accumulation.archived:
                raise DomainRuleError("Накопичення вже в архіві.")
            if accumulation.status is not AccumulationStatus.CLOSED:
                raise DomainRuleError(ARCHIVE_ONLY_CLOSED)
            self._accumulations.set_archived(accumulation_id, True)
        return self.get(accumulation_id)

    def unarchive(self, accumulation_id: int) -> AccumulationView:
        """«Закрите + архівоване» → «Закрите» (Q181, Q186): статус лишається «Закрите»."""
        require_normal_operation(self._connection)
        with transaction(self._connection):
            accumulation = self._require(accumulation_id)
            if not accumulation.archived:
                raise DomainRuleError("Накопичення не в архіві.")
            self._accumulations.set_archived(accumulation_id, False)
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
