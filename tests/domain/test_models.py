import pytest

from budget.domain.calendar import CalendarMonth
from budget.domain.models import (
    Accumulation,
    AccumulationStatus,
    Debt,
    DebtOrigin,
    Expense,
    Income,
    ReplenishmentPart,
    SourceKind,
    SourceRef,
    debt_status,
    income_status,
)
from budget.domain.money import Money
from budget.errors import DomainRuleError, ValidationError

MONTH = CalendarMonth(2026, 10)


def test_names_are_required():
    with pytest.raises(ValidationError):
        Income(None, MONTH, "  ", None, Money(100))
    with pytest.raises(ValidationError):
        Expense(None, MONTH, "", None, Money(100), SourceRef(SourceKind.GENERAL_REMAINDER))


def test_expense_source_reference_must_match_kind():
    with pytest.raises(ValueError):
        SourceRef(SourceKind.INCOME)
    with pytest.raises(ValueError):
        SourceRef(SourceKind.GENERAL_REMAINDER, income_id=1)


def test_only_closed_accumulation_can_be_archived():
    for status in (AccumulationStatus.ACTIVE, AccumulationStatus.REACHED):
        with pytest.raises(DomainRuleError):
            Accumulation(None, "На паркан", None, None, status, True, Money.zero())
    Accumulation(None, "На паркан", None, None, AccumulationStatus.CLOSED, True, Money.zero())


def test_accumulation_cannot_fund_replenishment():
    with pytest.raises(DomainRuleError):
        ReplenishmentPart(SourceRef(SourceKind.ACCUMULATION, accumulation_id=1), Money(100))


def test_initial_debt_has_no_month():
    Debt(None, DebtOrigin.INITIAL, None, "Позика", None, Money(100))
    with pytest.raises(ValueError):
        Debt(None, DebtOrigin.INITIAL, MONTH, "Позика", None, Money(100))
    with pytest.raises(ValueError):
        Debt(None, DebtOrigin.LOAN_RECEIPT, None, "Картка", None, Money(100))


def test_derived_statuses():
    assert income_status(Money.zero()).value == "completed"
    assert income_status(Money(1)).value == "active"
    assert debt_status(Money.zero()).value == "paid"
