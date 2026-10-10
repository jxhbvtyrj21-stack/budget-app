"""Отримання позикових коштів, сума й видалення отримання, метадані боргу.

ADR 0018 (пп. 1, 2, 4, 5, 7, 8), ADR 0022 (п. 4, 5), Q172, Q190; правило «погашений
борг не відкривається повторно».
"""

from datetime import UTC, datetime

import pytest

from budget.domain.calendar import CalendarMonth, current_month
from budget.domain.models import DebtOrigin, DebtRepayment, DebtStatus, SourceKind, SourceRef
from budget.domain.money import Money
from budget.errors import DomainRuleError, ValidationError
from budget.services.balances import BalanceService
from budget.services.debt import DebtReopenError, DebtRepaidFloorError
from budget.services.expense import ExpenseService
from budget.services.sources import InsufficientFundsError
from budget.storage.repositories import DebtRepository
from budget.storage.transaction import transaction
from tests.services.debt_fixtures import complete_setup_with_debt, count_debts, debts, remainder

REMAINDER = SourceRef(SourceKind.GENERAL_REMAINDER)


@pytest.fixture
def initial(db, clock):
    return complete_setup_with_debt(db, clock)


def add_repayment(db, debt_id, amount):
    """Фікстура погашення напряму через сховище (сервіс погашень — окремий блок)."""
    with transaction(db):
        DebtRepository(db).insert_repayment(
            DebtRepayment(None, debt_id, CalendarMonth(2026, 10), None, Money(amount), REMAINDER)
        )
        db.execute("UPDATE general_remainder SET balance = balance - ?", (amount,))


def test_receive_loan_creates_debt_and_credits_remainder(db, clock, initial):
    view = debts(db, clock).receive_loan("Кредитна картка", "Ноутбук", Money(5_000))
    debt = view.debt
    assert debt.origin is DebtOrigin.LOAN_RECEIPT and str(debt.month) == "2026-10"
    assert (view.remaining, view.status) == (Money(5_000), DebtStatus.ACTIVE)
    assert remainder(db) == Money(15_000)
    # Не дохід: доходів немає; борг не входить до загальної доступної суми.
    assert db.execute("SELECT COUNT(*) FROM incomes").fetchone()[0] == 0
    funds = BalanceService(db).available_funds()
    assert funds.total == Money(15_000) and funds.active_incomes == Money.zero()
    assert BalanceService(db).active_debts_total() == Money(8_000)


def test_each_receipt_is_a_separate_debt(db, clock, initial):
    service = debts(db, clock)
    first = service.receive_loan("Картка", None, Money(1_000))
    second = service.receive_loan("Картка", None, Money(2_000))
    assert first.debt.id != second.debt.id and count_debts(db) == 3
    assert [v.debt.id for v in service.list_for_month(first.debt.month)] == [
        second.debt.id,
        first.debt.id,
    ]


def test_receive_loan_validation_and_q190(db, clock, initial):
    service = debts(db, clock)
    with pytest.raises(ValidationError):
        service.receive_loan(" ", None, Money(100))
    with pytest.raises(ValidationError):
        service.receive_loan("Нуль", None, Money.zero())
    assert count_debts(db) == 1 and remainder(db) == Money(10_000)


def test_blocked_before_setup(db, clock):
    with pytest.raises(DomainRuleError):
        debts(db, clock).receive_loan("Картка", None, Money(100))


def test_initial_debt_is_not_a_receipt_and_is_fixed(db, clock, initial):
    """Q172: початковий борг не змінюється й не видаляється як «початковий»."""
    service = debts(db, clock)
    view = service.get(initial)
    assert view.debt.origin is DebtOrigin.INITIAL and view.debt.month is None
    assert service.list_for_month(current_month(clock)) == []
    assert service.loan_lock_reason(view.debt)
    with pytest.raises(DomainRuleError):
        service.update_loan_amount(initial, Money(5_000))
    with pytest.raises(DomainRuleError):
        service.delete_loan(initial)
    assert service.get(initial).debt.amount == Money(3_000) and remainder(db) == Money(10_000)


def test_update_loan_amount_moves_remainder(db, clock, initial):
    service = debts(db, clock)
    view = service.receive_loan("Картка", None, Money(5_000))
    service.update_loan_amount(view.debt.id, Money(7_000))
    assert remainder(db) == Money(17_000)
    service.update_loan_amount(view.debt.id, Money(4_000))
    assert remainder(db) == Money(14_000)
    assert service.get(view.debt.id).remaining == Money(4_000)


