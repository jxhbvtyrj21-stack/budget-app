from datetime import UTC, datetime

import pytest

from budget.domain.calendar import CalendarMonth
from budget.domain.models import IncomeStatus
from budget.domain.money import Money
from budget.errors import DomainRuleError, ValidationError
from budget.services.balances import BalanceService
from budget.services.income import IncomeService
from budget.services.sources import InsufficientFundsError
from budget.storage.transaction import transaction
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


def balances(db):
    return BalanceService(db)


def test_expense_from_income_reduces_balance(db, clock, accumulation):
    source = income_source(db, clock, 10_000)
    view = expenses(db, clock).create("Продукти", "Супермаркет", Money(4_000), source)
    assert view.expense.month == CalendarMonth(2026, 10)
    assert view.source_name == "Замовлення" and view.expense.description == "Супермаркет"
    assert IncomeService(db, clock).get(source.income_id).balance == Money(6_000)


def test_expense_from_general_remainder(db, clock, accumulation):
    expenses(db, clock).create("Комунальні", None, Money(2_500), REMAINDER)
    assert balances(db).general_remainder() == Money(7_500)


def test_expense_from_accumulation_is_ordinary_expense(db, clock, accumulation):
    view = expenses(db, clock).create("Квитки", None, Money(1_000), accumulation)
    assert view.source_name == "Подорож"
    (acc,) = balances(db)._accumulations.list_all()
    assert balances(db).accumulation_balance(acc) == Money(4_000)
    assert acc.status.value == "active"  # статус не змінюється автоматично
    assert db.execute("SELECT COUNT(*) FROM replenishments").fetchone()[0] == 0


def test_exact_balance_succeeds_and_archives_income(db, clock, accumulation):
    source = income_source(db, clock, 3_000)
    expenses(db, clock).create("Усе", None, Money(3_000), source)
    view = IncomeService(db, clock).get(source.income_id)
    assert view.balance == Money.zero()
    assert view.status is IncomeStatus.COMPLETED and view.income.archived


def test_archived_income_cannot_be_source(db, clock, accumulation):
    source = income_source(db, clock, 1_000)
    expenses(db, clock).create("Усе", None, Money(1_000), source)
    with pytest.raises(DomainRuleError):
        expenses(db, clock).create("Ще", None, Money(1), source)
    assert count_expenses(db) == 1


@pytest.mark.parametrize("kind", ["income", "remainder", "accumulation"])
def test_insufficient_source_blocked_with_structured_details(db, clock, accumulation, kind):
    source = {
        "income": lambda: income_source(db, clock, 4_000),
        "remainder": lambda: REMAINDER,
        "accumulation": lambda: accumulation,
    }[kind]()
    available = {"income": 4_000, "remainder": 10_000, "accumulation": 5_000}[kind]
    before = balances(db).available_funds()
    with pytest.raises(InsufficientFundsError) as error:
        expenses(db, clock).create("Завелика", None, Money(available + 1), source)
    assert error.value.available == Money(available)
    assert error.value.required == Money(available + 1)
    assert error.value.source_name
    assert count_expenses(db) == 0
    assert balances(db).available_funds() == before


def test_no_automatic_switching_split_or_top_up(db, clock, accumulation):
    """5 000 з доходу на 4 000: не добирається з залишку чи накопичення й не ділиться."""
    source = income_source(db, clock, 4_000)
    before = balances(db).available_funds()
    with pytest.raises(InsufficientFundsError):
        expenses(db, clock).create("Покупка", None, Money(5_000), source)
    assert balances(db).available_funds() == before
    assert count_expenses(db) == 0


def test_one_source_only(db, clock, accumulation):
    import inspect

    from budget.services.expense import ExpenseService

    parameters = list(inspect.signature(ExpenseService.create).parameters)
    assert parameters == ["self", "name", "description", "amount", "source"]


def test_required_name_optional_description(db, clock, accumulation):
    with pytest.raises(ValidationError):
        expenses(db, clock).create("   ", None, Money(100), REMAINDER)
    view = expenses(db, clock).create("Кава", "  ", Money(100), REMAINDER)
    assert view.expense.description is None
    assert count_expenses(db) == 1


def test_positive_amount_required(db, clock, accumulation):
    with pytest.raises(ValidationError):
        expenses(db, clock).create("Кава", None, Money.zero(), REMAINDER)


def test_current_month_only(db, clock, accumulation):
    clock.set(datetime(2026, 11, 1, 0, 30, tzinfo=UTC))  # 1 листопада 02:30 за Києвом
    view = expenses(db, clock).create("Кава", None, Money(100), REMAINDER)
    assert view.expense.month == CalendarMonth(2026, 11)


def test_income_of_previous_month_is_not_a_source(db, clock, accumulation):
    source = income_source(db, clock, 5_000)
    clock.set(datetime(2026, 11, 3, 9, 0, tzinfo=UTC))
    with pytest.raises(DomainRuleError):
        expenses(db, clock).create("Кава", None, Money(100), source)


def test_closed_unarchived_accumulation_allowed_archived_blocked(db, clock, accumulation):
    set_accumulation_state(db, accumulation, "closed", False)
    expenses(db, clock).create("Залишки", None, Money(500), accumulation)
    set_accumulation_state(db, accumulation, "closed", True)
    with pytest.raises(DomainRuleError):
        expenses(db, clock).create("Ще", None, Money(1), accumulation)
    assert count_expenses(db) == 1


def test_blocked_before_setup(db, clock):
    with pytest.raises(DomainRuleError):
        expenses(db, clock).create("Кава", None, Money(100), REMAINDER)


def test_base_minimum_does_not_limit_expenses(db, clock, accumulation):
    with transaction(db):
        db.execute("INSERT INTO base_minimums (month, amount) VALUES ('2026-10', 100)")
    expenses(db, clock).create("Більше за мінімум", None, Money(9_000), REMAINDER)
    assert balances(db).general_remainder() == Money(1_000)


def test_source_options_list_only_selectable_sources(db, clock, accumulation):
    income = income_source(db, clock, 2_000, name="Аванс")
    options = expenses(db, clock).source_options()
    assert [(o.name, o.available) for o in options] == [
        ("Аванс", Money(2_000)),
        ("Загальний нерозподілений залишок", Money(10_000)),
        ("Подорож", Money(5_000)),
    ]
    set_accumulation_state(db, accumulation, "closed", True)
    assert "Подорож" not in [o.name for o in expenses(db, clock).source_options()]
    assert income
