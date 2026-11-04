"""Ротація копій: окремі пули 7/4/12, захищені види й невідомі файли не видаляються (DS-5)."""

import logging
import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from budget.domain.calendar import FixedClock
from budget.errors import StorageError
from budget.services.backup import (
    BackupKind,
    BackupService,
    backup_file_name,
    parse_backup_name,
)
from budget.services.startup import prepare_database
from budget.storage.backup import PARTIAL_SUFFIX
from budget.storage.recovery import verify_backup

START = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)  # 12:00 за Києвом

PROTECTED = (
    "budget-20200101-000000-before-migration-1.db",
    "budget-20200101-000000-on-demand.db",
    "budget-20200101-000000-before-restore.db",
)
UNKNOWN = (
    "budget-20200101-000000-manual.db",  # стара довільна причина
    "budget-20200101-000000-daily.db.tmp",
    "budget-20200101-000000-daily.db-wal",
    "budget-20201301-000000-daily.db",  # неможлива дата
    "Budget-20200102-000000-daily.db",
    "budget-20200103-000000-Daily.db",
    "budget-20200101-000000-daily (1).db",
    "copy-budget-20200101-000000-daily.db",
    "budget.db",
    "notes.txt",
)


@pytest.fixture
def clock():
    return FixedClock(START)


@pytest.fixture
def setup(tmp_path, clock):
    backups_dir = tmp_path / "data" / "backups"
    connection = prepare_database(tmp_path / "data" / "budget.db", backups_dir, clock)
    backups_dir.mkdir(parents=True, exist_ok=True)
    yield BackupService(connection, backups_dir, clock), backups_dir
    connection.close()


def seed(backups_dir: Path, names) -> None:
    for name in names:
        (backups_dir / name).write_bytes(b"seed")


def names(backups_dir: Path, kind: BackupKind | None = None) -> list[str]:
    result = []
    for path in sorted(backups_dir.iterdir()):
        info = parse_backup_name(path)
        if kind is None or (info is not None and info.kind is kind):
            result.append(path.name)
    return result


def run_days(service, clock, days: int, step: int = 1) -> None:
    for day in range(0, days, step):
        clock.set(START + timedelta(days=day))
        service.run_automatic()


def old_pool(kind: BackupKind, count: int) -> list[str]:
    """Копії виду ``kind`` у минулому, по одній на місяць 2020 року."""
    return [backup_file_name(kind, datetime(2020, month, 1)) for month in range(1, count + 1)]


def test_protected_kinds_and_unknown_files_survive_many_rotations(setup, clock):
    service, backups_dir = setup
    seed(backups_dir, PROTECTED + UNKNOWN)
    run_days(service, clock, 400, step=5)  # понад рік: кожен пул багато разів ротується
    remaining = set(names(backups_dir))
    assert set(PROTECTED) <= remaining
    assert set(UNKNOWN) <= remaining
    assert len(names(backups_dir, BackupKind.DAILY)) == 7
    assert len(names(backups_dir, BackupKind.WEEKLY)) == 4
    assert len(names(backups_dir, BackupKind.MONTHLY)) == 12


def test_seed_names_are_distinct_on_case_insensitive_file_systems():
    """На Windows назви, що різняться лише регістром, — той самий файл."""
    seeded = PROTECTED + UNKNOWN
    assert len({name.casefold() for name in seeded}) == len(seeded)


def test_unknown_names_are_not_classified():
    for name in UNKNOWN:
        assert parse_backup_name(Path(name)) is None, name


@pytest.mark.parametrize(
    ("rotated", "spared"),
    [
        (BackupKind.DAILY, (BackupKind.WEEKLY, BackupKind.MONTHLY)),
        (BackupKind.WEEKLY, (BackupKind.DAILY, BackupKind.MONTHLY)),
        (BackupKind.MONTHLY, (BackupKind.DAILY, BackupKind.WEEKLY)),
    ],
)
def test_rotation_of_one_pool_spares_other_pools(setup, rotated, spared):
    """Пул, переповнений старими копіями, ротується; інші пули — понад їхній ліміт — ні."""
    service, backups_dir = setup
    seed(backups_dir, old_pool(rotated, 12))
    others = {kind: old_pool(kind, 12) for kind in spared}
    for pool in others.values():
        seed(backups_dir, pool)
    policy = {BackupKind.DAILY: 7, BackupKind.WEEKLY: 4, BackupKind.MONTHLY: 12}
    # Інші пули вже мають копію поточного періоду, тож створюється лише копія ``rotated``.
    for kind in spared:
        seed(backups_dir, [backup_file_name(kind, datetime(2026, 10, 6, 11, 0))])
    created = service.run_automatic()
    assert [parse_backup_name(p).kind for p in created] == [rotated]
    assert len(names(backups_dir, rotated)) == policy[rotated]
    for kind, pool in others.items():
        assert set(pool) <= set(names(backups_dir, kind))


