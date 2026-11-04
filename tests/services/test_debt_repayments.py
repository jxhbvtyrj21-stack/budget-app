"""Погашення боргу: створення, зміна, видалення (ADR 0018 пп. 3–5; Q168, Q189, Q191).

Окремо — правило «погашений борг не відкривається повторно».
"""

from datetime import UTC, datetime

import pytest

from budget.domain.models import (
    AccumulationStatus,
    DebtStatus,
    ReplenishmentPart,
    SourceKind,
    SourceRef,
)
from budget.domain.money import Money
from budget.errors import DomainRuleError, ValidationError
from budget.services.accumulation import AccumulationService
from budget.services.debt import DebtOverpaymentError, DebtReopenError
from budget.services.expense import ExpenseService
from budget.services.income import IncomeService
from budget.services.replenishment import ReplenishmentService
from budget.services.sources import InsufficientFundsError, SourceLedger
from budget.storage.repositories import DebtRepository
from tests.services.debt_fixtures import complete_setup_with_debt, debts, remainder

REMAINDER = SourceRef(SourceKind.GENERAL_REMAINDER)


class Interrupted(Exception):
    pass


@pytest.fixture
def debt(db, clock):
    """Початковий борг «Позика в Олени» 3 000; загальний нерозподілений залишок 10 000."""
    return complete_setup_with_debt(db, clock)


def income(db, clock, amount, name="Аванс"):
    created = IncomeService(db, clock).create(name, None, Money(amount)).income
    return SourceRef(SourceKind.INCOME, income_id=created.id)


def accumulation(db, clock, name="Подорож"):
    acc_id = AccumulationService(db, clock).create(name, None, None).accumulation.id
    return SourceRef(SourceKind.ACCUMULATION, accumulation_id=acc_id)


def remaining(db, clock, debt_id):
    return debts(db, clock).get(debt_id).remaining


def state(db):
    return (
        db.execute("SELECT * FROM general_remainder").fetchall(),
        db.execute("SELECT * FROM debt_repayments ORDER BY id").fetchall(),
        db.execute("SELECT * FROM debts ORDER BY id").fetchall(),
        db.execute("SELECT id, archived FROM incomes ORDER BY id").fetchall(),
        db.execute("SELECT COUNT(*) FROM expenses").fetchall(),
    )


# Створення --------------------------------------------------------------------------------


def test_partial_and_full_repayment_from_remainder(db, clock, debt):
    service = debts(db, clock)
    view = service.repay(debt, Money(1_000), REMAINDER, "Перша частина")
    assert view.source_name == "Загальний нерозподілений залишок"
    assert view.debt_name == "Позика в Олени"
    assert remaining(db, clock, debt) == Money(2_000) and remainder(db) == Money(9_000)
    service.repay(debt, Money(2_000), REMAINDER)
    assert service.get(debt).status is DebtStatus.PAID
    assert remainder(db) == Money(7_000)
    # Погашення не є звичайною витратою.
    assert db.execute("SELECT COUNT(*) FROM expenses").fetchone()[0] == 0


def test_repayment_cannot_exceed_debt_remaining(db, clock, debt):
    service = debts(db, clock)
    with pytest.raises(DebtOverpaymentError) as error:
        service.repay(debt, Money(3_001), REMAINDER)
    assert (error.value.remaining, error.value.required) == (Money(3_000), Money(3_001))
    assert remainder(db) == Money(10_000)


def test_repayment_cannot_exceed_source_balance(db, clock, debt):
    source = income(db, clock, 500)
    with pytest.raises(InsufficientFundsError) as error:
        debts(db, clock).repay(debt, Money(600), source)
    assert (error.value.available, error.value.required) == (Money(500), Money(600))
    assert remaining(db, clock, debt) == Money(3_000)


def test_repayment_from_income_archives_drained_income(db, clock, debt):
    source = income(db, clock, 1_200)
    debts(db, clock).repay(debt, Money(1_200), source)
    view = IncomeService(db, clock).get(source.income_id)
    assert view.balance == Money.zero() and view.income.archived
    with pytest.raises(DomainRuleError):
        debts(db, clock).repay(debt, Money(1), source)


def test_repayment_from_accumulation_keeps_status(db, clock, debt):
    source = accumulation(db, clock)
    ReplenishmentService(db, clock).create(
        "Відкладаю", None, source.accumulation_id, [ReplenishmentPart(REMAINDER, Money(2_000))]
    )
    debts(db, clock).repay(debt, Money(1_500), source)
    view = AccumulationService(db, clock).get(source.accumulation_id)
    assert view.balance == Money(500) and view.accumulation.status is AccumulationStatus.ACTIVE


