"""Сховище поповнень: заголовок і частини, читання, заміна структури, видалення."""

import sqlite3

import pytest

from budget.domain.calendar import CalendarMonth
from budget.domain.models import (
    Accumulation,
    AccumulationStatus,
    Income,
    Replenishment,
    ReplenishmentPart,
    SourceKind,
    SourceRef,
)
from budget.domain.money import Money
from budget.storage.repositories import (
    AccumulationRepository,
    IncomeRepository,
    ReplenishmentRepository,
)
from budget.storage.transaction import transaction

OCTOBER = CalendarMonth(2026, 10)
NOVEMBER = CalendarMonth(2026, 11)
REMAINDER = SourceRef(SourceKind.GENERAL_REMAINDER)


@pytest.fixture
def setup(connection):
    with transaction(connection):
        acc = AccumulationRepository(connection).insert(
            Accumulation(None, "Подорож", None, None, AccumulationStatus.ACTIVE, False, Money(0))
        )
        other = AccumulationRepository(connection).insert(
            Accumulation(None, "Ремонт", None, None, AccumulationStatus.ACTIVE, False, Money(0))
        )
        income = IncomeRepository(connection).insert(
            Income(None, OCTOBER, "Аванс", None, Money(10_000))
        )
    return acc, other, SourceRef(SourceKind.INCOME, income_id=income)


def make(acc, income, month=OCTOBER, name="Відкладаю", replenishment_id=None):
    return Replenishment(
        replenishment_id,
        month,
        name,
        None,
        acc,
        (ReplenishmentPart(income, Money(3_000)), ReplenishmentPart(REMAINDER, Money(2_000))),
    )


def test_insert_and_get_round_trip(connection, setup):
    acc, _, income = setup
    repository = ReplenishmentRepository(connection)
    with transaction(connection):
        rep_id = repository.insert(make(acc, income))
    stored = repository.get(rep_id)
    assert stored == make(acc, income, replenishment_id=rep_id)
    assert stored.total == Money(5_000)
    assert repository.get(rep_id + 100) is None


def test_lists_by_month_and_accumulation(connection, setup):
    acc, other, income = setup
    repository = ReplenishmentRepository(connection)
    with transaction(connection):
        first = repository.insert(make(acc, income, name="Перше"))
        second = repository.insert(make(other, income, name="Друге"))
        third = repository.insert(make(acc, income, month=NOVEMBER, name="Третє"))
    assert [r.id for r in repository.list_for_month(OCTOBER)] == [second, first]
    assert [r.id for r in repository.list_for_accumulation(acc)] == [third, first]
    assert [r.id for r in repository.list_for_accumulation(other)] == [second]


def test_replace_swaps_structure_in_place(connection, setup):
    acc, other, income = setup
    repository = ReplenishmentRepository(connection)
    with transaction(connection):
        rep_id = repository.insert(make(acc, income))
    replacement = Replenishment(
        rep_id, OCTOBER, "Нове", "Опис", other, (ReplenishmentPart(REMAINDER, Money(700)),)
    )
    with transaction(connection):
        repository.replace(replacement)
    assert repository.get(rep_id) == replacement
    parts = connection.execute("SELECT COUNT(*) FROM replenishment_parts").fetchone()[0]
    assert parts == 1


def test_replace_requires_transaction(connection, setup):
    acc, _, income = setup
    repository = ReplenishmentRepository(connection)
    with transaction(connection):
        rep_id = repository.insert(make(acc, income))
    with pytest.raises(RuntimeError):
        repository.replace(make(acc, income, replenishment_id=rep_id))


def test_failed_replace_leaves_old_structure(connection, setup):
    acc, _, income = setup
    repository = ReplenishmentRepository(connection)
    with transaction(connection):
        rep_id = repository.insert(make(acc, income))
    broken = Replenishment(
        rep_id, OCTOBER, "Нове", None, 999, (ReplenishmentPart(REMAINDER, Money(1)),)
    )
    with pytest.raises(sqlite3.IntegrityError), transaction(connection):
        repository.replace(broken)  # отримувача 999 немає — зовнішній ключ
    assert repository.get(rep_id) == make(acc, income, replenishment_id=rep_id)


def test_update_metadata_and_delete_cascade(connection, setup):
    acc, _, income = setup
    repository = ReplenishmentRepository(connection)
    with transaction(connection):
        rep_id = repository.insert(make(acc, income))
        repository.update_metadata(rep_id, "Перейменоване", "Опис")
    stored = repository.get(rep_id)
    assert (stored.name, stored.description) == ("Перейменоване", "Опис")
    assert stored.parts == make(acc, income).parts
    with transaction(connection):
        repository.delete(rep_id)
    assert repository.get(rep_id) is None
    assert connection.execute("SELECT COUNT(*) FROM replenishment_parts").fetchone()[0] == 0


def test_parts_feed_existing_balance_aggregates(connection, setup):
    acc, _, income = setup
    with transaction(connection):
        ReplenishmentRepository(connection).insert(make(acc, income))
    assert IncomeRepository(connection).charged_total(income.income_id) == Money(3_000)
    assert AccumulationRepository(connection).movement_total(acc) == Money(5_000)


def test_duplicate_source_parts_are_stored_as_given(connection, setup):
    acc, _, income = setup
    duplicate = Replenishment(
        None,
        OCTOBER,
        "Двічі",
        None,
        acc,
        (ReplenishmentPart(income, Money(100)), ReplenishmentPart(income, Money(200))),
    )
    assert duplicate.totals_by_source() == {income: Money(300)}
    with transaction(connection):
        rep_id = ReplenishmentRepository(connection).insert(duplicate)
    assert len(ReplenishmentRepository(connection).get(rep_id).parts) == 2
