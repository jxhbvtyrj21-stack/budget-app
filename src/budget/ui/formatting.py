"""Єдине місце форматування сум, місяців, дат і розмірів для показу (design-system.md, 3.3).

Розділювачі — за ``QLocale`` ``uk_UA``; позначка валюти не показується (ADR 0008);
копійки — лише якщо вони не нульові; округлення не виконується.
"""

from datetime import datetime

from PySide6.QtCore import QLocale

from budget.domain.calendar import CalendarMonth
from budget.domain.money import Money
from budget.services.backup import BackupKind

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


def format_moment(moment: datetime) -> str:
    """Дата й час, наприклад «6 жовтня 2026, 12:00» (час — як у назві копії, за Києвом)."""
    month = _LOCALE.monthName(moment.month, QLocale.FormatType.LongFormat)
    return f"{moment.day} {month} {moment.year}, {moment:%H:%M}"


def format_size(size: int) -> str:
    """Розмір файлу: «84,0 кБ», «1,2 МБ»."""
    return _LOCALE.formattedDataSize(size, 1, QLocale.DataSizeFormat.DataSizeTraditionalFormat)


BACKUP_KIND_LABELS = {
    BackupKind.DAILY: "Щоденна",
    BackupKind.WEEKLY: "Щотижнева",
    BackupKind.MONTHLY: "Щомісячна",
    BackupKind.BEFORE_MIGRATION: "Перед міграцією",
    BackupKind.ON_DEMAND: "На вимогу",
    BackupKind.BEFORE_RESTORE: "Перед відновленням",
}


def format_backup_kind(kind: BackupKind) -> str:
    """Вид резервної копії (IA 8)."""
    return BACKUP_KIND_LABELS[kind]


def parse_money_input(text: str) -> Money | None:
    """Розбір поля суми: порожнє поле — ``None``; помилка формату — ValidationError."""
    from budget.domain.money import parse_money

    return parse_money(text) if text.strip() else None