def test_archived_accumulation_is_not_a_repayment_source(db, clock, debt):
    source = accumulation(db, clock, "Старе")
    accumulations = AccumulationService(db, clock)
    accumulations.change_status(source.accumulation_id, AccumulationStatus.CLOSED)
    accumulations.archive(source.accumulation_id)
    with pytest.raises(DomainRuleError):
        debts(db, clock).repay(debt, Money(1), source)
    assert all(o.source != source for o in debts(db, clock).source_options())


def test_source_options_never_include_debts(db, clock, debt):
    source = income(db, clock, 700)
    acc = accumulation(db, clock)
    options = debts(db, clock).source_options()
    assert [o.source for o in options] == [source, REMAINDER, acc]


def test_repay_validation_and_q190(db, clock):
    with pytest.raises(DomainRuleError):
        debts(db, clock).repay(1, Money(1), REMAINDER)


def test_repay_rejects_non_positive_amount(db, clock, debt):
    with pytest.raises(ValidationError):
        debts(db, clock).repay(debt, Money.zero(), REMAINDER)


# Зміна ------------------------------------------------------------------------------------


def test_update_amount_source_and_description(db, clock, debt):
    service = debts(db, clock)
    source = income(db, clock, 2_000)
    view = service.repay(debt, Money(1_000), REMAINDER)
    rep_id = view.repayment.id
    service.update_repayment(rep_id, Money(1_500), REMAINDER, None)
    assert remainder(db) == Money(8_500) and remaining(db, clock, debt) == Money(1_500)
    service.update_repayment(rep_id, Money(1_500), source, None)
    assert remainder(db) == Money(10_000)
    assert IncomeService(db, clock).get(source.income_id).balance == Money(500)
    updated = service.update_repayment(rep_id, Money(1_500), source, "Частина")
    assert updated.repayment.description == "Частина" and updated.repayment.id == rep_id


def test_update_counts_old_amount_for_same_source(db, clock, debt):
    service = debts(db, clock)
    ExpenseService(db, clock).create("Усе інше", None, Money(8_000), REMAINDER)
    view = service.repay(debt, Money(2_000), REMAINDER)
    assert remainder(db) == Money.zero()
    service.update_repayment(view.repayment.id, Money(1_500), REMAINDER, None)
    assert remainder(db) == Money(500)


def test_update_rejects_overpayment_and_insufficient_source_atomically(db, clock, debt):
    service = debts(db, clock)
    source = income(db, clock, 400)
    view = service.repay(debt, Money(1_000), REMAINDER)
    before = state(db)
    with pytest.raises(DebtOverpaymentError) as error:
        service.update_repayment(view.repayment.id, Money(3_001), REMAINDER, None)
    assert error.value.remaining == Money(3_000)
    with pytest.raises(InsufficientFundsError):
        service.update_repayment(view.repayment.id, Money(1_000), source, None)
    assert state(db) == before


def test_q168_repayment_that_archived_income(db, clock, debt):
    service = debts(db, clock)
    source = income(db, clock, 1_000)
    view = service.repay(debt, Money(1_000), source)
    assert service.repayment_lock_reason(view.repayment)
    before = state(db)
    for amount, new_source in ((Money(500), source), (Money(1_000), REMAINDER)):
        with pytest.raises(DomainRuleError) as error:
            service.update_repayment(view.repayment.id, amount, new_source, None)
        assert "архівовано" in error.value.user_message
    with pytest.raises(DomainRuleError):
        service.delete_repayment(view.repayment.id)
    assert state(db) == before
    # Опис змінити можна: кошти не повертаються.
    service.update_repayment(view.repayment.id, Money(1_000), source, "Опис")
    assert IncomeService(db, clock).get(source.income_id).income.archived


def test_q189_q191_repayment_from_archived_accumulation(db, clock, debt):
    service = debts(db, clock)
    source = accumulation(db, clock)
    ReplenishmentService(db, clock).create(
        "Відкладаю", None, source.accumulation_id, [ReplenishmentPart(REMAINDER, Money(1_000))]
    )
    view = service.repay(debt, Money(1_000), source)
    accumulations = AccumulationService(db, clock)
    accumulations.change_status(source.accumulation_id, AccumulationStatus.CLOSED)
    accumulations.archive(source.accumulation_id)
    assert service.repayment_lock_reason(view.repayment)
    before = state(db)
    with pytest.raises(DomainRuleError):
        service.update_repayment(view.repayment.id, Money(500), source, None)
    with pytest.raises(DomainRuleError):
        service.delete_repayment(view.repayment.id)
    assert state(db) == before
    renamed = service.update_repayment(view.repayment.id, Money(1_000), source, "Опис")
    assert renamed.repayment.description == "Опис"


