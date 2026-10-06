"""Первинне налаштування (ADR 0010–0013, ADR 0018, ADR 0022).

Майстер має п'ять кроків: вітання, початковий загальний нерозподілений залишок,
накопичення, борги, перевірка. До першого успішного завершення нормальна фінансова
робота недоступна (Q190), а чернетка не є фінансовим записом (Q176). Завершення
атомарне: або створено весь стартовий стан і налаштування завершено, або нічого.
"""

import sqlite3
from dataclasses import dataclass, field, replace
from enum import IntEnum
from typing import Any

from budget.domain.calendar import Clock, current_month
from budget.domain.models import (
    Accumulation,
    AccumulationStatus,
    Debt,
    DebtOrigin,
    SetupStatus,
    optional_description,
    require_name,
)
from budget.domain.money import Money, require_non_negative, require_positive
from budget.errors import DomainRuleError
from budget.storage.repositories import (
    AccumulationRepository,
    DebtRepository,
    GeneralRemainderRepository,
)
from budget.storage.setup_repository import SetupState, SetupStateRepository
from budget.storage.transaction import transaction


class SetupStep(IntEnum):
    WELCOME = 1
    GENERAL_REMAINDER = 2
    ACCUMULATIONS = 3
    DEBTS = 4
    REVIEW = 5


@dataclass(frozen=True, slots=True)
class InitialAccumulation:
    """Накопичення з початковим балансом (0 або більше) і необов'язковою цільовою сумою."""

    name: str
    description: str | None
    initial_balance: Money
    target: Money | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", require_name(self.name))
        object.__setattr__(self, "description", optional_description(self.description))
        require_non_negative(self.initial_balance)
        if self.target is not None:
            require_non_negative(self.target)


@dataclass(frozen=True, slots=True)
class InitialDebt:
    """Наявний борг: не є отриманням позикових коштів і не поповнює залишок (ADR 0018)."""

    name: str
    description: str | None
    balance: Money

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", require_name(self.name))
        object.__setattr__(self, "description", optional_description(self.description))
        require_positive(self.balance)


@dataclass(frozen=True, slots=True)
class SetupDraft:
    step: SetupStep = SetupStep.WELCOME
    general_remainder: Money = field(default_factory=Money.zero)
    accumulations: tuple[InitialAccumulation, ...] = ()
    debts: tuple[InitialDebt, ...] = ()

    def __post_init__(self) -> None:
        require_non_negative(self.general_remainder)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": int(self.step),
            "general_remainder": self.general_remainder.kopiyky,
            "accumulations": [
                {
                    "name": a.name,
                    "description": a.description,
                    "initial_balance": a.initial_balance.kopiyky,
                    "target": a.target.kopiyky if a.target is not None else None,
                }
                for a in self.accumulations
            ],
            "debts": [
                {"name": d.name, "description": d.description, "balance": d.balance.kopiyky}
                for d in self.debts
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SetupDraft":
        return cls(
            step=SetupStep(int(data.get("step", SetupStep.WELCOME))),
            general_remainder=Money(int(data.get("general_remainder", 0))),
            accumulations=tuple(
                InitialAccumulation(
                    item["name"],
                    item.get("description"),
                    Money(int(item["initial_balance"])),
                    Money(int(item["target"])) if item.get("target") is not None else None,
                )
                for item in data.get("accumulations", [])
            ),
            debts=tuple(
                InitialDebt(item["name"], item.get("description"), Money(int(item["balance"])))
                for item in data.get("debts", [])
            ),
        )


class InitialSetupService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self._connection = connection
        self._clock = clock
        self._repository = SetupStateRepository(connection)

    def state(self) -> SetupState:
        return self._repository.get()

    def is_completed(self) -> bool:
        return self.state().status is SetupStatus.COMPLETED

    def draft(self) -> SetupDraft:
        """Чернетка для продовження майстра; без чернетки — новий майстер."""
        self._ensure_not_completed()
        stored = self.state().draft
        return SetupDraft.from_dict(stored) if stored is not None else SetupDraft()

    def save_draft(self, draft: SetupDraft | dict[str, Any]) -> None:
        """Зберігає незавершений стан майстра без строку дії (Q176, Q178)."""
        data = draft.to_dict() if isinstance(draft, SetupDraft) else draft
        self._ensure_not_completed()
        with transaction(self._connection):
            self._repository.save_draft(data)

    def reset(self) -> None:
        """Повністю скидає незавершений майстер (Q179); фінансових записів не створює."""
        self._ensure_not_completed()
        with transaction(self._connection):
            self._repository.clear_draft()

    def complete(self, draft: SetupDraft) -> None:
        """Атомарно створює стартовий стан і завершує налаштування (ADR 0010, Q172).

        Початковий залишок формує загальний нерозподілений залишок без доходу;
        накопичення отримують статус «Активне» незалежно від цільової суми (Q165, Q173);
        початкові борги не є отриманням позикових коштів і не змінюють залишок.
        """
        if self._clock is None:
            raise RuntimeError("Для завершення налаштування потрібен годинник")
        month = current_month(self._clock)
        accumulations = AccumulationRepository(self._connection)
        debts = DebtRepository(self._connection)
        remainder = GeneralRemainderRepository(self._connection)
        with transaction(self._connection):
            # Повторна перевірка всередині транзакції: дублікатів стартового стану немає.
            if self._repository.get().status is SetupStatus.COMPLETED:
                raise DomainRuleError("Первинне налаштування вже завершено.")
            for item in draft.accumulations:
                accumulations.insert(
                    Accumulation(
                        None,
                        item.name,
                        item.description,
                        item.target,
                        AccumulationStatus.ACTIVE,
                        False,
                        item.initial_balance,
                    )
                )
            for item in draft.debts:
                debts.insert(
                    Debt(None, DebtOrigin.INITIAL, None, item.name, item.description, item.balance)
                )
            if draft.general_remainder.is_positive:
                remainder.set(remainder.get() + draft.general_remainder)
            self._repository.mark_completed(month)

    def _ensure_not_completed(self) -> None:
        if self.is_completed():
            raise DomainRuleError("Первинне налаштування вже завершено.")


def require_normal_operation(connection: sqlite3.Connection) -> None:
    """Блокує фінансові операції до завершення первинного налаштування (Q190)."""
    if not InitialSetupService(connection).is_completed():
        raise DomainRuleError("Спершу завершіть первинне налаштування.")


def with_step(draft: SetupDraft, step: SetupStep) -> SetupDraft:
    return replace(draft, step=step)
