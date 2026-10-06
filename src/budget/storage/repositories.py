"""Репозиторії фінансових сутностей: лише читання й запис рядків та агрегування сум.

Бізнес-рішень тут немає: перевірки достатності коштів, поточного місяця, архівування
й налаштування виконує сервісний шар. Транзакції відкриває сервіс.
"""

import sqlite3

from budget.domain.calendar import CalendarMonth
from budget.domain.models import (
    Accumulation,
    AccumulationStatus,
    Debt,
    DebtOrigin,
    Expense,
    Income,
    SourceKind,
    SourceRef,
)
from budget.domain.money import Money


def _money(value: int | None) -> Money:
    return Money(int(value or 0))


class GeneralRemainderRepository:
    """Єдиний збережений залишок — загальний нерозподілений залишок (ADR 0003, Q167)."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def get(self) -> Money:
        (balance,) = self._connection.execute(
            "SELECT balance FROM general_remainder WHERE id = 1"
        ).fetchone()
        return Money(balance)

    def set(self, balance: Money) -> None:
        self._connection.execute(
            "UPDATE general_remainder SET balance = ? WHERE id = 1", (balance.kopiyky,)
        )


def _income(row: tuple) -> Income:
    income_id, month, name, description, amount, archived = row
    return Income(
        income_id, CalendarMonth.parse(month), name, description, Money(amount), bool(archived)
    )


_INCOME_COLUMNS = "id, month, name, description, amount, archived"


class IncomeRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def insert(self, income: Income) -> int:
        cursor = self._connection.execute(
            "INSERT INTO incomes (month, name, description, amount) VALUES (?, ?, ?, ?)",
            (str(income.month), income.name, income.description, income.amount.kopiyky),
        )
        return int(cursor.lastrowid)

    def get(self, income_id: int) -> Income | None:
        row = self._connection.execute(
            f"SELECT {_INCOME_COLUMNS} FROM incomes WHERE id = ?", (income_id,)
        ).fetchone()
        return _income(row) if row else None

    def list_for_month(self, month: CalendarMonth) -> list[Income]:
        rows = self._connection.execute(
            f"SELECT {_INCOME_COLUMNS} FROM incomes WHERE month = ? ORDER BY id DESC",
            (str(month),),
        )
        return [_income(row) for row in rows]

    def list_unarchived(self) -> list[Income]:
        rows = self._connection.execute(
            f"SELECT {_INCOME_COLUMNS} FROM incomes WHERE archived = 0 ORDER BY id"
        )
        return [_income(row) for row in rows]

    def set_archived(self, income_id: int) -> None:
        self._connection.execute("UPDATE incomes SET archived = 1 WHERE id = ?", (income_id,))

    def charged_total(self, income_id: int) -> Money:
        """Сума всіх операцій, джерелом яких є цей дохід."""
        (total,) = self._connection.execute(
            """
            SELECT
              (SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE source_income_id = :id)
            + (SELECT COALESCE(SUM(amount), 0) FROM replenishment_parts
                 WHERE source_income_id = :id)
            + (SELECT COALESCE(SUM(amount), 0) FROM debt_repayments WHERE source_income_id = :id)
            """,
            {"id": income_id},
        ).fetchone()
        return _money(total)


def _accumulation(row: tuple) -> Accumulation:
    acc_id, name, description, target, status, archived, initial = row
    return Accumulation(
        acc_id,
        name,
        description,
        Money(target) if target is not None else None,
        AccumulationStatus(status),
        bool(archived),
        Money(initial),
    )


class AccumulationRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def insert(self, accumulation: Accumulation) -> int:
        target = accumulation.target.kopiyky if accumulation.target is not None else None
        cursor = self._connection.execute(
            "INSERT INTO accumulations (name, description, target, status, archived,"
            " initial_balance) VALUES (?, ?, ?, ?, ?, ?)",
            (
                accumulation.name,
                accumulation.description,
                target,
                accumulation.status.value,
                int(accumulation.archived),
                accumulation.initial_balance.kopiyky,
            ),
        )
        return int(cursor.lastrowid)

    def get(self, accumulation_id: int) -> Accumulation | None:
        row = self._connection.execute(
            "SELECT id, name, description, target, status, archived, initial_balance"
            " FROM accumulations WHERE id = ?",
            (accumulation_id,),
        ).fetchone()
        return _accumulation(row) if row else None

    def list_all(self) -> list[Accumulation]:
        rows = self._connection.execute(
            "SELECT id, name, description, target, status, archived, initial_balance"
            " FROM accumulations ORDER BY id"
        )
        return [_accumulation(row) for row in rows]

    def movement_total(self, accumulation_id: int) -> Money:
        """Поповнення мінус витрати й погашення з накопичення (без початкового балансу)."""
        (total,) = self._connection.execute(
            """
            SELECT
              (SELECT COALESCE(SUM(p.amount), 0) FROM replenishment_parts p
                 JOIN replenishments r ON r.id = p.replenishment_id
                 WHERE r.accumulation_id = :id)
            - (SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE source_accumulation_id = :id)
            - (SELECT COALESCE(SUM(amount), 0) FROM debt_repayments
                 WHERE source_accumulation_id = :id)
            """,
            {"id": accumulation_id},
        ).fetchone()
        return _money(total)


def _expense(row: tuple) -> Expense:
    expense_id, month, name, description, amount, kind, income_id, accumulation_id = row
    return Expense(
        expense_id,
        CalendarMonth.parse(month),
        name,
        description,
        Money(amount),
        SourceRef(SourceKind(kind), income_id, accumulation_id),
    )


_EXPENSE_COLUMNS = (
    "id, month, name, description, amount, source_kind, source_income_id, source_accumulation_id"
)


class ExpenseRepository:
    """Звичайні витрати. Залишки джерел тут не змінюються й не перевіряються."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def insert(self, expense: Expense) -> int:
        """Записує витрату; ``expense.id`` зберігається, якщо задано (заміна під час зміни)."""
        cursor = self._connection.execute(
            f"INSERT INTO expenses ({_EXPENSE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                expense.id,
                str(expense.month),
                expense.name,
                expense.description,
                expense.amount.kopiyky,
                expense.source.kind.value,
                expense.source.income_id,
                expense.source.accumulation_id,
            ),
        )
        return int(cursor.lastrowid)

    def get(self, expense_id: int) -> Expense | None:
        row = self._connection.execute(
            f"SELECT {_EXPENSE_COLUMNS} FROM expenses WHERE id = ?", (expense_id,)
        ).fetchone()
        return _expense(row) if row else None

    def list_for_month(self, month: CalendarMonth) -> list[Expense]:
        rows = self._connection.execute(
            f"SELECT {_EXPENSE_COLUMNS} FROM expenses WHERE month = ? ORDER BY id DESC",
            (str(month),),
        )
        return [_expense(row) for row in rows]

    def update_metadata(self, expense_id: int, name: str, description: str | None) -> None:
        self._connection.execute(
            "UPDATE expenses SET name = ?, description = ? WHERE id = ?",
            (name, description, expense_id),
        )

    def delete(self, expense_id: int) -> None:
        self._connection.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))


