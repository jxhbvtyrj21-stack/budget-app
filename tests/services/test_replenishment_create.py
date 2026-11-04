"""Створення поповнення (ADR 0007, ADR 0010 Q152/Q156, ADR 0013 Q187, ADR 0022)."""

from datetime import UTC, datetime

import pytest

from budget.domain.models import AccumulationStatus
from budget.domain.money import Money
from budget.errors import DomainRuleError, ValidationError
from budget.services.accumulation import AccumulationService
from budget.services.balances import BalanceService
from budget.services.income import IncomeService
from budget.services.sources import InsufficientFundsError, SourceLedger
from tests.services.expense_fixtures import set_accumulation_state
from tests.services.replenishment_fixtures import (
    REMAINDER,
    acc_balance,
    acc_source,
    complete_setup,
    count_replenishments,
    income_balance,
    income_source,
    part,
    remainder,
    replenishments,
)


@pytest.fixture
def trip(db, clock):
    """«Подорож» (початковий баланс 5 000); загальний нерозподілений залишок 10 000."""
    return complete_setup(db, clock).accumulation_id


def test_single_source_moves_funds_into_accumulation(db, clock, trip):
    total_before = BalanceService(db).available_funds().total
    view = replenishments(db, clock).create("Відкладаю", None, trip, [part(REMAINDER, 4_000)])
    assert view.replenishment.month.year == 2026 and view.replenishment.month.month == 10
    assert view.recipient_name == "Подорож" and view.source_names == (
        "Загальний нерозподілений залишок",
    )
    assert remainder(db) == Money(6_000)
    assert acc_balance(db, trip) == Money(9_000)
    # Переміщення коштів: загальна доступна сума не змінюється.
    assert BalanceService(db).available_funds().total == total_before


def test_multiple_sources_in_one_operation(db, clock, trip):
    income = income_source(db, clock, 12_000)
    view = replenishments(db, clock).create(
        "На паркан", "Жовтень", trip, [part(income, 7_000), part(REMAINDER, 3_000)]
    )
    assert view.total == Money(10_000) and len(view.replenishment.parts) == 2
    assert income_balance(db, clock, income) == Money(5_000)
    assert remainder(db) == Money(7_000)
    assert acc_balance(db, trip) == Money(15_000)
    assert count_replenishments(db) == 1


def test_duplicate_source_is_checked_as_a_whole(db, clock, trip):
    """Дві частини з одного джерела не обходять його залишок (сума за джерелом)."""
    service = replenishments(db, clock)
    with pytest.raises(InsufficientFundsError) as error:
        service.create("Двічі", None, trip, [part(REMAINDER, 6_000), part(REMAINDER, 5_000)])
    assert (error.value.available, error.value.required) == (Money(10_000), Money(11_000))
    assert remainder(db) == Money(10_000) and count_replenishments(db) == 0
    service.create("Двічі", None, trip, [part(REMAINDER, 6_000), part(REMAINDER, 4_000)])
    assert remainder(db) == Money.zero()


def test_exact_balance_succeeds_one_kopiyka_more_blocked(db, clock, trip):
    service = replenishments(db, clock)
    income = income_source(db, clock, 2_000)
    with pytest.raises(InsufficientFundsError) as error:
        service.create("Усе", None, trip, [part(income, 2_001), part(REMAINDER, 1)])
    assert error.value.source_name == "Замовлення"
    assert (error.value.available, error.value.required) == (Money(2_000), Money(2_001))
    # Жодна частина не застосована: операція відхилена повністю.
    assert remainder(db) == Money(10_000) and income_balance(db, clock, income) == Money(2_000)
    service.create("Усе", None, trip, [part(income, 2_000)])
    # Дохід із нульовим залишком автоматично архівується (ADR 0009).
    assert IncomeService(db, clock).get(income.income_id).income.archived


def test_accumulation_cannot_be_a_source(db, clock, trip):
    with pytest.raises(DomainRuleError):
        replenishments(db, clock).create(
            "Між накопиченнями", None, trip, [part(acc_source(trip), 1)]
        )
    assert count_replenishments(db) == 0


