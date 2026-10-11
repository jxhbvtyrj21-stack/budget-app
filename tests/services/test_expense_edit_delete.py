from datetime import UTC, datetime

import pytest

from budget.domain.money import Money
from budget.errors import DomainRuleError, ValidationError
from budget.services.balances import BalanceService
from budget.services.income import IncomeService
from budget.services.sources import InsufficientFundsError
from tests.services.expense_fixtures import (
    REMAINDER,
    complete_setup,
    count_expenses,
    expenses,
    income_source,
    set_accumulation_state,
)


@pytest.fixture
def accumulation(db, clock):
    return complete_setup(db, clock)


def remainder(db):
    return BalanceService(db).general_remainder()


def income_balance(db, clock, source):
    return IncomeService(db, clock).get(source.income_id).balance


def test_amount_edit_same_source(db, clock, accumulation):
    service = expenses(db, clock)
    view = service.create("Продукти", None, Money(3_000), REMAINDER)
    service.update(view.expense.id, "Продукти", None, Money(4_500), REMAINDER)
    assert remainder(db) == Money(5_500)
    service.update(view.expense.id, "Продукти", None, Money(1_000), REMAINDER)
    assert remainder(db) == Money(9_000)


def test_amount_edit_counts_old_amount_as_available(db, clock, accumulation):
    """Повна сума джерела доступна для зміни тієї самої витрати: проміжного мінуса немає."""
    service = expenses(db, clock)
    view = service.create("Усе", None, Money(10_000), REMAINDER)
    assert remainder(db) == Money.zero()
    service.update(view.expense.id, "Усе", None, Money(9_000), REMAINDER)
    assert remainder(db) == Money(1_000)


def test_source_edit_moves_financial_effect(db, clock, accumulation):
    service = expenses(db, clock)
    income = income_source(db, clock, 8_000)
    view = service.create("Ремонт", None, Money(2_000), income)
    service.update(view.expense.id, "Ремонт", None, Money(2_000), REMAINDER)
    assert income_balance(db, clock, income) == Money(8_000)
    assert remainder(db) == Money(8_000)
    service.update(view.expense.id, "Ремонт", None, Money(2_000), accumulation)
    assert remainder(db) == Money(10_000)
    assert service.get(view.expense.id).source_name == "Подорож"


def test_name_and_description_edit(db, clock, accumulation):
    service = expenses(db, clock)
    view = service.create("Кава", None, Money(100), REMAINDER)
    updated = service.update(view.expense.id, "Кава з собою", "Ранок", Money(100), REMAINDER)
    assert (updated.expense.name, updated.expense.description) == ("Кава з собою", "Ранок")
    assert updated.expense.id == view.expense.id
    with pytest.raises(ValidationError):
        service.update(view.expense.id, " ", None, Money(100), REMAINDER)
    assert service.get(view.expense.id).expense.name == "Кава з собою"


def test_insufficient_new_configuration_rejected_atomically(db, clock, accumulation):
    service = expenses(db, clock)
    income = income_source(db, clock, 1_000)
    view = service.create("Продукти", None, Money(2_000), REMAINDER)
    before = (remainder(db), income_balance(db, clock, income))
    with pytest.raises(InsufficientFundsError) as error:
        service.update(view.expense.id, "Продукти", None, Money(2_000), income)
    assert (error.value.available, error.value.required) == (Money(1_000), Money(2_000))
    with pytest.raises(InsufficientFundsError):
        service.update(view.expense.id, "Продукти", None, Money(10_001), REMAINDER)
    assert (remainder(db), income_balance(db, clock, income)) == before
    unchanged = service.get(view.expense.id).expense
    assert (unchanged.amount, unchanged.source) == (Money(2_000), REMAINDER)


def test_edit_that_would_restore_archived_income_is_blocked(db, clock, accumulation):
    """Q168: витрата архівувала дохід — фінансова зміна, що повернула б кошти, заблокована."""
    service = expenses(db, clock)
    income = income_source(db, clock, 3_000)
    view = service.create("Усе", None, Money(3_000), income)
    assert IncomeService(db, clock).get(income.income_id).income.archived
    for amount, source in ((Money(2_000), income), (Money(3_000), REMAINDER)):
        with pytest.raises(DomainRuleError):
            service.update(view.expense.id, "Усе", None, amount, source)
    after = IncomeService(db, clock).get(income.income_id)
    assert after.income.archived and after.balance == Money.zero()
    # Метадані змінювати можна: кошти не повертаються.
    service.update(view.expense.id, "Усе за замовлення", None, Money(3_000), income)
    assert IncomeService(db, clock).get(income.income_id).income.archived


def test_current_month_delete_restores_source(db, clock, accumulation):
    service = expenses(db, clock)
    income = income_source(db, clock, 5_000)
    by_income = service.create("Одне", None, Money(1_500), income)
    by_remainder = service.create("Друге", None, Money(2_500), REMAINDER)
    by_acc = service.create("Третє", None, Money(500), accumulation)
    service.delete(by_income.expense.id)
    service.delete(by_remainder.expense.id)
    service.delete(by_acc.expense.id)
    assert income_balance(db, clock, income) == Money(5_000)
    assert remainder(db) == Money(10_000)
    assert BalanceService(db).available_funds().accumulations == Money(5_000)
    assert count_expenses(db) == 0


def test_delete_that_would_restore_archived_income_is_blocked(db, clock, accumulation):
    service = expenses(db, clock)
    income = income_source(db, clock, 2_000)
    view = service.create("Усе", None, Money(2_000), income)
    with pytest.raises(DomainRuleError):
        service.delete(view.expense.id)
    assert count_expenses(db) == 1
    assert IncomeService(db, clock).get(income.income_id).income.archived


def test_historical_edit_and_delete_blocked(db, clock, accumulation):
    service = expenses(db, clock)
    view = service.create("Жовтнева", None, Money(1_000), REMAINDER)
    clock.set(datetime(2026, 11, 2, 9, 0, tzinfo=UTC))
    with pytest.raises(DomainRuleError):
        service.update(view.expense.id, "Інша", None, Money(1_000), REMAINDER)
    with pytest.raises(DomainRuleError):
        service.delete(view.expense.id)
    assert service.get(view.expense.id).expense.name == "Жовтнева"
    assert remainder(db) == Money(9_000)
    assert service.financial_lock_reason(view.expense)


def test_archived_accumulation_operation_metadata_only(db, clock, accumulation):
    """Q189, Q191: фінансові зміни заблоковані, назва й опис — дозволені."""
    service = expenses(db, clock)
    set_accumulation_state(db, accumulation, "closed", False)
    view = service.create("Квитки", None, Money(5_000), accumulation)
    set_accumulation_state(db, accumulation, "closed", True)  # залишок 0, архів
    assert service.financial_lock_reason(view.expense)
    with pytest.raises(DomainRuleError):
        service.update(view.expense.id, "Квитки", None, Money(4_000), accumulation)
    with pytest.raises(DomainRuleError):
        service.delete(view.expense.id)
    renamed = service.update(view.expense.id, "Квитки на потяг", "Київ", Money(5_000), accumulation)
    assert renamed.expense.name == "Квитки на потяг"
    assert renamed.expense.amount == Money(5_000)


def test_edit_and_delete_blocked_before_setup(db, clock):
    with pytest.raises(DomainRuleError):
        expenses(db, clock).delete(1)
