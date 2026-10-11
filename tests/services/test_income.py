from datetime import UTC, datetime

import pytest

from budget.domain.calendar import CalendarMonth
from budget.domain.models import IncomeStatus, SourceKind, SourceRef
from budget.domain.money import Money
from budget.errors import DomainRuleError, ValidationError
from budget.services.balances import BalanceService
from budget.services.income import IncomeService
from budget.services.sources import InsufficientFundsError, SourceLedger
from budget.storage.transaction import transaction


def spend_from(db, clock, source: SourceRef, amount: Money) -> None:
    """Як зробить майбутній сервіс витрат: перевірка, запис операції, урегулювання — атомарно."""
    ledger = SourceLedger(db, clock)
    with transaction(db):
        ledger.require_available(source, amount)
        db.execute(
            "INSERT INTO expenses (month, name, amount, source_kind, source_income_id)"
            " VALUES ('2026-10', 'Витрата', ?, ?, ?)",
            (amount.kopiyky, source.kind.value, source.income_id),
        )
        if source.kind is SourceKind.INCOME:
            ledger.settle_income(source.income_id)


@pytest.fixture
def incomes(completed_db, clock):
    return IncomeService(completed_db, clock)


def test_create_income_in_current_month(incomes):
    view = incomes.create("Замовлення №1", "Сайт", Money(1_000_000))
    assert view.income.month == CalendarMonth(2026, 10)
    assert view.income.name == "Замовлення №1" and view.income.description == "Сайт"
    assert view.balance == Money(1_000_000)
    assert view.status is IncomeStatus.ACTIVE and not view.income.archived


def test_month_follows_kyiv_calendar_not_user_input(completed_db, clock):
    # 31 жовтня 22:30 UTC — уже 1 листопада в Києві.
    clock.set(datetime(2026, 10, 31, 22, 30, tzinfo=UTC))
    view = IncomeService(completed_db, clock).create("Аванс", None, Money(100))
    assert view.income.month == CalendarMonth(2026, 11)


def test_api_has_no_date_edit_delete_cancel_or_restore(incomes):
    import inspect

    assert list(inspect.signature(incomes.create).parameters) == ["name", "description", "amount"]
    forbidden = (
        "update",
        "edit",
        "delete",
        "remove",
        "cancel",
        "restore",
        "set_month",
        "set_amount",
        "rename",
        "set_status",
        "archive",
        "unarchive",
    )
    assert not [name for name in dir(incomes) if any(f in name for f in forbidden)]


def test_name_required_and_amount_positive(incomes):
    with pytest.raises(ValidationError):
        incomes.create("  ", None, Money(100))
    with pytest.raises(ValidationError):
        incomes.create("Дохід", None, Money.zero())


def test_creation_blocked_before_setup(db, clock):
    with pytest.raises(DomainRuleError):
        IncomeService(db, clock).create("Дохід", None, Money(100))
    assert db.execute("SELECT COUNT(*) FROM incomes").fetchone()[0] == 0


def test_mistake_is_fixed_by_new_income(incomes):
    wrong = incomes.create("Замовлення", None, Money(100_000))
    right = incomes.create("Замовлення (виправлено)", None, Money(150_000))
    assert incomes.get(wrong.income.id).income.amount == Money(100_000)
    assert len(incomes.list_for_month(CalendarMonth(2026, 10))) == 2
    assert right.balance == Money(150_000)


def test_balance_decreases_and_zero_balance_archives(completed_db, clock, incomes):
    income = incomes.create("Замовлення", None, Money(1_000)).income
    source = SourceRef(SourceKind.INCOME, income_id=income.id)
    spend_from(completed_db, clock, source, Money(400))
    assert incomes.get(income.id).balance == Money(600)
    assert not incomes.get(income.id).income.archived
    spend_from(completed_db, clock, source, Money(600))
    view = incomes.get(income.id)
    assert view.balance == Money.zero()
    assert view.status is IncomeStatus.COMPLETED
    assert view.income.archived
    assert incomes.current_sources() == []


def test_archived_income_cannot_be_source(completed_db, clock, incomes):
    income = incomes.create("Замовлення", None, Money(500)).income
    source = SourceRef(SourceKind.INCOME, income_id=income.id)
    spend_from(completed_db, clock, source, Money(500))
    with pytest.raises(DomainRuleError):
        spend_from(completed_db, clock, source, Money(1))


def test_negative_balance_blocked_and_nothing_written(completed_db, clock, incomes):
    income = incomes.create("Замовлення", None, Money(500)).income
    source = SourceRef(SourceKind.INCOME, income_id=income.id)
    with pytest.raises(InsufficientFundsError) as error:
        spend_from(completed_db, clock, source, Money(501))
    assert (error.value.available, error.value.required) == (Money(500), Money(501))
    assert completed_db.execute("SELECT COUNT(*) FROM expenses").fetchone()[0] == 0
    assert incomes.get(income.id).balance == Money(500)


def test_no_automatic_switching_or_splitting(completed_db, clock, incomes):
    """Нестача в обраному доході не добирається з іншого доходу чи залишку (ADR 0003)."""
    first = incomes.create("Перший", None, Money(300)).income
    second = incomes.create("Другий", None, Money(10_000)).income
    with pytest.raises(InsufficientFundsError):
        spend_from(
            completed_db, clock, SourceRef(SourceKind.INCOME, income_id=first.id), Money(500)
        )
    assert incomes.get(first.id).balance == Money(300)
    assert incomes.get(second.id).balance == Money(10_000)
    assert completed_db.execute("SELECT COUNT(*) FROM expenses").fetchone()[0] == 0


def test_income_of_another_month_is_not_a_current_source(completed_db, clock, incomes):
    income = incomes.create("Вересневий", None, Money(1_000)).income
    clock.set(datetime(2026, 11, 2, 9, 0, tzinfo=UTC))
    with pytest.raises(DomainRuleError):
        SourceLedger(completed_db, clock).require_available(
            SourceRef(SourceKind.INCOME, income_id=income.id), Money(1)
        )


def test_archived_income_is_never_revived(completed_db, clock, incomes):
    """Q168, Q174: зміна, що повернула б кошти архівованому доходу, блокується."""
    income = incomes.create("Замовлення", None, Money(700)).income
    spend_from(completed_db, clock, SourceRef(SourceKind.INCOME, income_id=income.id), Money(700))
    ledger = SourceLedger(completed_db, clock)
    with pytest.raises(DomainRuleError):
        ledger.require_income_not_revived(income.id)
    active = incomes.create("Інший", None, Money(700)).income
    ledger.require_income_not_revived(active.id)  # активний дохід — без обмежень


def test_archived_and_completed_incomes_excluded_from_available(completed_db, clock, incomes):
    spent = incomes.create("Витрачений", None, Money(200)).income
    incomes.create("Активний", None, Money(900))
    spend_from(completed_db, clock, SourceRef(SourceKind.INCOME, income_id=spent.id), Money(200))
    assert BalanceService(completed_db).available_funds().active_incomes == Money(900)