def test_rotation_removes_oldest_of_pool(setup):
    service, backups_dir = setup
    pool = old_pool(BackupKind.DAILY, 9)
    seed(backups_dir, pool)
    service.run_automatic()
    daily = names(backups_dir, BackupKind.DAILY)
    # 9 старих + 1 нова → лишаються 7 найновіших: нова й 6 останніх старих.
    assert daily == sorted([*pool[-6:], "budget-20261006-120000-daily.db"])


def test_same_second_backups_of_all_kinds_have_distinct_files(setup):
    service, _ = setup
    created = service.run_automatic()
    on_demand = service.create_backup(BackupKind.ON_DEMAND)
    before_restore = service.create_backup(BackupKind.BEFORE_RESTORE)
    paths = {*created, on_demand, before_restore}
    assert len(paths) == 5
    assert {p.name for p in paths} == {
        "budget-20261006-120000-daily.db",
        "budget-20261006-120000-weekly.db",
        "budget-20261006-120000-monthly.db",
        "budget-20261006-120000-on-demand.db",
        "budget-20261006-120000-before-restore.db",
    }


def test_filename_collision_fails_without_touching_existing_file(setup):
    service, backups_dir = setup
    first = service.create_backup(BackupKind.ON_DEMAND)
    content = first.read_bytes()
    with pytest.raises(StorageError):
        service.create_backup(BackupKind.ON_DEMAND)  # та сама секунда — та сама назва
    assert first.read_bytes() == content
    assert names(backups_dir) == [first.name]


def test_equal_timestamps_in_pool_are_ordered_stably(setup):
    """Однаковий час у різних видів не змішує пули; ротація рахує лише свій вид."""
    service, backups_dir = setup
    stamp = datetime(2020, 1, 1)
    seed(
        backups_dir,
        [
            backup_file_name(kind, stamp)
            for kind in BackupKind
            if kind is not BackupKind.BEFORE_MIGRATION
        ],
    )
    seed(backups_dir, [backup_file_name(BackupKind.BEFORE_MIGRATION, stamp, 3)])
    service.run_automatic()
    assert len(names(backups_dir, BackupKind.DAILY)) == 2
    assert len(names(backups_dir, BackupKind.WEEKLY)) == 2
    assert len(names(backups_dir, BackupKind.MONTHLY)) == 2
    assert len(names(backups_dir)) == 9


def test_failed_deletion_is_logged_and_rotation_continues(setup, monkeypatch, caplog):
    service, backups_dir = setup
    pool = old_pool(BackupKind.DAILY, 9)
    seed(backups_dir, pool)
    locked = backups_dir / pool[0]
    original_unlink = Path.unlink

    def unlink(self, missing_ok=False):
        if self == locked:
            raise PermissionError("файл зайнятий іншим процесом")
        return original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    with caplog.at_level(logging.WARNING):
        service.run_automatic()
    daily = names(backups_dir, BackupKind.DAILY)
    assert locked.name in daily and pool[1] not in daily and pool[2] not in daily
    assert len(daily) == 8  # зайнятий файл лишився до наступної ротації
    assert "Не вдалося видалити" in caplog.text


# Аварія під час автоматичної копії (B1) ----------------------------------------------------

CRASH_DURING_AUTOMATIC = textwrap.dedent(
    """
    import os, sys
    from datetime import datetime
    from pathlib import Path
    import budget.storage.backup as backup
    from budget.domain.calendar import FixedClock
    from budget.services.backup import BackupService
    from budget.storage.database import open_database

    database, backups_dir, moment = sys.argv[1:4]
    calls = []
    original = backup.integrity_check

    def check(connection):  # аварія під час перевірки вже записаної щоденної копії
        calls.append(connection)
        if len(calls) == 2:
            os._exit(9)
        return original(connection)

    backup.integrity_check = check
    connection = open_database(Path(database))
    clock = FixedClock(datetime.fromisoformat(moment))
    BackupService(connection, Path(backups_dir), clock).run_automatic()
    """
)


def test_crashed_automatic_copy_neither_blocks_the_period_nor_takes_a_rotation_slot(
    setup, clock, tmp_path
):
    service, backups_dir = setup
    run_days(service, clock, 7)  # 7 справних щоденних копій
    day8 = START + timedelta(days=7)
    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            CRASH_DURING_AUTOMATIC,
            str(tmp_path / "data" / "budget.db"),
            str(backups_dir),
            day8.isoformat(),
        ]
    )
    assert crashed.returncode == 9
    partials = [name for name in names(backups_dir) if PARTIAL_SUFFIX in name]
    assert partials  # аварія лишила лише тимчасовий файл
    assert not [n for n in names(backups_dir, BackupKind.DAILY) if n.startswith("budget-20261013")]

    clock.set(day8)
    created = [path.name for path in service.run_automatic()]
    assert backup_file_name(BackupKind.DAILY, clock.now().replace(tzinfo=None)) in created
    for day in (8, 9):
        clock.set(START + timedelta(days=day))
        service.run_automatic()

    daily = names(backups_dir, BackupKind.DAILY)
    assert len(daily) == 7  # правило ротації 7, і всі місця — справні копії
    assert all(verify_backup(backups_dir / name) for name in daily)
    assert daily[-1].startswith("budget-20261015")
