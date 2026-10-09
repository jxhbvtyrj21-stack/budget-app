"""Види й назви резервних копій: форматування, розбір, порядок, нерозпізнані файли."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from budget.domain.calendar import FixedClock
from budget.services.backup import (
    BackupKind,
    RecoveryService,
    backup_file_name,
    file_timestamp,
    newest_first,
    parse_backup_name,
)

MOMENT = datetime(2026, 10, 6, 12, 0, 5)


@pytest.mark.parametrize(
    ("kind", "version", "name"),
    [
        (BackupKind.DAILY, None, "budget-20261006-120005-daily.db"),
        (BackupKind.WEEKLY, None, "budget-20261006-120005-weekly.db"),
        (BackupKind.MONTHLY, None, "budget-20261006-120005-monthly.db"),
        (BackupKind.ON_DEMAND, None, "budget-20261006-120005-on-demand.db"),
        (BackupKind.BEFORE_RESTORE, None, "budget-20261006-120005-before-restore.db"),
        (BackupKind.BEFORE_MIGRATION, 2, "budget-20261006-120005-before-migration-2.db"),
    ],
)
def test_format_and_parse_round_trip(kind, version, name):
    assert backup_file_name(kind, MOMENT, version) == name
    info = parse_backup_name(Path("backups") / name)
    assert (info.kind, info.created, info.migration_version) == (kind, MOMENT, version)


def test_migration_version_only_for_migration_backups():
    with pytest.raises(ValueError):
        backup_file_name(BackupKind.DAILY, MOMENT, 2)
    with pytest.raises(ValueError):
        backup_file_name(BackupKind.BEFORE_MIGRATION, MOMENT)


@pytest.mark.parametrize(
    "name",
    [
        "budget-20261006-120005-manual.db",  # вільний текст — не вид копії
        "budget-20261006-120005-daily.db.tmp",
        "budget-20261006-120005-dailyx.db",
        "budget-20261306-120005-daily.db",  # неіснуюча дата
        "budget-20261006-120005-before-migration-0.db",
        "budget-20261006-120005-before-migration.db",
        "copy-20261006-120005-daily.db",
        "budget.db",
        "budget.db.corrupted-20261006-120005",
    ],
)
def test_unknown_names_are_not_classified(name):
    assert parse_backup_name(Path(name)) is None


def test_kinds_never_overlap():
    """Назва одного виду ніколи не розпізнається як інший (окремі пули ротації)."""
    for kind in BackupKind:
        version = 1 if kind is BackupKind.BEFORE_MIGRATION else None
        parsed = parse_backup_name(Path(backup_file_name(kind, MOMENT, version)))
        assert parsed.kind is kind


def test_newest_first_with_equal_timestamps_is_stable():
    names = [
        "budget-20261005-090000-daily.db",
        "budget-20261006-120005-weekly.db",
        "budget-20261006-120005-daily.db",
        "budget-20261006-120005-monthly.db",
    ]
    ordered = newest_first([parse_backup_name(Path(n)) for n in names])
    assert [b.path.name for b in ordered] == [
        "budget-20261006-120005-weekly.db",
        "budget-20261006-120005-monthly.db",
        "budget-20261006-120005-daily.db",
        "budget-20261005-090000-daily.db",
    ]


@pytest.mark.parametrize(
    ("moment", "stamp"),
    [
        pytest.param(datetime(2026, 10, 6, 9, 0, tzinfo=UTC), "20261006-120000", id="EEST"),
        pytest.param(datetime(2026, 1, 15, 9, 0, tzinfo=UTC), "20260115-110000", id="EET"),
    ],
)
def test_file_timestamp_is_kyiv_local_time_in_backup_name_format(moment, stamp):
    """Мітка для назв відкладених файлів: локальний час Europe/Kyiv (літній і зимовий),
    той самий формат, що й у назвах копій."""
    clock = FixedClock(moment)
    assert file_timestamp(clock) == stamp
    assert backup_file_name(BackupKind.DAILY, clock.now()) == f"budget-{stamp}-daily.db"


def test_quarantined_database_name_uses_the_file_timestamp(tmp_path):
    database = tmp_path / "budget.db"
    database.write_bytes(b"corrupted" * 100)
    clock = FixedClock(datetime(2026, 10, 6, 9, 0, tzinfo=UTC))
    quarantined = RecoveryService(database, tmp_path / "backups", clock).quarantine_corrupted()
    assert quarantined == tmp_path / "budget.db.corrupted-20261006-120000"
    assert quarantined.read_bytes() == b"corrupted" * 100 and not database.exists()
