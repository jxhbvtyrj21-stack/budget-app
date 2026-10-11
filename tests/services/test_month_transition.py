from datetime import UTC, datetime

import pytest

from budget.domain.models import SourceKind, SourceRef
from budget.domain.money import Money
from budget.services.balances import BalanceService
from budget.services.income import IncomeService
from budget.services.month import LongGapChoice, MonthTransitionService, PendingLongGap
from budget.services.setup import InitialAccumulation, InitialSetupService, SetupDraft
from budget.services.sources import SourceLedger
from budget.storage.repositories import IncomeRepository
from budget.storage.transaction import transaction

OCTOBER = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
NOVEMBER = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
JANUARY = datetime(2027, 1, 5, 9, 0, tzinfo=UTC)


@pytest.fixture
def setup_done(db, clock):
    clock.set(OCTOBER)
    InitialSetupService(db, clock).complete(
        SetupDraft(
            general_remainder=Money(1_000),
            accumulations=(InitialAccumulation("Подорож", None, Money(5_000)),),
        )
    )
    return db


def spend(db, clock, income_id, amount):
    ledger = SourceLedger(db, clock)
    with transaction(db):
        ledger.require_available(SourceRef(SourceKind.INCOME, income_id=income_id), amount)
        db.execute(
            "INSERT INTO expenses (month, name, amount, source_kind, source_income_id)"
            " VALUES ('2026-10', 'Витрата', ?, 'income', ?)",
            (amount.kopiyky, income_id),
        )
        ledger.settle_income(income_id)


def remainder(db):
    return BalanceService(db).general_remainder()


def table_count(db, table):
    return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_normal_transition_consolidates_positive_balances(setup_done, clock):
    db = setup_done
    incomes = IncomeService(db, clock)
    partly = incomes.create("Замовлення №1", None, Money(10_000)).income
    untouched = incomes.create("Замовлення №2", None, Money(7_000)).income
    spend(db, clock, partly.id, Money(8_000))

    clock.set(NOVEMBER)
    assert MonthTransitionService(db, clock).run_on_startup() is None
    # 1 000 початковий + 2 000 + 7 000; нового доходу чи переказу немає.
    assert remainder(db) == Money(10_000)
    repository = IncomeRepository(db)
    assert repository.get(partly.id).archived and repository.get(untouched.id).archived
    assert table_count(db, "incomes") == 2
    assert table_count(db, "expenses") == 1


def test_zero_balance_income_contributes_nothing(setup_done, clock):
    db = setup_done
    income = IncomeService(db, clock).create("Замовлення", None, Money(4_000)).income
    spend(db, clock, income.id, Money(4_000))
    clock.set(NOVEMBER)
    MonthTransitionService(db, clock).run_on_startup()
    assert remainder(db) == Money(1_000)


def test_transition_is_idempotent_and_keeps_current_month(setup_done, clock):
    db = setup_done
    IncomeService(db, clock).create("Жовтневий", None, Money(3_000))
    clock.set(NOVEMBER)
    current = IncomeService(db, clock).create("Листопадовий", None, Money(500)).income
    service = MonthTransitionService(db, clock)
    service.run_on_startup()
    service.run_on_startup()
    assert remainder(db) == Money(4_000)
    assert not IncomeRepository(db).get(current.id).archived


def test_transition_is_atomic(setup_done, clock, monkeypatch):
    db = setup_done
    incomes = IncomeService(db, clock)
    incomes.create("Перший", None, Money(1_000))
    incomes.create("Другий", None, Money(2_000))
    clock.set(NOVEMBER)
    calls = {"n": 0}
    original = IncomeRepository.set_archived

    def fail_second(self, income_id):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("збій посеред переходу")
        original(self, income_id)

    monkeypatch.setattr(IncomeRepository, "set_archived", fail_second)
    with pytest.raises(RuntimeError):
        MonthTransitionService(db, clock).run_on_startup()
    assert remainder(db) == Money(1_000)
    assert all(not i.archived for i in IncomeRepository(db).list_unarchived())
    assert len(IncomeRepository(db).list_unarchived()) == 2


def test_not_applied_before_setup(db, clock):
    clock.set(NOVEMBER)
    assert MonthTransitionService(db, clock).run_on_startup() is None


def long_gap_state(db, clock):
    incomes = IncomeService(db, clock)
    positive = incomes.create("Замовлення", None, Money(20_000)).income
    zero = incomes.create("Витрачений", None, Money(3_000)).income
    spend(db, clock, positive.id, Money(13_000))
    spend(db, clock, zero.id, Money(3_000))
    clock.set(JANUARY)  # листопад і грудень порожні
    return positive, zero


def test_long_gap_requires_one_decision_for_actual_remainder(setup_done, clock):
    db = setup_done
    positive, _ = long_gap_state(db, clock)
    pending = MonthTransitionService(db, clock).run_on_startup()
    assert pending == PendingLongGap(Money(7_000))
    # Без рішення фінансових змін немає; запит повторюється при наступному запуску.
    assert remainder(db) == Money(1_000)
    assert not IncomeRepository(db).get(positive.id).archived
    assert MonthTransitionService(db, clock).run_on_startup() == PendingLongGap(Money(7_000))


def test_long_gap_transfer(setup_done, clock):
    db = setup_done
    positive, zero = long_gap_state(db, clock)
    service = MonthTransitionService(db, clock)
    service.run_on_startup()
    service.resolve_long_gap(LongGapChoice.TRANSFER)
    assert remainder(db) == Money(8_000)
    assert IncomeRepository(db).get(positive.id).archived
    assert IncomeRepository(db).get(zero.id).archived
    assert service.pending_long_gap() is None


def test_long_gap_remove_keeps_remainder_and_accumulations(setup_done, clock):
    db = setup_done
    positive, _ = long_gap_state(db, clock)
    balances = BalanceService(db)
    before = balances.available_funds()
    service = MonthTransitionService(db, clock)
    service.run_on_startup()
    service.resolve_long_gap(LongGapChoice.REMOVE)
    after = balances.available_funds()
    assert after.general_remainder == before.general_remainder
    assert after.accumulations == before.accumulations == Money(5_000)
    assert after.active_incomes == Money.zero()
    assert IncomeRepository(db).get(positive.id).archived
    assert table_count(db, "expenses") == 2  # «Вилучити» не є витратою
