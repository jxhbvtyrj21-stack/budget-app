"""Поповнення накопичень (ADR 0007, ADR 0010–0014, ADR 0020, ADR 0022).

Поповнення — окрема фінансова операція поточного місяця: кошти з одного чи кількох
джерел, обраних і розподілених лише користувачем, переходять у неархівоване
накопичення. Джерела — доходи поточного місяця й загальний нерозподілений залишок;
накопичення джерелом не буває. Поповнення не є звичайною витратою.

Достатність коштів перевіряє ``SourceLedger`` для кожного джерела загалом — після
підсумовування всіх частин із цього джерела. Залишки рахує лише ``BalanceService``.
Кожна дія — одна транзакція; минулі місяці — лише перегляд.
"""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from budget.domain.calendar import CalendarMonth, Clock, current_month
from budget.domain.models import (
    Accumulation,
    Replenishment,
    ReplenishmentPart,
    SourceKind,
    SourceRef,
)
from budget.domain.money import Money
from budget.errors import DomainRuleError
from budget.services.balances import BalanceService
from budget.services.expense import HISTORICAL_READ_ONLY, SourceOption
from budget.services.setup import require_normal_operation
from budget.services.sources import GENERAL_REMAINDER_NAME, SourceLedger
from budget.storage.repositories import (
    AccumulationRepository,
    IncomeRepository,
    ReplenishmentRepository,
)
from budget.storage.transaction import transaction


@dataclass(frozen=True, slots=True)
class ReplenishmentView:
    replenishment: Replenishment
    recipient_name: str
    source_names: tuple[str, ...]  # у порядку частин

    @property
    def total(self) -> Money:
        return self.replenishment.total


@dataclass(frozen=True, slots=True)
class RecipientOption:
    accumulation_id: int
    name: str
    balance: Money


class ReplenishmentService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock) -> None:
        self._connection = connection
        self._clock = clock
        self._replenishments = ReplenishmentRepository(connection)
        self._incomes = IncomeRepository(connection)
        self._accumulations = AccumulationRepository(connection)
        self._balances = BalanceService(connection)
        self._ledger = SourceLedger(connection, clock)

    # Читання ---------------------------------------------------------------------------

    def get(self, replenishment_id: int) -> ReplenishmentView:
        replenishment = self._replenishments.get(replenishment_id)
        if replenishment is None:
            raise DomainRuleError("Поповнення не знайдено.")
        return self._view(replenishment)

    def list_for_month(self, month: CalendarMonth) -> list[ReplenishmentView]:
        return [self._view(r) for r in self._replenishments.list_for_month(month)]

    def list_for_accumulation(self, accumulation_id: int) -> list[ReplenishmentView]:
        return [self._view(r) for r in self._replenishments.list_for_accumulation(accumulation_id)]

    def source_options(self, editing: Replenishment | None = None) -> list[SourceOption]:
        """Джерела поповнення зараз: доходи поточного місяця й нерозподілений залишок.

        Під час зміни поповнення суми, які воно вже бере з джерела, рахуються доступними
        в тому самому джерелі (старий ефект знімається разом із застосуванням нового).
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
        if editing is None:
            return options
        released = editing.totals_by_source()
        return [
            SourceOption(o.source, o.name, o.available + released[o.source])
            if o.source in released
            else o
            for o in options
        ]

    def recipient_options(self) -> list[RecipientOption]:
        """Отримувачі: неархівовані накопичення будь-якого статусу (ADR 0007, п. 12; Q187)."""
        return [
            RecipientOption(a.id, a.name, self._balances.accumulation_balance(a))
            for a in self._accumulations.list_by_archived(False)
        ]

    def financial_lock_reason(self, replenishment: Replenishment) -> str | None:
        """Чому поповнення не можна фінансово змінювати загалом; ``None`` — можна.

        Минулий місяць — лише перегляд; поповнення архівованого накопичення — лише
        назва й опис (Q189, Q191). Окремі частини з архівованого доходу описує
        ``locked_sources``.
        """
        if replenishment.month != current_month(self._clock):
            return HISTORICAL_READ_ONLY
        try:
            self._require_recipient_changeable(replenishment.accumulation_id)
        except DomainRuleError as error:
            return error.user_message
        return None

    def locked_sources(self, replenishment: Replenishment) -> set[SourceRef]:
        """Джерела-доходи, які вже архівовано: їхні частини не можна зменшити чи прибрати
        (Q174), бо це повернуло б кошти архівованому доходу."""
        locked = set()
        for source in replenishment.totals_by_source():
            if source.kind is SourceKind.INCOME:
                income = self._incomes.get(source.income_id)
                if income is not None and income.archived:
                    locked.add(source)
        return locked

    # Зміни -----------------------------------------------------------------------------

    def create(
        self,
        name: str,
        description: str | None,
        accumulation_id: int,
        parts: Sequence[ReplenishmentPart],
    ) -> ReplenishmentView:
        """Створює поповнення поточного місяця однією транзакцією (ADR 0007, Q152, Q156)."""
        require_normal_operation(self._connection)
        replenishment = Replenishment(
            None, current_month(self._clock), name, description, accumulation_id, tuple(parts)
        )
        with transaction(self._connection):
            self._require_recipient(accumulation_id)
            self._require_sources(replenishment, released={})
            replenishment_id = self._replenishments.insert(replenishment)
            self._draw(replenishment)
        return self.get(replenishment_id)

    # Допоміжне -------------------------------------------------------------------------

    def _require_recipient(self, accumulation_id: int) -> Accumulation:
        """Отримувач нового фінансового ефекту — наявне неархівоване накопичення (Q187)."""
        accumulation = self._accumulations.get(accumulation_id)
        if accumulation is None:
            raise DomainRuleError("Накопичення не знайдено.")
        if accumulation.archived:
            raise DomainRuleError(
                f"Накопичення «{accumulation.name}» в архіві; воно не може отримати поповнення."
            )
        return accumulation

    def _require_recipient_changeable(self, accumulation_id: int) -> None:
        """Q189: операції архівованого накопичення фінансово не змінюються."""
        self._ledger.require_financially_changeable(
            SourceRef(SourceKind.ACCUMULATION, accumulation_id=accumulation_id)
        )

    def _require_sources(
        self, replenishment: Replenishment, released: dict[SourceRef, Money]
    ) -> None:
        """Перевіряє всю конфігурацію до запису: кожне джерело — за сумою його частин.

        ``released`` — суми, які та сама операція вже бере з джерел (під час зміни).
        Джерело, з якого не береться більше, ніж уже взято, нових коштів не потребує.
        """
        for source, total in replenishment.totals_by_source().items():
            already = released.get(source, Money.zero())
            if total > already:
                self._ledger.require_available(source, total, released=already)

    def _draw(self, replenishment: Replenishment) -> None:
        """Застосовує записане поповнення до джерел: нерозподілений залишок зменшується,
        дохід із нульовим залишком архівується (ADR 0009). Накопичення-отримувач
        отримує кошти через сам запис частин (залишок рахує ``BalanceService``)."""
        for source, total in replenishment.totals_by_source().items():
            if source.kind is SourceKind.GENERAL_REMAINDER:
                self._ledger.debit_general_remainder(total)
            else:
                self._ledger.settle_income(source.income_id)

    def _view(self, replenishment: Replenishment) -> ReplenishmentView:
        accumulation = self._accumulations.get(replenishment.accumulation_id)
        return ReplenishmentView(
            replenishment,
            accumulation.name if accumulation else "Накопичення",
            tuple(self._ledger.source_name(p.source) for p in replenishment.parts),
        )
