"""Зміна й видалення поповнення: Q171, Q174, Q175, Q177, Q189, Q191, лише перегляд минулого."""

from datetime import UTC, datetime

import pytest

from budget.domain.models import AccumulationStatus
from budget.domain.money import Money
from budget.errors import DomainRuleError, ValidationError
from budget.services.accumulation import AccumulationService
from budget.services.expense import ExpenseService
from budget.services.income import IncomeService
from budget.services.replenishment import RecipientBalanceError
from budget.services.sources import InsufficientFundsError, SourceLedger
from budget.storage.repositories import ReplenishmentRepository
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


class Interrupted(Exception):
    pass


@pytest.fixture
def trip(db, clock):
    """«Подорож» (5 000); загальний нерозподілений залишок 10 000."""
    return complete_setup(db, clock).accumulation_id


@pytest.fixture
def other(db, clock, trip):
    return AccumulationService(db, clock).create("Ремонт", None, None).accumulation.id


def state(db, clock, *accumulations):
    return (
        remainder(db),
        tuple(acc_balance(db, a) for a in accumulations),
        db.execute("SELECT * FROM replenishments ORDER BY id").fetchall(),
        db.execute("SELECT * FROM replenishment_parts ORDER BY id").fetchall(),
        db.execute("SELECT id, archived FROM incomes ORDER BY id").fetchall(),
    )


# Редагування ------------------------------------------------------------------------------


def test_metadata_only_edit_changes_no_balance(db, clock, trip):
    service = replenishments(db, clock)
    view = service.create("Відкладаю", None, trip, [part(REMAINDER, 1_000)])
    before = (remainder(db), acc_balance(db, trip))
    updated = service.update(
        view.replenishment.id, "На море", "Серпень", trip, [part(REMAINDER, 1_000)]
    )
    assert (updated.replenishment.name, updated.replenishment.description) == ("На море", "Серпень")
    assert (remainder(db), acc_balance(db, trip)) == before
    with pytest.raises(ValidationError):
        service.update(view.replenishment.id, "  ", None, trip, [part(REMAINDER, 1_000)])
    assert service.get(view.replenishment.id).replenishment.name == "На море"


def test_full_edit_sources_amounts_and_recipient(db, clock, trip, other):
    service = replenishments(db, clock)
    income = income_source(db, clock, 8_000)
    view = service.create("Відкладаю", None, trip, [part(REMAINDER, 4_000)])
    rep_id = view.replenishment.id
    service.update(rep_id, "Відкладаю", None, trip, [part(income, 3_000), part(REMAINDER, 1_000)])
    assert remainder(db) == Money(9_000) and income_balance(db, clock, income) == Money(5_000)
    assert acc_balance(db, trip) == Money(9_000)
    moved = service.update(rep_id, "Ремонт", None, other, [part(income, 2_000)])
    assert moved.recipient_name == "Ремонт" and moved.replenishment.id == rep_id
    assert remainder(db) == Money(10_000) and income_balance(db, clock, income) == Money(6_000)
    assert acc_balance(db, trip) == Money(5_000) and acc_balance(db, other) == Money(2_000)
    assert count_replenishments(db) == 1


def test_old_amount_counts_as_available_for_same_source(db, clock, trip):
    service = replenishments(db, clock)
    view = service.create("Усе", None, trip, [part(REMAINDER, 10_000)])
    assert remainder(db) == Money.zero()
    service.update(view.replenishment.id, "Усе", None, trip, [part(REMAINDER, 9_000)])
    assert remainder(db) == Money(1_000)


def test_insufficient_new_configuration_rejected_atomically(db, clock, trip):
    service = replenishments(db, clock)
    income = income_source(db, clock, 1_000)
    view = service.create("Відкладаю", None, trip, [part(REMAINDER, 2_000)])
    before = state(db, clock, trip)
    with pytest.raises(InsufficientFundsError) as error:
        service.update(view.replenishment.id, "Відкладаю", None, trip, [part(income, 1_500)])
    assert (error.value.available, error.value.required) == (Money(1_000), Money(1_500))
    with pytest.raises(InsufficientFundsError) as error:
        service.update(
            view.replenishment.id,
            "Відкладаю",
            None,
            trip,
            [part(REMAINDER, 7_000), part(REMAINDER, 5_001)],
        )
    assert (error.value.available, error.value.required) == (Money(10_000), Money(12_001))
    assert state(db, clock, trip) == before


