import pytest

from budget.domain.models import AccumulationStatus, DebtOrigin, SetupStatus
from budget.domain.money import Money
from budget.errors import DomainRuleError, ValidationError
from budget.services.balances import BalanceService
from budget.services.setup import (
    InitialAccumulation,
    InitialDebt,
    InitialSetupService,
    SetupDraft,
    SetupStep,
    require_normal_operation,
)
from budget.storage.repositories import AccumulationRepository, DebtRepository

FINANCIAL_TABLES = (
    "incomes",
    "expenses",
    "replenishments",
    "replenishment_parts",
    "debt_repayments",
)


def full_draft() -> SetupDraft:
    return SetupDraft(
        step=SetupStep.REVIEW,
        general_remainder=Money(1_500_000),
        accumulations=(
            InitialAccumulation("На паркан", "Нова огорожа", Money(5_000_000), Money(5_000_000)),
            InitialAccumulation("Подорож", None, Money.zero()),
        ),
        debts=(InitialDebt("Позика в Олени", None, Money(300_000)),),
    )


def count(db, table):
    return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_wizard_starts_empty(db, clock):
    service = InitialSetupService(db, clock)
    assert service.state().status is SetupStatus.NOT_STARTED
    assert service.draft() == SetupDraft()


def test_draft_persists_and_resumes(db, clock, tmp_path):
    InitialSetupService(db, clock).save_draft(full_draft())
    # Новий екземпляр сервісу, як після повторного запуску застосунку.
    resumed = InitialSetupService(db, clock)
    assert resumed.state().status is SetupStatus.IN_PROGRESS
    assert resumed.draft() == full_draft()


def test_reset_clears_draft_without_financial_records(db, clock):
    service = InitialSetupService(db, clock)
    service.save_draft(full_draft())
    service.reset()
    assert service.state().status is SetupStatus.NOT_STARTED
    assert service.draft() == SetupDraft()
    for table in (*FINANCIAL_TABLES, "accumulations", "debts"):
        assert count(db, table) == 0


def test_normal_operation_blocked_before_completion(db, clock):
    InitialSetupService(db, clock).save_draft(full_draft())
    with pytest.raises(DomainRuleError):
        require_normal_operation(db)


def test_successful_completion_creates_initial_state(db, clock):
    service = InitialSetupService(db, clock)
    service.complete(full_draft())
    state = service.state()
    assert state.status is SetupStatus.COMPLETED
    assert str(state.completed_month) == "2026-10"
    assert state.draft is None
    require_normal_operation(db)

    balances = BalanceService(db)
    # Початковий залишок: лише загальний нерозподілений залишок, без доходу.
    assert balances.general_remainder() == Money(1_500_000)
    for table in FINANCIAL_TABLES:
        assert count(db, table) == 0

    fence, trip = AccumulationRepository(db).list_all()
    # Статус «Активне» навіть коли початковий баланс дорівнює цільовій сумі (Q165).
    assert fence.status is AccumulationStatus.ACTIVE and trip.status is AccumulationStatus.ACTIVE
    assert balances.accumulation_balance(fence) == Money(5_000_000)
    assert balances.accumulation_balance(trip) == Money.zero()
    assert trip.target is None and not fence.archived

    (debt,) = DebtRepository(db).list_all()
    # Початковий борг — не отримання позикових коштів і без місяця.
    assert debt.origin is DebtOrigin.INITIAL and debt.month is None
    assert debt.amount == Money(300_000)

    funds = balances.available_funds()
    assert funds.total == Money(1_500_000 + 5_000_000)  # борг не віднімається


def test_completion_is_atomic(db, clock, monkeypatch):
    service = InitialSetupService(db, clock)
    service.save_draft(full_draft())

    def fail(self, debt):
        raise RuntimeError("збій під час запису боргу")

    monkeypatch.setattr(DebtRepository, "insert", fail)
    with pytest.raises(RuntimeError):
        service.complete(full_draft())
    assert service.state().status is SetupStatus.IN_PROGRESS
    assert service.draft() == full_draft()
    assert count(db, "accumulations") == 0
    assert BalanceService(db).general_remainder() == Money.zero()


def test_completion_cannot_be_repeated(db, clock):
    service = InitialSetupService(db, clock)
    service.complete(full_draft())
    with pytest.raises(DomainRuleError):
        service.complete(full_draft())
    with pytest.raises(DomainRuleError):
        service.save_draft(SetupDraft())
    with pytest.raises(DomainRuleError):
        service.reset()
    assert count(db, "accumulations") == 2
    assert count(db, "debts") == 1
    assert BalanceService(db).general_remainder() == Money(1_500_000)


def test_completion_with_empty_draft(db, clock):
    InitialSetupService(db, clock).complete(SetupDraft())
    assert BalanceService(db).available_funds().total == Money.zero()


def test_initial_values_are_validated():
    with pytest.raises(ValidationError):
        InitialAccumulation(" ", None, Money.zero())
    with pytest.raises(ValidationError):
        InitialAccumulation("Подорож", None, Money(-1))
    with pytest.raises(ValidationError):
        InitialDebt("Позика", None, Money.zero())
    with pytest.raises(ValidationError):
        SetupDraft(general_remainder=Money(-1))
