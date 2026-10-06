"""Тексти повідомлень для користувача, що містять суми (форматування — лише тут і в formatting)."""

from budget.errors import BudgetError
from budget.services.sources import InsufficientFundsError
from budget.ui.formatting import format_money


def insufficient_funds_text(source_name: str, available, required) -> str:
    return (
        f"У джерелі «{source_name}» доступно {format_money(available)}, "
        f"а потрібно {format_money(required)}. Зменште суму або оберіть інше джерело."
    )


def user_text(error: BudgetError) -> str:
    if isinstance(error, InsufficientFundsError):
        return insufficient_funds_text(error.source_name, error.available, error.required)
    return error.user_message