def test_edit_to_archived_recipient_rejected(db, clock, trip, other):
    service = replenishments(db, clock)
    view = service.create("Відкладаю", None, trip, [part(REMAINDER, 1_000)])
    accumulations = AccumulationService(db, clock)
    accumulations.change_status(other, AccumulationStatus.CLOSED)
    accumulations.archive(other)
    before = state(db, clock, trip, other)
    with pytest.raises(DomainRuleError):
        service.update(view.replenishment.id, "Відкладаю", None, other, [part(REMAINDER, 1_000)])
    assert state(db, clock, trip, other) == before


def test_q174_part_from_archived_income_cannot_shrink(db, clock, trip, other):
    service = replenishments(db, clock)
    income = income_source(db, clock, 3_000)
    view = service.create("Усе", None, trip, [part(income, 3_000), part(REMAINDER, 500)])
    rep_id = view.replenishment.id
    assert IncomeService(db, clock).get(income.income_id).income.archived
    assert service.locked_sources(view.replenishment) == {income}
    before = state(db, clock, trip, other)
    for parts in ([part(income, 2_000), part(REMAINDER, 500)], [part(REMAINDER, 3_500)]):
        with pytest.raises(DomainRuleError) as error:
            service.update(rep_id, "Усе", None, trip, parts)
        assert "архівовано" in error.value.user_message
    assert state(db, clock, trip, other) == before
    # Частина з архівованого доходу незмінна — інші частини й отримувача змінити можна.
    service.update(rep_id, "Усе", None, other, [part(income, 3_000), part(REMAINDER, 200)])
    assert remainder(db) == Money(9_800) and acc_balance(db, other) == Money(3_200)
    after = IncomeService(db, clock).get(income.income_id)
    assert after.income.archived and after.balance == Money.zero()


def test_q189_q191_archived_recipient_metadata_only(db, clock, other):
    service = replenishments(db, clock)
    view = service.create("Ремонт", None, other, [part(REMAINDER, 1_000)])
    ExpenseService(db, clock).create("Фарба", None, Money(1_000), acc_source(other))
    accumulations = AccumulationService(db, clock)
    accumulations.change_status(other, AccumulationStatus.CLOSED)
    accumulations.archive(other)
    rep_id = view.replenishment.id
    assert service.financial_lock_reason(view.replenishment)
    before = state(db, clock, other)
    with pytest.raises(DomainRuleError):
        service.update(rep_id, "Ремонт", None, other, [part(REMAINDER, 900)])
    with pytest.raises(DomainRuleError):
        service.delete(rep_id)
    assert state(db, clock, other) == before
    renamed = service.update(rep_id, "Ремонт кухні", "Фарба", other, [part(REMAINDER, 1_000)])
    assert renamed.replenishment.name == "Ремонт кухні"
    assert AccumulationService(db, clock).get(other).accumulation.archived


def test_recipient_balance_never_goes_negative(db, clock, other, trip):
    service = replenishments(db, clock)
    view = service.create("Ремонт", None, other, [part(REMAINDER, 1_000)])
    ExpenseService(db, clock).create("Фарба", None, Money(800), acc_source(other))
    rep_id = view.replenishment.id
    before = state(db, clock, other, trip)
    with pytest.raises(RecipientBalanceError) as error:
        service.update(rep_id, "Ремонт", None, other, [part(REMAINDER, 700)])
    assert (error.value.balance, error.value.reduction) == (Money(200), Money(300))
    with pytest.raises(RecipientBalanceError):
        service.update(rep_id, "Ремонт", None, trip, [part(REMAINDER, 1_000)])
    with pytest.raises(RecipientBalanceError):
        service.delete(rep_id)
    assert state(db, clock, other, trip) == before
    # Зменшення в межах залишку дозволене.
    service.update(rep_id, "Ремонт", None, other, [part(REMAINDER, 800)])
    assert acc_balance(db, other) == Money.zero()


