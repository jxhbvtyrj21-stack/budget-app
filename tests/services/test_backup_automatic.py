"""Автоматичні копії: одна на календарний період, окремі пули, ротація 7/4/12 (DS-5)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from budget.domain.calendar import FixedClock
from budget.errors import DatabaseCorruptedError, StorageError
from budget.services.backup import BackupKind, BackupService, parse_backup_name
from budget.services.startup import prepare_database
from budget.storage import backup as backup_module

# 6 жовтня 2026 (вівторок), 12:00 за Києвом.
START = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)


@pytest.fixture
def clock():
    return FixedClock(START)


@pytest.fixture
def setup(tmp_path, clock):
    backups_dir = tmp_path / "data" / "backups"
    connection = prepare_database(tmp_path / "data" / "budget.db", backups_dir, clock)
    yield BackupService(connection, backups_dir, clock), backups_dir
    connection.close()


def kinds(backups_dir: Path, kind: BackupKind) -> list[str]:
    infos = [parse_backup_name(p) for p in sorted(backups_dir.iterdir())]
    return [i.path.name for i in infos if i is not None and i.kind is kind]


def at(clock, **delta):
    clock.set(START + timedelta(**delta))


# Щоденні ----------------------------------------------------------------------------------


def test_first_run_of_day_creates_daily(setup):
    service, backups_dir = setup
    created = service.run_automatic()
    assert "budget-20261006-120000-daily.db" in [p.name for p in created]
    assert kinds(backups_dir, BackupKind.DAILY) == ["budget-20261006-120000-daily.db"]


def test_second_run_same_day_creates_no_duplicate(setup, clock):
    service, backups_dir = setup
    service.run_automatic()
    at(clock, hours=11)  # 23:00 того самого дня за Києвом
    service.run_automatic()
    assert len(kinds(backups_dir, BackupKind.DAILY)) == 1


def test_next_day_creates_new_daily(setup, clock):
    service, backups_dir = setup
    service.run_automatic()
    at(clock, hours=13)  # 01:00 наступного дня за Києвом
    service.run_automatic()
    assert kinds(backups_dir, BackupKind.DAILY) == [
        "budget-20261006-120000-daily.db",
        "budget-20261007-010000-daily.db",
    ]


def test_existing_backup_of_day_is_respected(setup):
    service, backups_dir = setup
    service.create_backup(BackupKind.DAILY)
    created = service.run_automatic()
    assert all(parse_backup_name(p).kind is not BackupKind.DAILY for p in created)
    assert len(kinds(backups_dir, BackupKind.DAILY)) == 1


def test_corrupted_source_creates_nothing_and_keeps_backups(setup, clock, monkeypatch):
    service, backups_dir = setup
    service.run_automatic()
    before = sorted(p.name for p in backups_dir.iterdir())
    at(clock, days=1)
    monkeypatch.setattr(backup_module, "integrity_check", lambda connection: False)
    with pytest.raises(DatabaseCorruptedError):
        service.run_automatic()
    assert sorted(p.name for p in backups_dir.iterdir()) == before


def test_invalid_generated_backup_is_removed_and_pool_untouched(setup, clock, monkeypatch):
    service, backups_dir = setup
    for day in range(7):
        at(clock, days=day)
        service.run_automatic()
    before = kinds(backups_dir, BackupKind.DAILY)
    assert len(before) == 7
    calls = iter([True, False])  # вихідна база справна, нова копія — ні
    monkeypatch.setattr(backup_module, "integrity_check", lambda connection: next(calls))
    at(clock, days=7)
    with pytest.raises(StorageError):
        service.run_automatic()
    # Невдалу копію видалено, а ротація не відбулася: старі копії на місці.
    assert kinds(backups_dir, BackupKind.DAILY) == before


def test_daily_rotation_keeps_seven(setup, clock):
    service, backups_dir = setup
    for day in range(8):
        at(clock, days=day)
        service.run_automatic()
    daily = kinds(backups_dir, BackupKind.DAILY)
    assert len(daily) == 7
    assert "budget-20261006-120000-daily.db" not in daily  # найстаріша видалена
    assert daily[-1] == "budget-20261013-120000-daily.db"


# Щотижневі --------------------------------------------------------------------------------


def test_first_launch_of_week_creates_separate_weekly(setup):
    service, backups_dir = setup
    service.run_automatic()
    assert kinds(backups_dir, BackupKind.WEEKLY) == ["budget-20261006-120000-weekly.db"]
    # Окремий файл, а не позначка щоденної копії.
    assert kinds(backups_dir, BackupKind.DAILY) == ["budget-20261006-120000-daily.db"]


def test_repeated_launch_same_week_no_duplicate(setup, clock):
    service, backups_dir = setup
    service.run_automatic()  # вівторок
    at(clock, days=5, hours=11)  # неділя 11 жовтня, 23:00 — той самий тиждень ISO
    service.run_automatic()
    assert len(kinds(backups_dir, BackupKind.WEEKLY)) == 1
    assert len(kinds(backups_dir, BackupKind.DAILY)) == 2


def test_calendar_week_not_rolling_seven_days(setup, clock):
    service, backups_dir = setup
    service.run_automatic()  # вівторок, 6 жовтня
    at(clock, days=5, hours=13)  # понеділок 12 жовтня, 01:00 — новий тиждень ISO
    service.run_automatic()
    assert kinds(backups_dir, BackupKind.WEEKLY) == [
        "budget-20261006-120000-weekly.db",
        "budget-20261012-010000-weekly.db",
    ]


def test_weekly_rotation_keeps_four_and_spares_other_pools(setup, clock):
    service, backups_dir = setup
    for week in range(5):
        at(clock, weeks=week)
        service.run_automatic()
    weekly = kinds(backups_dir, BackupKind.WEEKLY)
    assert len(weekly) == 4 and "budget-20261006-120000-weekly.db" not in weekly
    assert len(kinds(backups_dir, BackupKind.DAILY)) == 5  # щоденні не зачеплено


# Щомісячні --------------------------------------------------------------------------------


def months_later(count: int) -> datetime:
    year, month = divmod(10 - 1 + count, 12)
    return datetime(2026 + year, month + 1, 6, 9, 0, tzinfo=UTC)


def test_first_launch_of_month_creates_separate_monthly(setup):
    service, backups_dir = setup
    service.run_automatic()
    assert kinds(backups_dir, BackupKind.MONTHLY) == ["budget-20261006-120000-monthly.db"]


def test_repeated_launch_same_month_no_duplicate(setup, clock):
    service, backups_dir = setup
    service.run_automatic()
    clock.set(datetime(2026, 10, 31, 20, 59, tzinfo=UTC))  # 31 жовтня, 22:59 за Києвом
    service.run_automatic()
    assert len(kinds(backups_dir, BackupKind.MONTHLY)) == 1


def test_next_month_by_kyiv_time(setup, clock):
    service, backups_dir = setup
    service.run_automatic()
    clock.set(datetime(2026, 10, 31, 22, 30, tzinfo=UTC))  # уже 1 листопада, 00:30 за Києвом
    service.run_automatic()
    assert kinds(backups_dir, BackupKind.MONTHLY) == [
        "budget-20261006-120000-monthly.db",
        "budget-20261101-003000-monthly.db",
    ]


def test_monthly_rotation_keeps_twelve_and_spares_other_pools(setup, clock):
    service, backups_dir = setup
    for month in range(13):
        clock.set(months_later(month))
        service.run_automatic()
    monthly = kinds(backups_dir, BackupKind.MONTHLY)
    assert len(monthly) == 12 and "budget-20261006-120000-monthly.db" not in monthly
    assert len(kinds(backups_dir, BackupKind.DAILY)) == 7
    assert len(kinds(backups_dir, BackupKind.WEEKLY)) == 4