def test_archived_income_is_not_a_source(db, clock, trip):
    service = replenishments(db, clock)
    income = income_source(db, clock, 1_000)
    service.create("Усе", None, trip, [part(income, 1_000)])
    with pytest.raises(DomainRuleError) as error:
        service.create("Ще", None, trip, [part(income, 1)])
    assert "архівовано" in error.value.user_message
    assert all(o.source != income for o in service.source_options())


def test_income_of_previous_month_is_not_a_source(db, clock, trip):
    income = income_source(db, clock, 1_000)
    clock.set(datetime(2026, 11, 2, 9, 0, tzinfo=UTC))
    with pytest.raises(DomainRuleError):
        replenishments(db, clock).create("Пізно", None, trip, [part(income, 100)])


def test_archived_accumulation_is_not_a_recipient(db, clock, trip):
    accumulations = AccumulationService(db, clock)
    closed = accumulations.create("Старе", None, None).accumulation.id
    accumulations.change_status(closed, AccumulationStatus.CLOSED)
    accumulations.archive(closed)
    service = replenishments(db, clock)
    with pytest.raises(DomainRuleError) as error:
        service.create("Не можна", None, closed, [part(REMAINDER, 100)])
    assert "архіві" in error.value.user_message
    assert remainder(db) == Money(10_000)
    assert closed not in [o.accumulation_id for o in service.recipient_options()]


@pytest.mark.parametrize("status", ["active", "reached", "closed"])
def test_unarchived_recipient_of_any_status(db, clock, trip, status):
    set_accumulation_state(db, acc_source(trip), status, False)
    replenishments(db, clock).create("Поповнення", None, trip, [part(REMAINDER, 500)])
    accumulation = AccumulationService(db, clock).get(trip).accumulation
    assert accumulation.status.value == status  # статус не змінюється автоматично
    assert acc_balance(db, trip) == Money(5_500)


def test_unknown_recipient_rejected(db, clock, trip):
    with pytest.raises(DomainRuleError):
        replenishments(db, clock).create("Нікуди", None, 999, [part(REMAINDER, 1)])
    assert remainder(db) == Money(10_000)


def test_name_required_and_parts_required(db, clock, trip):
    service = replenishments(db, clock)
    with pytest.raises(ValidationError):
        service.create(" ", None, trip, [part(REMAINDER, 1)])
    with pytest.raises(ValidationError):
        service.create("Без джерел", None, trip, [])
    with pytest.raises(ValidationError):
        service.create("Нуль", None, trip, [part(REMAINDER, 0)])
    assert count_replenishments(db) == 0


def test_blocked_before_setup(db, clock):
    with pytest.raises(DomainRuleError):
        replenishments(db, clock).create("Рано", None, 1, [part(REMAINDER, 1)])


def test_belongs_to_current_kyiv_month(db, clock, trip):
    # 31 жовтня 23:30 UTC — уже 1 листопада за Києвом.
    clock.set(datetime(2026, 10, 31, 23, 30, tzinfo=UTC))
    view = replenishments(db, clock).create("Нічне", None, trip, [part(REMAINDER, 1)])
    assert str(view.replenishment.month) == "2026-11"


def test_source_and_recipient_options(db, clock, trip):
    service = replenishments(db, clock)
    income = income_source(db, clock, 3_000)
    options = service.source_options()
    assert [(o.source, o.available) for o in options] == [
        (income, Money(3_000)),
        (REMAINDER, Money(10_000)),
    ]
    assert all(o.source.kind.value != "accumulation" for o in options)
    assert [(o.name, o.balance) for o in service.recipient_options()] == [("Подорож", Money(5_000))]


def test_create_rolls_back_when_interrupted_after_debit(db, clock, trip, monkeypatch):
    original = SourceLedger.debit_general_remainder

    def debit_then_fail(self, amount):
        original(self, amount)
        raise RuntimeError("збій")

    income = income_source(db, clock, 1_000)
    monkeypatch.setattr(SourceLedger, "debit_general_remainder", debit_then_fail)
    with pytest.raises(RuntimeError):
        replenishments(db, clock).create(
            "Збій", None, trip, [part(income, 1_000), part(REMAINDER, 100)]
        )
    assert count_replenishments(db) == 0
    assert remainder(db) == Money(10_000)
    assert income_balance(db, clock, income) == Money(1_000)
    assert not IncomeService(db, clock).get(income.income_id).income.archived
