"""Доменні сутності першого релізу та їхні інваріанти.

Тут лише структура й перевірки окремої сутності. Правила, що залежать від інших
записів (достатність залишку, поточний місяць, архівування доходу), виконує
сервісний шар. Відхилених сутностей (рахунки, валюти, бюджетні періоди, стани
місяців, фінансові цілі, перекази) у моделі немає.
"""

from dataclasses import dataclass, field
from enum import StrEnum

from budget.domain.calendar import CalendarMonth
from budget.domain.money import Money, require_non_negative, require_positive
from budget.errors import DomainRuleError, ValidationError


def require_name(name: str) -> str:
    """Назва обов'язкова (ADR 0012, Q183; ADR 0018; ADR 0022)."""
    cleaned = name.strip()
    if not cleaned:
        raise ValidationError("Вкажіть назву.")
    return cleaned


def optional_description(description: str | None) -> str | None:
    if description is None:
        return None
    cleaned = description.strip()
    return cleaned or None


class SourceKind(StrEnum):
    """Джерело звичайної витрати, частини поповнення чи погашення (ADR 0003)."""

    INCOME = "income"
    GENERAL_REMAINDER = "general_remainder"
    ACCUMULATION = "accumulation"


@dataclass(frozen=True, slots=True)
class SourceRef:
    kind: SourceKind
    income_id: int | None = None
    accumulation_id: int | None = None

    def __post_init__(self) -> None:
        expected = {
            SourceKind.INCOME: (self.income_id is not None, self.accumulation_id is None),
            SourceKind.GENERAL_REMAINDER: (self.income_id is None, self.accumulation_id is None),
            SourceKind.ACCUMULATION: (self.income_id is None, self.accumulation_id is not None),
        }[self.kind]
        if not all(expected):
            raise ValueError(f"Некоректне посилання на джерело: {self}")


class IncomeStatus(StrEnum):
    """Похідний статус доходу за залишком (ADR 0009)."""

    ACTIVE = "active"
    COMPLETED = "completed"


def income_status(balance: Money) -> IncomeStatus:
    return IncomeStatus.ACTIVE if balance.is_positive else IncomeStatus.COMPLETED


@dataclass(frozen=True, slots=True)
class Income:
    """Дохід: незмінний після створення; залишок похідний від пов'язаних операцій."""

    id: int | None
    month: CalendarMonth
    name: str
    description: str | None
    amount: Money
    archived: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", require_name(self.name))
        object.__setattr__(self, "description", optional_description(self.description))
        require_positive(self.amount)


@dataclass(frozen=True, slots=True)
class Expense:
    """Звичайна витрата: рівно одне джерело (ADR 0003), зокрема накопичення (Q170)."""

    id: int | None
    month: CalendarMonth
    name: str
    description: str | None
    amount: Money
    source: SourceRef

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", require_name(self.name))
        object.__setattr__(self, "description", optional_description(self.description))
        require_positive(self.amount)


class AccumulationStatus(StrEnum):
    """Статус життєвого циклу накопичення; змінюється лише вручну (ADR 0007)."""

    ACTIVE = "active"
    REACHED = "reached"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class Accumulation:
    """Накопичення: залишок похідний (початковий баланс + поповнення − витрати − погашення).

    Архівність — окремий вимір від статусу; архівованим може бути лише закрите
    накопичення (ADR 0012, ADR 0013).
    """

    id: int | None
    name: str
    description: str | None
    target: Money | None
    status: AccumulationStatus
    archived: bool
    initial_balance: Money

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", require_name(self.name))
        object.__setattr__(self, "description", optional_description(self.description))
        if self.target is not None:
            require_non_negative(self.target)
        require_non_negative(self.initial_balance)
        if self.archived and self.status is not AccumulationStatus.CLOSED:
            raise DomainRuleError("Архівувати можна лише закрите накопичення.")


@dataclass(frozen=True, slots=True)
class ReplenishmentPart:
    """Частина поповнення: джерело — дохід або загальний нерозподілений залишок (ADR 0007)."""

    source: SourceRef
    amount: Money

    def __post_init__(self) -> None:
        if self.source.kind is SourceKind.ACCUMULATION:
            raise DomainRuleError("Накопичення не може бути джерелом поповнення.")
        require_positive(self.amount)


@dataclass(frozen=True, slots=True)
class Replenishment:
    """Поповнення накопичення — одна операція з одним або кількома джерелами."""

    id: int | None
    month: CalendarMonth
    name: str
    description: str | None
    accumulation_id: int
    parts: tuple[ReplenishmentPart, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", require_name(self.name))
        object.__setattr__(self, "description", optional_description(self.description))
        if not self.parts:
            raise ValidationError("Додайте хоча б одне джерело поповнення.")

    @property
    def total(self) -> Money:
        total = Money.zero()
        for part in self.parts:
            total = total + part.amount
        return total


class DebtOrigin(StrEnum):
    """Походження боргу: початковий (майстер) чи отримання позикових коштів (ADR 0018)."""

    INITIAL = "initial"
    LOAN_RECEIPT = "loan_receipt"


class DebtStatus(StrEnum):
    """Похідний статус боргу за залишком (ADR 0018)."""

    ACTIVE = "active"
    PAID = "paid"


def debt_status(remaining: Money) -> DebtStatus:
    return DebtStatus.ACTIVE if remaining.is_positive else DebtStatus.PAID


@dataclass(frozen=True, slots=True)
class Debt:
    """Борг. Початковий борг не має місяця й не є отриманням позикових коштів."""

    id: int | None
    origin: DebtOrigin
    month: CalendarMonth | None
    name: str
    description: str | None
    amount: Money

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", require_name(self.name))
        object.__setattr__(self, "description", optional_description(self.description))
        require_positive(self.amount)
        if (self.origin is DebtOrigin.INITIAL) != (self.month is None):
            raise ValueError("Місяць має лише борг, створений отриманням позикових коштів")


@dataclass(frozen=True, slots=True)
class DebtRepayment:
    """Погашення: одне вручну обране джерело; не є звичайною витратою (ADR 0018)."""

    id: int | None
    debt_id: int
    month: CalendarMonth
    description: str | None
    amount: Money
    source: SourceRef

    def __post_init__(self) -> None:
        object.__setattr__(self, "description", optional_description(self.description))
        require_positive(self.amount)


@dataclass(frozen=True, slots=True)
class BaseMinimum:
    """Базовий мінімум місяця: орієнтир, не план і не ліміт (ADR 0002, ADR 0019)."""

    month: CalendarMonth
    amount: Money

    def __post_init__(self) -> None:
        require_non_negative(self.amount)


class SetupStatus(StrEnum):
    """Стан первинного налаштування (ADR 0010–0013)."""

    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
