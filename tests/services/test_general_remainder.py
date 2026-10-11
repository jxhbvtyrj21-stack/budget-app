import pytest

from budget.domain.money import Money
from budget.errors import DomainRuleError, ValidationError
from budget.services.sources import GeneralRemainderService, InsufficientFundsError, SourceLedger
from budget.storage.transaction import transaction


def test_initial_value_is_zero(completed_db, clock):
    assert GeneralRemainderService(completed_db, clock).current() == Money.zero()


def test_increase_and_decrease(completed_db, clock):
    service = GeneralRemainderService(completed_db, clock)
    service.increase(Money(10_000))
    service.decrease(Money(2_500))
    assert service.current() == Money(7_500)


def test_insufficient_balance_is_blocked_with_details(completed_db, clock):
    service = GeneralRemainderService(completed_db, clock)
    service.increase(Money(1_000))
    with pytest.raises(InsufficientFundsError) as error:
        service.decrease(Money(1_001))
    assert error.value.available == Money(1_000)
    assert error.value.required == Money(1_001)
    assert service.current() == Money(1_000)


def test_non_positive_amounts_rejected(completed_db, clock):
    service = GeneralRemainderService(completed_db, clock)
    with pytest.raises(ValidationError):
        service.increase(Money.zero())
    with pytest.raises(ValidationError):
        service.decrease(Money(-1))


def test_ledger_requires_transaction(completed_db, clock):
    with pytest.raises(RuntimeError):
        SourceLedger(completed_db, clock).credit_general_remainder(Money(1))


def test_atomicity_on_failure(completed_db, clock):
    service = GeneralRemainderService(completed_db, clock)
    service.increase(Money(5_000))
    ledger = SourceLedger(completed_db, clock)
    with pytest.raises(DomainRuleError), transaction(completed_db):
        ledger.debit_general_remainder(Money(2_000))
        raise DomainRuleError("наступний крок операції не вдався")
    assert service.current() == Money(5_000)


def test_negative_remainder_blocked_by_storage(completed_db):
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        completed_db.execute("UPDATE general_remainder SET balance = -1")
