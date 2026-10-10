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


class RecipientBalanceError(DomainRuleError):
    """Зміна зробила б залишок накопичення-отримувача від'ємним.

    Частину коштів поповнення вже витрачено з накопичення; від'ємних залишків джерел
    не буває (ADR 0003, ADR 0011 Q171). Суми форматує інтерфейс.
    """

    def __init__(self, name: str, balance: Money, reduction: Money) -> None:
        self.name = name
        self.balance = balance
        self.reduction = reduction
        super().__init__(
            f"Зміну не можна зберегти: залишок накопичення «{name}» став би від'ємним."
        )


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

    def update(
        self,
        replenishment_id: int,
        name: str,
        description: str | None,
        accumulation_id: int,
        parts: Sequence[ReplenishmentPart],
    ) -> ReplenishmentView:
        """Змінює поповнення поточного місяця (ADR 0011 Q171, ADR 0012 Q175, Q177).

        Зміна лише назви чи опису — метадані: залишки не змінюються, дозволено й для
        поповнення архівованого накопичення (Q191). Зміна отримувача, набору джерел чи
        сум перевіряється цілком до запису; старий ефект знімається й новий
        застосовується однією транзакцією. Не зберігається зміна, що повернула б кошти
        архівованому доходу (Q174), змінює операцію архівованого накопичення (Q189) чи
        робить залишок накопичення від'ємним.
        """
        require_normal_operation(self._connection)
        with transaction(self._connection):
            old = self._current_month_replenishment(replenishment_id)
            new = Replenishment(old.id, old.month, name, description, accumulation_id, tuple(parts))
            if new.accumulation_id == old.accumulation_id and new.parts == old.parts:
                self._replenishments.update_metadata(old.id, new.name, new.description)
            else:
                self._require_recipient_changeable(old.accumulation_id)
                if new.accumulation_id != old.accumulation_id:
                    self._require_recipient(new.accumulation_id)
                old_totals = old.totals_by_source()
                self._require_no_income_revival(old_totals, new.totals_by_source())
                self._require_sources(new, released=old_totals)
                self._require_recipient_not_negative(old, new)
                self._release(old)
                self._replenishments.replace(new)
                self._draw(new)
        return self.get(replenishment_id)

    def delete(self, replenishment_id: int) -> None:
        """Видаляє поповнення поточного місяця й повертає кошти джерелам (Q171).

        Заблоковано, якщо це повернуло б кошти архівованому доходу (Q174), якщо
        отримувач в архіві (Q189) або якщо залишок отримувача став би від'ємним.
        """
        require_normal_operation(self._connection)
        with transaction(self._connection):
            old = self._current_month_replenishment(replenishment_id)
            self._require_recipient_changeable(old.accumulation_id)
            self._require_no_income_revival(old.totals_by_source(), {})
            self._require_recipient_not_negative(old, None)
            self._release(old)
            self._replenishments.delete(old.id)

    # Допоміжне -------------------------------------------------------------------------

    def _current_month_replenishment(self, replenishment_id: int) -> Replenishment:
        replenishment = self._replenishments.get(replenishment_id)
        if replenishment is None:
            raise DomainRuleError("Поповнення не знайдено.")
        if replenishment.month != current_month(self._clock):
            raise DomainRuleError(HISTORICAL_READ_ONLY)
        return replenishment

    def _require_no_income_revival(
        self, old_totals: dict[SourceRef, Money], new_totals: dict[SourceRef, Money]
    ) -> None:
        """Q174: частину з архівованого доходу не можна зменшити чи прибрати."""
        for source, old_total in old_totals.items():
            if source.kind is SourceKind.INCOME:
                if new_totals.get(source, Money.zero()) < old_total:
                    self._ledger.require_income_not_revived(source.income_id)

    def _require_recipient_not_negative(
        self, old: Replenishment, new: Replenishment | None
    ) -> None:
        """Після зміни чи видалення залишок старого отримувача не стає від'ємним."""
        accumulation = self._accumulations.get(old.accumulation_id)
        balance = self._balances.accumulation_balance(accumulation)
        kept = new.total if new is not None and new.accumulation_id == old.accumulation_id else None
        reduction = old.total - (kept or Money.zero())
        if (balance - reduction).is_negative:
            raise RecipientBalanceError(accumulation.name, balance, reduction)

    def _release(self, replenishment: Replenishment) -> None:
        """Знімає фінансовий ефект поповнення з нерозподіленого залишку перед заміною чи
        видаленням; доходи й накопичення отримують кошти назад через зняття частин."""
        released = replenishment.totals_by_source().get(SourceRef(SourceKind.GENERAL_REMAINDER))
        if released is not None:
            self._ledger.credit_general_remainder(released)

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
