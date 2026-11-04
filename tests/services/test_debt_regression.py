"""Регресія боргів: взаємодія з витратами, доходами, накопиченнями, переходом місяця.

ADR 0002 (пп. 5–7), ADR 0003, ADR 0009, ADR 0010 (Q153, Q155), ADR 0018, ADR 0022;
правило «погашений борг не відкривається повторно».
"""

from datetime import UTC, datetime

import pytest

from budget.domain.calendar import CalendarMonth
from budget.domain.models import (
    AccumulationStatus,
    DebtOrigin,
    DebtStatus,
    ReplenishmentPart,
    SourceKind,
    SourceRef,
)
from budget.domain.money import Money
from budget.errors import DomainRuleError
from budget.services.accumulation import AccumulationService, CloseBlockedError
from budget.services.balances import BalanceService
from budget.services.debt import DebtReopenError
from budget.services.expense import ExpenseService
from budget.services.income import IncomeService
from budget.services.month import MonthTransitionService
from budget.services.replenishment import ReplenishmentService
from budget.services.sources import InsufficientFundsError
from budget.storage.repositories import FinancialRecordRepository
from tests.services.debt_fixtures import complete_setup_with_debt, debts, remainder

REMAINDER = SourceRef(SourceKind.GENERAL_REMAINDER)
OCTOBER = CalendarMonth(2026, 10)


@pytest.fixture
def initial(db, clock):
    """Початковий борг 3 000; загальний нерозподілений залишок 10 000."""
    return complete_setup_with_debt(db, clock)


def test_loan_then_expense_then_loan_cannot_shrink_below_spent(db, clock, initial):
    """Отримання → витрата з нерозподіленого залишку → зменшення й видалення отримання
    не створюють від'ємного залишку (ADR 0018, пп. 2, 5)."""
    service = debts(db, clock)
    loan = service.receive_loan("Кредитна картка", None, Money(5_000)).debt.id
    assert remainder(db) == Money(15_000)
    ExpenseService(db, clock).create("Ноутбук", None, Money(13_000), REMAINDER)
    with pytest.raises(InsufficientFundsError):
        service.update_loan_amount(loan, Money(2_000))
    with pytest.raises(InsufficientFundsError):
        service.delete_loan(loan)
    assert remainder(db) == Money(2_000)
    service.update_loan_amount(loan, Money(3_000))  # у межах залишку
    assert remainder(db) == Money.zero()


def test_repayment_sources_end_to_end(db, clock, initial):
    service = debts(db, clock)
    income = IncomeService(db, clock).create("Аванс", None, Money(1_000)).income
    income_source = SourceRef(SourceKind.INCOME, income_id=income.id)
    acc = AccumulationService(db, clock).create("Подорож", None, None).accumulation.id
    acc_source = SourceRef(SourceKind.ACCUMULATION, accumulation_id=acc)
    ReplenishmentService(db, clock).create(
        "Відкладаю", None, acc, [ReplenishmentPart(REMAINDER, Money(1_500))]
    )
    service.repay(initial, Money(1_000), income_source)
    assert IncomeService(db, clock).get(income.id).income.archived
    service.repay(initial, Money(500), acc_source)
    assert AccumulationService(db, clock).get(acc).balance == Money(1_000)
    service.repay(initial, Money(1_500), REMAINDER)
    assert service.get(initial).status is DebtStatus.PAID
    assert remainder(db) == Money(10_000 - 1_500 - 1_500)
    # Погашення не є звичайними витратами.
    assert ExpenseService(db, clock).list_for_month(OCTOBER) == []


def test_debt_is_not_part_of_total_available(db, clock, initial):
    balances = BalanceService(db)
    assert balances.available_funds().total == Money(10_000)
    assert balances.active_debts_total() == Money(3_000)
    debts(db, clock).receive_loan("Картка", None, Money(2_000))
    # Отримані кошти — у нерозподіленому залишку; сам борг суму не зменшує.
    assert balances.available_funds().total == Money(12_000)
    assert balances.active_debts_total() == Money(5_000)
    debts(db, clock).repay(initial, Money(1_000), REMAINDER)
    assert balances.available_funds().total == Money(11_000)
    assert balances.active_debts_total() == Money(4_000)


