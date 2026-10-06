"""Єдине місце форматування сум для показу (design-system.md, 3.3).

Розділювачі — за ``QLocale`` ``uk_UA``; позначка валюти не показується (ADR 0008);
копійки — лише якщо вони не нульові; округлення не виконується.
"""

from PySide6.QtCore import QLocale

from budget.domain.money import Money

_LOCALE = QLocale(QLocale.Language.Ukrainian, QLocale.Country.Ukraine)


def format_money(amount: Money) -> str:
    negative, hryvni, kopiyky = amount.split()
    text = _LOCALE.toString(hryvni)
    if kopiyky:
        text = f"{text}{_LOCALE.decimalPoint()}{kopiyky:02d}"
    return f"−{text}" if negative else text
