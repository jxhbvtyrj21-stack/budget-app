"""Сховище базового мінімуму: один запис на місяць, задання й зміна, межі суми."""

import sqlite3

import pytest

from budget.domain.calendar import CalendarMonth
from budget.domain.models import BaseMinimum
from budget.domain.money import Money
from budget.errors import ValidationError
from budget.storage.repositories import BaseMinimumRepository
from budget.storage.transaction import transaction

OCTOBER = CalendarMonth(2026, 10)
NOVEMBER = CalendarMonth(2026, 11)


@pytest.fixture
def repository(connection):
    return BaseMinimumRepository(connection)


def test_absent_value_is_none(repository):
    assert repository.get(OCTOBER) is None


def test_set_and_get(connection, repository):
    with transaction(connection):
        repository.set(BaseMinimum(OCTOBER, Money(3_000_000)))
    assert repository.get(OCTOBER) == BaseMinimum(OCTOBER, Money(3_000_000))
    assert repository.get(NOVEMBER) is None


def test_repeated_set_updates_single_row(connection, repository):
    with transaction(connection):
        repository.set(BaseMinimum(OCTOBER, Money(3_000_000)))
        repository.set(BaseMinimum(OCTOBER, Money(2_500_050)))
    assert repository.get(OCTOBER).amount == Money(2_500_050)
    assert connection.execute("SELECT COUNT(*) FROM base_minimums").fetchone()[0] == 1


def test_zero_is_valid(connection, repository):
    with transaction(connection):
        repository.set(BaseMinimum(OCTOBER, Money.zero()))
    assert repository.get(OCTOBER).amount == Money.zero()


def test_negative_rejected_by_domain_and_schema(connection, repository):
    with pytest.raises(ValidationError):
        BaseMinimum(OCTOBER, Money(-1))
    with pytest.raises(sqlite3.IntegrityError), transaction(connection):
        connection.execute("INSERT INTO base_minimums (month, amount) VALUES ('2026-10', -1)")
    assert repository.get(OCTOBER) is None


def test_no_delete_operation(repository):
    assert not any("delete" in name for name in dir(BaseMinimumRepository))