def test_past_month_repayment_read_only(db, clock, debt):
    service = debts(db, clock)
    view = service.repay(debt, Money(1_000), REMAINDER)
    clock.set(datetime(2026, 11, 2, 9, 0, tzinfo=UTC))
    with pytest.raises(DomainRuleError):
        service.update_repayment(view.repayment.id, Money(1_000), REMAINDER, "Опис")
    with pytest.raises(DomainRuleError):
        service.delete_repayment(view.repayment.id)
    assert service.repayment_lock_reason(view.repayment)
    # Борг із минулого місяця можна погашати в поточному.
    service.repay(debt, Money(500), REMAINDER)
    assert remaining(db, clock, debt) == Money(1_500)


# Видалення --------------------------------------------------------------------------------


def test_delete_returns_funds_to_source(db, clock, debt):
    service = debts(db, clock)
    source = income(db, clock, 2_000)
    view = service.repay(debt, Money(500), source)
    service.delete_repayment(view.repayment.id)
    assert IncomeService(db, clock).get(source.income_id).balance == Money(2_000)
    assert remaining(db, clock, debt) == Money(3_000)
    assert DebtRepository(db).list_repayments_for_debt(debt) == []


# Погашений борг не відкривається повторно ------------------------------------------------


def test_reopen_by_reducing_last_repayment_blocked(db, clock, debt):
    service = debts(db, clock)
    service.repay(debt, Money(1_000), REMAINDER)
    last = service.repay(debt, Money(2_000), REMAINDER)
    assert service.get(debt).status is DebtStatus.PAID
    before = state(db)
    with pytest.raises(DebtReopenError):
        service.update_repayment(last.repayment.id, Money(1_999), REMAINDER, None)
    assert state(db) == before and service.get(debt).status is DebtStatus.PAID


def test_reopen_by_deleting_last_repayment_blocked(db, clock, debt):
    service = debts(db, clock)
    last = service.repay(debt, Money(3_000), REMAINDER)
    assert service.repayment_delete_block_reason(last.repayment)
    before = state(db)
    with pytest.raises(DebtReopenError):
        service.delete_repayment(last.repayment.id)
    assert state(db) == before


def test_paid_debt_source_swap_with_same_amount_allowed(db, clock, debt):
    """Зміна джерела за тієї самої суми залишку боргу не створює — дозволено."""
    service = debts(db, clock)
    source = income(db, clock, 5_000)
    last = service.repay(debt, Money(3_000), REMAINDER)
    service.update_repayment(last.repayment.id, Money(3_000), source, "Інше джерело")
    assert service.get(debt).status is DebtStatus.PAID
    assert remainder(db) == Money(10_000)


# Атомарність ------------------------------------------------------------------------------


def test_repay_rolls_back_when_interrupted(db, clock, debt, monkeypatch):
    original = SourceLedger.debit_general_remainder

    def debit_then_fail(self, amount):
        original(self, amount)
        raise Interrupted

    before = state(db)
    monkeypatch.setattr(SourceLedger, "debit_general_remainder", debit_then_fail)
    with pytest.raises(Interrupted):
        debts(db, clock).repay(debt, Money(1_000), REMAINDER)
    assert state(db) == before


def test_update_rolls_back_when_interrupted_after_replace(db, clock, debt, monkeypatch):
    service = debts(db, clock)
    source = income(db, clock, 1_000)
    view = service.repay(debt, Money(500), REMAINDER)
    before = state(db)
    original = DebtRepository.replace_repayment

    def replace_then_fail(self, repayment):
        original(self, repayment)
        raise Interrupted

    monkeypatch.setattr(DebtRepository, "replace_repayment", replace_then_fail)
    with pytest.raises(Interrupted):
        service.update_repayment(view.repayment.id, Money(1_000), source, None)
    assert state(db) == before


def test_delete_rolls_back_when_interrupted(db, clock, debt, monkeypatch):
    service = debts(db, clock)
    view = service.repay(debt, Money(500), REMAINDER)
    before = state(db)
    original = DebtRepository.delete_repayment

    def delete_then_fail(self, repayment_id):
        original(self, repayment_id)
        raise Interrupted

    monkeypatch.setattr(DebtRepository, "delete_repayment", delete_then_fail)
    with pytest.raises(Interrupted):
        service.delete_repayment(view.repayment.id)
    assert state(db) == before
