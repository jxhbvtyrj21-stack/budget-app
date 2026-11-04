"""Перелік копій для відновлення: лише справні, усі види, від найновішої (DS-6; IA 10.1)."""

from datetime import UTC, datetime, timedelta

import pytest

from budget.domain.calendar import FixedClock
from budget.services.backup import (
    BackupKind,
    BackupService,
    find_backups,
    restore_candidates,
)
from budget.services.startup import prepare_database

START = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)  # 12:00 за Києвом


@pytest.fixture
def clock():
    return FixedClock(START)


@pytest.fixture
def setup(tmp_path, clock):
    backups_dir = tmp_path / "Мої дані" / "Budget" / "backups"
    connection = prepare_database(backups_dir.parent / "budget.db", backups_dir, clock)
    yield BackupService(connection, backups_dir, clock), backups_dir
    connection.close()


def test_candidates_include_all_kinds_newest_first(setup, clock):
    service, backups_dir = setup
    service.run_automatic()
    clock.set(START + timedelta(hours=1))
    service.create_backup(BackupKind.ON_DEMAND)
    clock.set(START + timedelta(hours=2))
    service.create_backup(BackupKind.BEFORE_RESTORE)
    clock.set(START + timedelta(hours=3))
    service.create_backup(BackupKind.BEFORE_MIGRATION, 1)
    candidates = restore_candidates(backups_dir)
    assert [c.backup.kind for c in candidates] == [
        BackupKind.BEFORE_MIGRATION,
        BackupKind.BEFORE_RESTORE,
        BackupKind.ON_DEMAND,
        # Однаковий час — стабільно за назвою, у зворотному порядку.
        BackupKind.WEEKLY,
        BackupKind.MONTHLY,
        BackupKind.DAILY,
    ]
    first = candidates[0]
    assert first.backup.created == datetime(2026, 10, 6, 15, 0)
    assert first.backup.migration_version == 1
    assert all(c.size == c.backup.path.stat().st_size > 0 for c in candidates)


def test_invalid_and_unknown_files_are_excluded_but_kept(setup):
    service, backups_dir = setup
    good = service.create_backup(BackupKind.ON_DEMAND)
    broken = backups_dir / "budget-20261005-120000-daily.db"
    broken.write_bytes(b"not a database" * 100)
    truncated = backups_dir / "budget-20261004-120000-weekly.db"
    truncated.write_bytes(good.read_bytes()[:2048])
    unknown = backups_dir / "budget-20261003-120000-manual.db"
    unknown.write_bytes(good.read_bytes())
    listing = sorted(p.name for p in backups_dir.iterdir())
    candidates = restore_candidates(backups_dir)
    assert [c.backup.path for c in candidates] == [good]
    # Побудова переліку нічого не видаляє й не створює.
    assert sorted(p.name for p in backups_dir.iterdir()) == listing


def test_no_backups_directory(tmp_path):
    assert restore_candidates(tmp_path / "missing") == []
    assert find_backups(tmp_path / "missing") == []


def test_directory_with_backup_name_is_ignored(setup):
    service, backups_dir = setup
    (backups_dir / "budget-20261001-120000-daily.db").mkdir(parents=True)
    service.create_backup(BackupKind.ON_DEMAND)
    assert [c.backup.kind for c in restore_candidates(backups_dir)] == [BackupKind.ON_DEMAND]
