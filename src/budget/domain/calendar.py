"""Фінансовий календар: часова зона Europe/Kyiv і календарний місяць (ADR 0009).

Це єдине місце, де визначено фінансову часову зону й читається системний час.
Бюджетний період — значення ``CalendarMonth``, а не сутність чи запис у базі.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

FINANCIAL_TIMEZONE = ZoneInfo("Europe/Kyiv")

_MONTH_PATTERN = re.compile(r"^(\d{4})-(\d{2})$")


@dataclass(frozen=True, slots=True, order=True)
class CalendarMonth:
    year: int
    month: int

    def __post_init__(self) -> None:
        if not 1 <= self.month <= 12 or not 1 <= self.year <= 9999:
            raise ValueError(f"Некоректний місяць: {self.year}-{self.month}")

    @classmethod
    def parse(cls, text: str) -> "CalendarMonth":
        match = _MONTH_PATTERN.fullmatch(text)
        if match is None:
            raise ValueError(f"Некоректний місяць: {text!r}")
        return cls(int(match.group(1)), int(match.group(2)))

    @classmethod
    def containing(cls, moment: datetime) -> "CalendarMonth":
        """Календарний місяць моменту часу за Europe/Kyiv."""
        if moment.tzinfo is None:
            raise ValueError("Потрібен момент часу з часовою зоною")
        local = moment.astimezone(FINANCIAL_TIMEZONE)
        return cls(local.year, local.month)

    def next(self) -> "CalendarMonth":
        return CalendarMonth(self.year + self.month // 12, self.month % 12 + 1)

    def previous(self) -> "CalendarMonth":
        if self.month == 1:
            return CalendarMonth(self.year - 1, 12)
        return CalendarMonth(self.year, self.month - 1)

    def __str__(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"


class Clock(Protocol):
    """Джерело поточного часу для сервісного шару."""

    def now(self) -> datetime: ...


class SystemClock:
    """Поточний час комп'ютера в Europe/Kyiv."""

    def now(self) -> datetime:
        return datetime.now(FINANCIAL_TIMEZONE)


class FixedClock:
    """Детермінований час для тестів і перевірок."""

    def __init__(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("Потрібен момент часу з часовою зоною")
        self._moment = moment

    def now(self) -> datetime:
        return self._moment.astimezone(FINANCIAL_TIMEZONE)

    def set(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("Потрібен момент часу з часовою зоною")
        self._moment = moment


def current_month(clock: Clock) -> CalendarMonth:
    return CalendarMonth.containing(clock.now())
