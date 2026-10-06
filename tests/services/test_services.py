from datetime import UTC, datetime

import pytest

from budget.domain.calendar import CalendarMonth, FixedClock
from budget.domain.models import SetupStatus
from budget.errors import DatabaseCorruptedError, DomainRuleError
from budget.services.backup import BackupKind, BackupService, RecoveryService
from budget.services.month import MonthService
from budget.services.setup import InitialSetupService, require_normal_operation
from budget.services.startup import prepare_database
from budget.storage.migrations import LATEST_VERSION, schema_version

CLOCK = FixedClock(datetime(2026, 10, 6, 9, 0, tzinfo=UTC))


@pytest.fixture
def paths(tmp_path):
    return tmp_path / "data" / "budget.db", tmp_path / "data" / "backups"


@pytest.fixture
def connection(paths):
    connection = prepare_database(*paths, CLOCK)
    yield connection
    connection.close()


def test_prepare_clean_database(connection, paths):
    assert schema_version(connection) == LATEST_VERSION
    # Чиста база: копія перед міграцією не потрібна.
    assert not paths[1].exists()


def test_existing_database_is_backed_up_before_migration(paths, monkeypatch):
    from budget.storage import migrations

    database_path, backups_dir = paths
    prepare_database(database_path, backups_dir, CLOCK).close()
    extra = (LATEST_VERSION + 1, "CREATE TABLE probe (id INTEGER PRIMARY KEY) STRICT;")
    monkeypatch.setattr(migrations, "MIGRATIONS", (*migrations.MIGRATIONS, extra))
    monkeypatch.setattr(migrations, "LATEST_VERSION", extra[0])
    connection = prepare_database(database_path, backups_dir, CLOCK)
    assert schema_version(connection) == extra[0]
    connection.close()
    backups = sorted(backups_dir.iterdir())
    assert [b.name for b in backups] == [f"budget-20261006-120000-before-migration-{extra[0]}.db"]


def test_corrupted_database_is_reported_and_quarantined(paths):
    database_path, backups_dir = paths
    database_path.parent.mkdir(parents=True)
    database_path.write_bytes(b"garbage" * 1000)
    with pytest.raises(DatabaseCorruptedError):
        prepare_database(database_path, backups_dir, CLOCK)
    quarantined = RecoveryService(database_path, backups_dir, CLOCK).quarantine_corrupted()
    assert quarantined.name.startswith("budget.db.corrupted-")
    assert not database_path.exists()


def test_setup_gates_normal_operation(connection):
    service = InitialSetupService(connection)
    with pytest.raises(DomainRuleError):
        require_normal_operation(connection)
    service.save_draft({"step": 2})
    assert service.state().status is SetupStatus.IN_PROGRESS
    service.reset()
    assert service.state().status is SetupStatus.NOT_STARTED
    assert service.state().draft is None
    # Чернетка майстра не створює фінансових записів (Q176).
    for table in ("incomes", "expenses", "replenishments", "debts", "debt_repayments"):
        assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_month_service_uses_clock():
    months = MonthService(CLOCK)
    assert months.current_month() == CalendarMonth(2026, 10)
    assert months.is_current(CalendarMonth(2026, 10))
    assert not months.is_current(CalendarMonth(2026, 9))


def test_backup_service(connection, paths):
    service = BackupService(connection, paths[1], CLOCK)
    backup = service.create_backup(BackupKind.ON_DEMAND)
    assert backup.name == "budget-20261006-120000-on-demand.db"
    assert service.list_backups() == [backup]