def test_month_records_include_debt_operations(db, clock, initial):
    records = FinancialRecordRepository(db)
    assert OCTOBER not in records.months_with_records()  # початковий борг — не запис
    loan = debts(db, clock).receive_loan("Картка", None, Money(1_000)).debt.id
    assert OCTOBER in records.months_with_records()
    debts(db, clock).delete_loan(loan)
    assert OCTOBER not in records.months_with_records()
    repayment = debts(db, clock).repay(initial, Money(100), REMAINDER)
    assert OCTOBER in records.months_with_records()
    debts(db, clock).delete_repayment(repayment.repayment.id)
    assert OCTOBER not in records.months_with_records()


def test_month_transition_keeps_debts(db, clock, initial):
    service = debts(db, clock)
    loan = service.receive_loan("Картка", None, Money(2_000)).debt.id
    service.repay(loan, Money(500), REMAINDER)
    clock.set(datetime(2026, 11, 2, 9, 0, tzinfo=UTC))
    MonthTransitionService(db, clock).run_on_startup()
    assert service.get(loan).remaining == Money(1_500)
    assert service.get(initial).remaining == Money(3_000)
    # Борг минулого місяця погашається в поточному; операції минулого — лише перегляд.
    service.repay(loan, Money(1_500), REMAINDER)
    assert service.get(loan).status is DebtStatus.PAID
    with pytest.raises(DomainRuleError):
        service.update_loan_amount(loan, Money(2_500))


def test_initial_debts_never_become_receipts(db, clock, initial):
    view = debts(db, clock).get(initial)
    assert view.debt.origin is DebtOrigin.INITIAL and view.debt.month is None
    assert remainder(db) == Money(10_000)  # не зараховано до нерозподіленого залишку
    assert debts(db, clock).list_for_month(OCTOBER) == []


def test_repayment_from_closed_accumulation_keeps_status(db, clock, initial):
    accumulations = AccumulationService(db, clock)
    acc = accumulations.create("Ремонт", None, None).accumulation.id
    ReplenishmentService(db, clock).create(
        "Ремонт", None, acc, [ReplenishmentPart(REMAINDER, Money(800))]
    )
    with pytest.raises(CloseBlockedError):
        accumulations.change_status(acc, AccumulationStatus.CLOSED)  # залишок > 0
    debts(db, clock).repay(
        initial, Money(800), SourceRef(SourceKind.ACCUMULATION, accumulation_id=acc)
    )
    assert accumulations.get(acc).accumulation.status is AccumulationStatus.ACTIVE
    accumulations.change_status(acc, AccumulationStatus.CLOSED)  # тепер залишок 0


# Погашений борг не відкривається повторно -------------------------------------------------


@pytest.fixture
def paid(db, clock, initial):
    service = debts(db, clock)
    loan = service.receive_loan("Позика в брата", None, Money(2_000)).debt.id
    service.repay(loan, Money(500), REMAINDER)
    last = service.repay(loan, Money(1_500), REMAINDER)
    assert service.get(loan).status is DebtStatus.PAID
    return loan, last.repayment.id


def snapshot(db):
    return (
        db.execute("SELECT * FROM general_remainder").fetchall(),
        db.execute("SELECT * FROM debts ORDER BY id").fetchall(),
        db.execute("SELECT * FROM debt_repayments ORDER BY id").fetchall(),
    )


def test_paid_debt_reducing_last_repayment_blocked(db, clock, paid):
    _, last = paid
    before = snapshot(db)
    with pytest.raises(DebtReopenError):
        debts(db, clock).update_repayment(last, Money(1_000), REMAINDER, None)
    assert snapshot(db) == before


def test_paid_debt_deleting_last_repayment_blocked(db, clock, paid):
    _, last = paid
    before = snapshot(db)
    with pytest.raises(DebtReopenError):
        debts(db, clock).delete_repayment(last)
    assert snapshot(db) == before


def test_paid_debt_increasing_receipt_blocked(db, clock, paid):
    loan, _ = paid
    before = snapshot(db)
    with pytest.raises(DebtReopenError):
        debts(db, clock).update_loan_amount(loan, Money(2_001))
    assert snapshot(db) == before


def test_paid_debt_metadata_editable(db, clock, paid):
    loan, _ = paid
    view = debts(db, clock).update_metadata(loan, "Позика в брата Івана", "Повернуто")
    assert (view.debt.name, view.debt.description) == ("Позика в брата Івана", "Повернуто")
    assert view.status is DebtStatus.PAID and view.remaining == Money.zero()