def _debt(row: tuple) -> Debt:
    debt_id, origin, month, name, description, amount = row
    return Debt(
        debt_id,
        DebtOrigin(origin),
        CalendarMonth.parse(month) if month is not None else None,
        name,
        description,
        Money(amount),
    )


class DebtRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def insert(self, debt: Debt) -> int:
        cursor = self._connection.execute(
            "INSERT INTO debts (origin, month, name, description, amount) VALUES (?, ?, ?, ?, ?)",
            (
                debt.origin.value,
                str(debt.month) if debt.month is not None else None,
                debt.name,
                debt.description,
                debt.amount.kopiyky,
            ),
        )
        return int(cursor.lastrowid)

    def list_all(self) -> list[Debt]:
        rows = self._connection.execute(
            "SELECT id, origin, month, name, description, amount FROM debts ORDER BY id"
        )
        return [_debt(row) for row in rows]

    def repaid_total(self, debt_id: int) -> Money:
        (total,) = self._connection.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM debt_repayments WHERE debt_id = ?", (debt_id,)
        ).fetchone()
        return _money(total)


class FinancialRecordRepository:
    """Місяці, у яких є фінансові записи (ADR 0010, Q153; ADR 0018)."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def months_with_records(self) -> set[CalendarMonth]:
        rows = self._connection.execute(
            """
            SELECT month FROM incomes
            UNION SELECT month FROM expenses
            UNION SELECT month FROM replenishments
            UNION SELECT month FROM debts WHERE month IS NOT NULL
            UNION SELECT month FROM debt_repayments
            """
        )
        return {CalendarMonth.parse(month) for (month,) in rows}