def test_historical_replenishment_fully_read_only(db, clock, trip):
    service = replenishments(db, clock)
    view = service.create("Жовтень", None, trip, [part(REMAINDER, 1_000)])
    clock.set(datetime(2026, 11, 2, 9, 0, tzinfo=UTC))
    with pytest.raises(DomainRuleError):
        service.update(view.replenishment.id, "Інша назва", None, trip, [part(REMAINDER, 1_000)])
    with pytest.raises(DomainRuleError):
        service.delete(view.replenishment.id)
    assert service.get(view.replenishment.id).replenishment.name == "Жовтень"
    assert service.financial_lock_reason(view.replenishment)


def test_edit_and_delete_blocked_before_setup(db, clock):
    with pytest.raises(DomainRuleError):
        replenishments(db, clock).update(1, "А", None, 1, [part(REMAINDER, 1)])
    with pytest.raises(DomainRuleError):
        replenishments(db, clock).delete(1)


# Видалення --------------------------------------------------------------------------------


def test_delete_returns_funds_to_every_source(db, clock, trip):
    service = replenishments(db, clock)
    income = income_source(db, clock, 5_000)
    view = service.create("Відкладаю", None, trip, [part(income, 2_000), part(REMAINDER, 3_000)])
    service.delete(view.replenishment.id)
    assert income_balance(db, clock, income) == Money(5_000)
    assert remainder(db) == Money(10_000) and acc_balance(db, trip) == Money(5_000)
    assert count_replenishments(db) == 0
    assert db.execute("SELECT COUNT(*) FROM replenishment_parts").fetchone()[0] == 0


def test_delete_that_would_restore_archived_income_blocked(db, clock, trip):
    service = replenishments(db, clock)
    income = income_source(db, clock, 1_000)
    view = service.create("Усе", None, trip, [part(income, 1_000)])
    before = state(db, clock, trip)
    with pytest.raises(DomainRuleError):
        service.delete(view.replenishment.id)
    assert state(db, clock, trip) == before


# Атомарність ------------------------------------------------------------------------------


def test_update_rolls_back_when_interrupted_after_replace(db, clock, trip, monkeypatch):
    service = replenishments(db, clock)
    income = income_source(db, clock, 4_000)
    view = service.create("Відкладаю", None, trip, [part(REMAINDER, 2_000)])
    before = state(db, clock, trip)
    original = ReplenishmentRepository.replace

    def replace_then_fail(self, replenishment):
        original(self, replenishment)
        raise Interrupted

    monkeypatch.setattr(ReplenishmentRepository, "replace", replace_then_fail)
    with pytest.raises(Interrupted):
        service.update(view.replenishment.id, "Відкладаю", None, trip, [part(income, 4_000)])
    assert state(db, clock, trip) == before
    assert not IncomeService(db, clock).get(income.income_id).income.archived


def test_update_rolls_back_when_interrupted_during_draw(db, clock, trip, monkeypatch):
    service = replenishments(db, clock)
    view = service.create("Відкладаю", None, trip, [part(REMAINDER, 2_000)])
    before = state(db, clock, trip)

    def fail(self, amount):
        raise Interrupted

    monkeypatch.setattr(SourceLedger, "debit_general_remainder", fail)
    with pytest.raises(Interrupted):
        service.update(view.replenishment.id, "Відкладаю", None, trip, [part(REMAINDER, 3_000)])
    assert state(db, clock, trip) == before


def test_delete_rolls_back_when_interrupted(db, clock, trip, monkeypatch):
    service = replenishments(db, clock)
    view = service.create("Відкладаю", None, trip, [part(REMAINDER, 2_000)])
    before = state(db, clock, trip)
    original = ReplenishmentRepository.delete

    def delete_then_fail(self, replenishment_id):
        original(self, replenishment_id)
        raise Interrupted

    monkeypatch.setattr(ReplenishmentRepository, "delete", delete_then_fail)
    with pytest.raises(Interrupted):
        service.delete(view.replenishment.id)
    assert state(db, clock, trip) == before


def test_edited_options_count_released_amounts(db, clock, trip):
    service = replenishments(db, clock)
    income = income_source(db, clock, 3_000)
    view = service.create("Відкладаю", None, trip, [part(income, 1_000), part(REMAINDER, 500)])
    options = {o.source: o.available for o in service.source_options(editing=view.replenishment)}
    assert options == {income: Money(3_000), REMAINDER: Money(10_000)}
