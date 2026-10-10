"""Підготовка сценаріїв для тестів поповнень (стартовий стан — через сервіси)."""

from budget.domain.models import ReplenishmentPart, SourceKind, SourceRef
from budget.domain.money import Money
from budget.services.balances import BalanceService
from budget.services.income import IncomeService
from budget.services.replenishment import ReplenishmentService
from budget.storage.repositories import AccumulationRepository
from tests.services.expense_fixtures import REMAINDER, complete_setup, income_source

__all__ = [
    "REMAINDER",
    "acc_balance",
    "acc_source",
    "complete_setup",
    "count_replenishments",
    "income_balance",
    "income_source",
    "part",
    "remainder",
    "replenishments",
]


def replenishments(db, clock) -> ReplenishmentService:
    return ReplenishmentService(db, clock)


def part(source, amount) -> ReplenishmentPart:
    return ReplenishmentPart(source, Money(amount))


def acc_source(accumulation_id) -> SourceRef:
    return SourceRef(SourceKind.ACCUMULATION, accumulation_id=accumulation_id)


def remainder(db) -> Money:
    return BalanceService(db).general_remainder()


def income_balance(db, clock, source) -> Money:
    return IncomeService(db, clock).get(source.income_id).balance


def acc_balance(db, accumulation_id) -> Money:
    accumulation = AccumulationRepository(db).get(accumulation_id)
    return BalanceService(db).accumulation_balance(accumulation)


def count_replenishments(db) -> int:
    return db.execute("SELECT COUNT(*) FROM replenishments").fetchone()[0]
