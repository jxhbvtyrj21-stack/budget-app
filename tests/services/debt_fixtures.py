"""Підготовка сценаріїв для тестів боргів (стартовий стан — через сервіси, як у майстрі)."""

from budget.domain.money import Money
from budget.services.balances import BalanceService
from budget.services.debt import DebtService
from budget.services.setup import InitialDebt, InitialSetupService, SetupDraft
from budget.storage.repositories import DebtRepository


def complete_setup_with_debt(db, clock, remainder=10_000, initial_debt=3_000) -> int:
    """Нерозподілений залишок і початковий борг «Позика в Олени»; повертає id боргу."""
    InitialSetupService(db, clock).complete(
        SetupDraft(
            general_remainder=Money(remainder),
            debts=(InitialDebt("Позика в Олени", None, Money(initial_debt)),),
        )
    )
    (debt,) = DebtRepository(db).list_all()
    return debt.id


def debts(db, clock) -> DebtService:
    return DebtService(db, clock)


def remainder(db) -> Money:
    return BalanceService(db).general_remainder()


def count_debts(db) -> int:
    return db.execute("SELECT COUNT(*) FROM debts").fetchone()[0]
