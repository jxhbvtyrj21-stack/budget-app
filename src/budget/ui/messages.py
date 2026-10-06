"""Тексти повідомлень для користувача, що містять суми (форматування — лише тут і в formatting)."""

from budget.errors import BudgetError
from budget.services.accumulation import CloseBlockedError
from budget.services.replenishment import RecipientBalanceError
from budget.services.sources import InsufficientFundsError
from budget.ui.formatting import format_money


def insufficient_funds_text(source_name: str, available, required) -> str:
    return (
        f"У джерелі «{source_name}» доступно {format_money(available)}, "
        f"а потрібно {format_money(required)}. Зменште суму або оберіть інше джерело."
    )


def close_blocked_text(balance) -> str:
    return f"Закрити накопичення можна лише при залишку 0. Зараз залишок {format_money(balance)}."


def recipient_balance_text(name: str, balance, reduction) -> str:
    return (
        f"Зміну не можна зберегти: залишок накопичення «{name}» — {format_money(balance)}, "
        f"а зміна зменшила б його на {format_money(reduction)}. Частину цих коштів уже "
        "витрачено з накопичення."
    )


def user_text(error: BudgetError) -> str:
    if isinstance(error, RecipientBalanceError):
        return recipient_balance_text(error.name, error.balance, error.reduction)
    if isinstance(error, CloseBlockedError):
        return close_blocked_text(error.balance)
    if isinstance(error, InsufficientFundsError):
        return insufficient_funds_text(error.source_name, error.available, error.required)
    return error.user_message
