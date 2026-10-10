"""Поведінкові й регресійні сценарії звичайних витрат: атомарність і невід'ємність джерел."""

import pytest

from budget.domain.money import Money
from budget.errors import DomainRuleError
from budget.services.balances import BalanceService
from budget.services.income import IncomeService
from budget.services.sources import InsufficientFundsError, SourceLedger
from budget.storage.repositories import AccumulationRepository
from tests.services.expense_fixtures import (
    REMAINDER,
    complete_setup,
    count_expenses,
    expenses,
    income_source,
)


@pytest.fixture
def accumulation(db, clock):
    return complete_setup(db, clock)


class Interrupted(Exception):
    pass


def snapshot(db, clock):
    balances = BalanceService(db)
    incomes = IncomeService(db, clock)
    income_rows = db.execute("SELECT id FROM incomes ORDER BY id").fetchall()
    return (
        balances.general_remainder(),
        tuple(incomes.get(row[0]).balance for row in income_rows),
        tuple(balances.accumulation_balance(a) for a in AccumulationRepository(db).list_all()),
        count_expenses(db),
    )


def assert_no_negative_source(db, clock):
    remainder, incomes, accumulations, _ = snapshot(db, clock)
    for balance in (remainder, *incomes, *accumulations):
        assert not balance.is_negative


def test_exact_remainder_succeeds_and_one_kopiyka_more_is_blocked(db, clock, accumulation):
    service = expenses(db, clock)
    service.create("Перша", None, Money(6_000), REMAINDER)
    with pytest.raises(InsufficientFundsError) as error:
        service.create("Друга", None, Money(4_001), REMAINDER)
    assert (error.value.available, error.value.required) == (Money(4_000), Money(4_001))
    service.create("Друга", None, Money(4_000), REMAINDER)
    assert BalanceService(db).general_remainder() == Money.zero()


def test_create_rolls_back_when_interrupted_after_debit(db, clock, accumulation, monkeypatch):
    """Збій після списання з нерозподіленого залишку не лишає ні запису, ні списання."""
    original = SourceLedger.debit_general_remainder

    def debit_then_fail(self, amount):
        original(self, amount)
        raise Interrupted

    service = expenses(db, clock)
    before = snapshot(db, clock)
    monkeypatch.setattr(SourceLedger, "debit_general_remainder", debit_then_fail)
    with pytest.raises(Interrupted):
        service.create("Продукти", None, Money(3_000), REMAINDER)
    assert snapshot(db, clock) == before


def test_edit_rolls_back_when_interrupted_between_revert_and_apply(
    db, clock, accumulation, monkeypatch
):
    """Старий ефект знято, новий не застосовано — транзакція повертає початковий стан."""
    service = expenses(db, clock)
    income = income_source(db, clock, 5_000)
    view = service.create("Ремонт", None, Money(2_000), REMAINDER)
    before = snapshot(db, clock)
    monkeypatch.setattr(service, "_apply", lambda expense: (_ for _ in ()).throw(Interrupted))
    with pytest.raises(Interrupted):
        service.update(view.expense.id, "Ремонт", None, Money(1_000), income)
    assert snapshot(db, clock) == before
    unchanged = service.get(view.expense.id).expense
    assert (unchanged.amount, unchanged.source) == (Money(2_000), REMAINDER)


def test_sources_never_go_negative_across_operations(db, clock, accumulation):
    service = expenses(db, clock)
    income = income_source(db, clock, 4_000)
    attempts = [
        ("create", "А", Money(3_000), income),
        ("create", "Б", Money(1_500), income),
        ("create", "В", Money(9_000), REMAINDER),
        ("create", "Г", Money(1_001), REMAINDER),
        ("create", "Ґ", Money(5_001), accumulation),
        ("create", "Д", Money(5_000), accumulation),
        ("create", "Е", Money(1), accumulation),
    ]
    created = []
    for _, name, amount, source in attempts:
        try:
            created.append(service.create(name, None, amount, source).expense.id)
        except DomainRuleError:
            pass
        assert_no_negative_source(db, clock)
    for expense_id in created:
        for amount, source in ((Money(20_000), REMAINDER), (Money(6_000), accumulation)):
            try:
                service.update(expense_id, "Зміна", None, amount, source)
            except DomainRuleError:
                pass
            assert_no_negative_source(db, clock)
    assert snapshot(db, clock)[0] == Money(1_000)
