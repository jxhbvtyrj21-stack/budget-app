"""Базовий мінімум: лише поточний місяць, без впливу на фінанси (ADR 0002, 0019, 0022)."""

from datetime import UTC, datetime

import pytest

from budget.domain.calendar import CalendarMonth
from budget.domain.models import SourceKind, SourceRef
from budget.domain.money import Money
from budget.errors import DomainRuleError, ValidationError
from budget.services.balances import BalanceService
from budget.services.base_minimum import BaseMinimumService
from budget.services.expense import ExpenseService
from budget.services.setup import InitialSetupService, SetupDraft
from budget.storage.repositories import FinancialRecordRepository

OCTOBER = CalendarMonth(2026, 10)
NOVEMBER = CalendarMonth(2026, 11)
SEPTEMBER = CalendarMonth(2026, 9)


@pytest.fixture
def service(db, clock):
    InitialSetupService(db, clock).complete(SetupDraft(general_remainder=Money(10_000)))
    return BaseMinimumService(db, clock)


def test_set_and_update_current_month(service):
    assert service.get(OCTOBER) is None
    assert service.set(OCTOBER, Money(3_000_000)) == Money(3_000_000)
    service.set(OCTOBER, Money(2_800_000))
    assert service.get(OCTOBER) == Money(2_800_000)
    assert service.is_editable(OCTOBER) and not service.is_editable(SEPTEMBER)


def test_zero_accepted_negative_rejected(service):
    service.set(OCTOBER, Money.zero())
    assert service.get(OCTOBER) == Money.zero()
    with pytest.raises(ValidationError):
        service.set(OCTOBER, Money(-1))
    assert service.get(OCTOBER) == Money.zero()


def test_historical_month_is_read_only(service, clock):
    service.set(OCTOBER, Money(1_000))
    clock.set(datetime(2026, 11, 2, 9, 0, tzinfo=UTC))
    assert service.get(OCTOBER) == Money(1_000)  # читання минулого місяця
    with pytest.raises(DomainRuleError):
        service.set(OCTOBER, Money(2_000))
    with pytest.raises(DomainRuleError):
        service.set(SEPTEMBER, Money(2_000))
    assert service.get(OCTOBER) == Money(1_000)


def test_future_month_cannot_be_set(service):
    with pytest.raises(DomainRuleError):
        service.set(NOVEMBER, Money(1))


def test_no_automatic_carry_forward(service, clock):
    service.set(OCTOBER, Money(3_000_000))
    clock.set(datetime(2026, 11, 2, 9, 0, tzinfo=UTC))
    assert service.get(NOVEMBER) is None


def test_not_a_financial_record_and_no_financial_effect(db, clock, service):
    balances = BalanceService(db)
    funds = balances.available_funds()
    service.set(OCTOBER, Money(3_000_000))
    assert OCTOBER not in FinancialRecordRepository(db).months_with_records()
    assert balances.available_funds() == funds
    assert balances.general_remainder() == Money(10_000)
    assert balances.active_debts_total() == Money.zero()
    for table in ("incomes", "expenses", "replenishments", "debts", "debt_repayments"):
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_not_a_limit_for_expenses(db, clock, service):
    service.set(OCTOBER, Money(100))
    remainder = SourceRef(SourceKind.GENERAL_REMAINDER)
    ExpenseService(db, clock).create("Продукти", None, Money(5_000), remainder)
    assert service.get(OCTOBER) == Money(100)


def test_q190_blocked_before_setup(db, clock):
    with pytest.raises(DomainRuleError):
        BaseMinimumService(db, clock).set(OCTOBER, Money(1))
    assert BaseMinimumService(db, clock).get(OCTOBER) is None
