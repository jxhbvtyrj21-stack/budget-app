"""Єдиний грошовий примітив першого релізу (A-15, A-24, ADR 0008).

Усі суми — гривні в цілих копійках. Валюти як поля чи сутності немає, ``float`` не
використовується. Розбір введення — лише тут; показ сум — лише в
``budget.ui.formatting``.
"""

import re
from dataclasses import dataclass

from budget.errors import ValidationError

KOPIYKY_PER_HRYVNIA = 100
# Межа значно нижча за 64-бітне ціле SQLite, щоб суми й різниці не переповнювалися.
MAX_KOPIYKY = 10**15

_AMOUNT_PATTERN = re.compile(r"^(\d+)(?:[.,](\d{1,2}))?$")
_GROUP_SEPARATORS = (" ", " ", " ")


@dataclass(frozen=True, slots=True, order=True)
class Money:
    """Сума в копійках. Може бути від'ємною лише як різниця для порівняння."""

    kopiyky: int

    def __post_init__(self) -> None:
        if type(self.kopiyky) is not int:
            raise TypeError("Money приймає лише int (копійки)")
        if abs(self.kopiyky) > MAX_KOPIYKY:
            raise ValidationError("Сума завелика.")

    @classmethod
    def zero(cls) -> "Money":
        return cls(0)

    @classmethod
    def from_hryvni(cls, hryvni: int, kopiyky: int = 0) -> "Money":
        if type(hryvni) is not int or type(kopiyky) is not int:
            raise TypeError("Гривні й копійки мають бути int")
        if hryvni < 0 or not 0 <= kopiyky < KOPIYKY_PER_HRYVNIA:
            raise ValueError("Некоректні гривні або копійки")
        return cls(hryvni * KOPIYKY_PER_HRYVNIA + kopiyky)

    def __add__(self, other: "Money") -> "Money":
        if not isinstance(other, Money):
            return NotImplemented
        return Money(self.kopiyky + other.kopiyky)

    def __sub__(self, other: "Money") -> "Money":
        if not isinstance(other, Money):
            return NotImplemented
        return Money(self.kopiyky - other.kopiyky)

    @property
    def is_zero(self) -> bool:
        return self.kopiyky == 0

    @property
    def is_positive(self) -> bool:
        return self.kopiyky > 0

    @property
    def is_negative(self) -> bool:
        return self.kopiyky < 0

    def split(self) -> tuple[bool, int, int]:
        """Повертає (від'ємна, гривні, копійки) для форматування."""
        whole, fraction = divmod(abs(self.kopiyky), KOPIYKY_PER_HRYVNIA)
        return self.kopiyky < 0, whole, fraction


def parse_money(text: str) -> Money:
    """Розбирає введену користувачем суму: ``5000``, ``5 000``, ``5 000,5``, ``5000.50``.

    Від'ємні суми й понад два знаки після коми відхиляються: округлення під час
    введення не виконується.
    """
    cleaned = text.strip()
    for separator in _GROUP_SEPARATORS:
        cleaned = cleaned.replace(separator, "")
    match = _AMOUNT_PATTERN.fullmatch(cleaned)
    if match is None:
        raise ValidationError("Введіть суму цифрами, наприклад 5 000 або 5 000,50.")
    hryvni = int(match.group(1))
    fraction = match.group(2) or ""
    kopiyky = int(fraction.ljust(2, "0")) if fraction else 0
    if hryvni * KOPIYKY_PER_HRYVNIA + kopiyky > MAX_KOPIYKY:
        raise ValidationError("Сума завелика.")
    return Money.from_hryvni(hryvni, kopiyky)


def require_positive(amount: Money) -> Money:
    """Сума операції має бути більшою за нуль."""
    if not amount.is_positive:
        raise ValidationError("Сума має бути більшою за нуль.")
    return amount


def require_non_negative(amount: Money) -> Money:
    if amount.is_negative:
        raise ValidationError("Сума не може бути від'ємною.")
    return amount
