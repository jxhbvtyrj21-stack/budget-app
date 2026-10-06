"""Єдине місце форматування сум і місяців для показу (design-system.md, 3.3).

Розділювачі — за ``QLocale`` ``uk_UA``; позначка валюти не показується (ADR 0008);
копійки — лише якщо вони не нульові; округлення не виконується.
"""

from PySide6.QtCore import QLocale

from budget.domain.calendar import CalendarMonth
from budget.domain.money import Money

_LOCALE = QLocale(QLocale.Language.Ukrainian, QLocale.Country.Ukraine)


def format_money(amount: Money) -> str:
    negative, hryvni, kopiyky = amount.split()
    text = _LOCALE.toString(hryvni)
    if kopiyky:
        text = f"{text}{_LOCALE.decimalPoint()}{kopiyky:02d}"
    return f"−{text}" if negative else text


def format_month(month: CalendarMonth) -> str:
    """Назва місяця з роком, наприклад «Жовтень 2026»."""
    name = _LOCALE.standaloneMonthName(month.month, QLocale.FormatType.LongFormat)
    return f"{name[:1].upper()}{name[1:]} {month.year}"


def parse_money_input(text: str) -> Money | None:
    """Розбір поля суми: порожнє поле — ``None``; помилка формату — ValidationError."""
    from budget.domain.money import parse_money

    return parse_money(text) if text.strip() else None