def test_loan_amount_floor_is_repaid_sum(db, clock, initial):
    service = debts(db, clock)
    view = service.receive_loan("Картка", None, Money(5_000))
    add_repayment(db, view.debt.id, 2_000)
    with pytest.raises(DebtRepaidFloorError) as error:
        service.update_loan_amount(view.debt.id, Money(1_999))
    assert error.value.repaid == Money(2_000)
    service.update_loan_amount(view.debt.id, Money(2_500))
    assert service.get(view.debt.id).remaining == Money(500)


def test_loan_decrease_cannot_make_remainder_negative(db, clock, initial):
    service = debts(db, clock)
    view = service.receive_loan("Картка", None, Money(5_000))
    ExpenseService(db, clock).create("Ноутбук", None, Money(14_000), REMAINDER)
    with pytest.raises(InsufficientFundsError):
        service.update_loan_amount(view.debt.id, Money(3_000))
    with pytest.raises(InsufficientFundsError):
        service.delete_loan(view.debt.id)
    assert service.get(view.debt.id).debt.amount == Money(5_000)
    assert remainder(db) == Money(1_000)


def test_paid_debt_amount_increase_is_blocked(db, clock, initial):
    """Правило: погашений борг не відкривається повторно збільшенням суми отримання."""
    service = debts(db, clock)
    view = service.receive_loan("Картка", None, Money(1_000))
    add_repayment(db, view.debt.id, 1_000)
    assert service.get(view.debt.id).status is DebtStatus.PAID
    before = remainder(db)
    with pytest.raises(DebtReopenError):
        service.update_loan_amount(view.debt.id, Money(1_500))
    after = service.get(view.debt.id)
    assert after.debt.amount == Money(1_000) and after.status is DebtStatus.PAID
    assert remainder(db) == before


def test_delete_loan_without_repayments(db, clock, initial):
    service = debts(db, clock)
    view = service.receive_loan("Картка", None, Money(2_000))
    service.delete_loan(view.debt.id)
    assert count_debts(db) == 1 and remainder(db) == Money(10_000)


def test_delete_loan_with_repayments_blocked(db, clock, initial):
    service = debts(db, clock)
    view = service.receive_loan("Картка", None, Money(2_000))
    add_repayment(db, view.debt.id, 100)
    with pytest.raises(DebtRepaidFloorError):
        service.delete_loan(view.debt.id)
    assert service.get(view.debt.id).debt.amount == Money(2_000)


def test_loan_of_past_month_is_read_only(db, clock, initial):
    service = debts(db, clock)
    view = service.receive_loan("Картка", None, Money(2_000))
    clock.set(datetime(2026, 11, 2, 9, 0, tzinfo=UTC))
    with pytest.raises(DomainRuleError):
        service.update_loan_amount(view.debt.id, Money(3_000))
    with pytest.raises(DomainRuleError):
        service.delete_loan(view.debt.id)
    assert service.loan_lock_reason(service.get(view.debt.id).debt)


def test_metadata_editable_for_past_paid_and_initial_debts(db, clock, initial):
    service = debts(db, clock)
    paid = service.receive_loan("Картка", None, Money(1_000))
    add_repayment(db, paid.debt.id, 1_000)
    clock.set(datetime(2026, 12, 2, 9, 0, tzinfo=UTC))
    funds = BalanceService(db).available_funds()
    for debt_id in (paid.debt.id, initial):
        view = service.update_metadata(debt_id, "Нова назва", "Опис")
        assert (view.debt.name, view.debt.description) == ("Нова назва", "Опис")
    assert service.get(paid.debt.id).status is DebtStatus.PAID
    assert service.get(initial).debt.amount == Money(3_000)
    assert BalanceService(db).available_funds() == funds
    with pytest.raises(ValidationError):
        service.update_metadata(initial, "  ", None)
    assert service.get(initial).debt.name == "Нова назва"


def test_active_and_paid_lists(db, clock, initial):
    service = debts(db, clock)
    paid = service.receive_loan("Картка", None, Money(1_000))
    add_repayment(db, paid.debt.id, 1_000)
    assert [v.debt.id for v in service.list_active()] == [initial]
    assert [v.debt.id for v in service.list_paid()] == [paid.debt.id]
    assert service.active_total() == Money(3_000)
