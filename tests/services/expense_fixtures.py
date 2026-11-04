"""Підготовка сценаріїв для тестів витрат: стартовий стан через сервіси, стани через SQL."""

from budget.domain.models import SourceKind, SourceRef
from budget.domain.money import Money
from budget.services.expense import ExpenseService
from budget.services.income import IncomeService
from budget.services.setup import InitialAccumulation, InitialSetupService, SetupDraft
from budget.storage.repositories import AccumulationRepository
from budget.storage.transaction import transaction

REMAINDER = SourceRef(SourceKind.GENERAL_REMAINDER)


def complete_setup(db, clock, remainder=10_000, accumulation=5_000):
    InitialSetupService(db, clock).complete(
        SetupDraft(
            general_remainder=Money(remainder),
            accumulations=(InitialAccumulation("Подорож", None, Money(accumulation)),),
        )
    )
    (acc,) = AccumulationRepository(db).list_all()
    return SourceRef(SourceKind.ACCUMULATION, accumulation_id=acc.id)


def income_source(db, clock, amount, name="Замовлення"):
    income = IncomeService(db, clock).create(name, None, Money(amount)).income
    return SourceRef(SourceKind.INCOME, income_id=income.id)


def set_accumulation_state(db, source, status, archived):
    with transaction(db):
        db.execute(
            "UPDATE accumulations SET status = ?, archived = ? WHERE id = ?",
            (status, int(archived), source.accumulation_id),
        )


def expenses(db, clock):
    return ExpenseService(db, clock)


def count_expenses(db):
    return db.execute("SELECT COUNT(*) FROM expenses").fetchone()[0]
