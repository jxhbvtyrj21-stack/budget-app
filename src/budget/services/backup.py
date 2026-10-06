"""Резервні копії й відновлення (DS-4, DS-5, DS-6).

Вид і час копії визначаються лише з назви файлу ``budget-YYYYMMDD-HHMMSS-<вид>.db``;
окремої таблиці чи метаданих у базі немає. Час у назві — за Europe/Kyiv (``Clock``).
Сценарій відновлення в інтерфейсі реалізується на наступних етапах.
"""

import logging
import re
import sqlite3
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from budget.domain.calendar import Clock
from budget.storage.backup import backup_database
from budget.storage.recovery import quarantine_database, restore_from_backup

log = logging.getLogger(__name__)

BACKUP_SUFFIX = ".db"
_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"
_NAME_PATTERN = re.compile(
    r"^budget-(?P<stamp>\d{8}-\d{6})-(?P<kind>daily|weekly|monthly|on-demand|before-restore"
    r"|before-migration-(?P<version>[1-9]\d*))\.db$"
)


class BackupKind(StrEnum):
    """Вид копії. Лише щоденні, щотижневі й щомісячні входять до ротації (DS-5)."""

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    BEFORE_MIGRATION = "before-migration"
    ON_DEMAND = "on-demand"
    BEFORE_RESTORE = "before-restore"


@dataclass(frozen=True, slots=True)
class BackupInfo:
    path: Path
    kind: BackupKind
    created: datetime  # місцевий час Europe/Kyiv без tzinfo, як у назві файлу
    migration_version: int | None = None


def backup_file_name(
    kind: BackupKind, moment: datetime, migration_version: int | None = None
) -> str:
    """Назва файлу копії; номер міграції — лише для копії перед міграцією."""
    if (kind is BackupKind.BEFORE_MIGRATION) != (migration_version is not None):
        raise ValueError("Номер міграції задається лише для копії перед міграцією")
    token = f"{kind.value}-{migration_version}" if migration_version is not None else kind.value
    return f"budget-{moment.strftime(_TIMESTAMP_FORMAT)}-{token}{BACKUP_SUFFIX}"


def parse_backup_name(path: Path) -> BackupInfo | None:
    """Розпізнає копію за назвою; ``None`` — файл не класифіковано (його ніколи не видаляють)."""
    match = _NAME_PATTERN.fullmatch(path.name)
    if match is None:
        return None
    try:
        created = datetime.strptime(match.group("stamp"), _TIMESTAMP_FORMAT)
    except ValueError:
        return None
    version = match.group("version")
    if version is not None:
        return BackupInfo(path, BackupKind.BEFORE_MIGRATION, created, int(version))
    return BackupInfo(path, BackupKind(match.group("kind")), created)


def newest_first(backups: list[BackupInfo]) -> list[BackupInfo]:
    """Від найновішої до найстарішої; за однакового часу — стабільно за назвою."""
    return sorted(backups, key=lambda b: (b.created, b.path.name), reverse=True)


@dataclass(frozen=True, slots=True)
class RotationPolicy:
    """Автоматичний пул копій: одна копія на календарний період, зберігається ``keep``."""

    kind: BackupKind
    keep: int
    period: Callable[[datetime], Hashable]


def calendar_day(moment: datetime) -> Hashable:
    return moment.date()


DAILY = RotationPolicy(BackupKind.DAILY, 7, calendar_day)
AUTOMATIC_POLICIES: tuple[RotationPolicy, ...] = (DAILY,)


class BackupService:
    def __init__(self, connection: sqlite3.Connection, backups_dir: Path, clock: Clock) -> None:
        self._connection = connection
        self._backups_dir = backups_dir
        self._clock = clock

    def create_backup(self, kind: BackupKind, migration_version: int | None = None) -> Path:
        """Створює перевірену копію заданого виду (DS-4, DS-5)."""
        local = self._clock.now().replace(tzinfo=None)
        name = backup_file_name(kind, local, migration_version)
        return backup_database(self._connection, self._backups_dir / name)

    def backups(self) -> list[BackupInfo]:
        """Розпізнані копії від найновішої; нерозпізнані файли не враховуються."""
        if not self._backups_dir.is_dir():
            return []
        found = (parse_backup_name(p) for p in self._backups_dir.glob(f"budget-*{BACKUP_SUFFIX}"))
        return newest_first([b for b in found if b is not None])

    def list_backups(self) -> list[Path]:
        return [b.path for b in self.backups()]

    def run_automatic(self) -> list[Path]:
        """Створює автоматичні копії, яких ще немає за поточний період (DS-5).

        Для кожного пулу: якщо копії цього виду за поточний календарний період немає —
        створити й перевірити нову, і лише після цього застосувати ротацію саме цього
        пулу. Помилка створення пробрасується, а наявні копії лишаються незмінними.
        """
        created = []
        for policy in AUTOMATIC_POLICIES:
            path = self._ensure_period_backup(policy)
            if path is not None:
                created.append(path)
        return created

    def _ensure_period_backup(self, policy: RotationPolicy) -> Path | None:
        current = policy.period(self._clock.now().replace(tzinfo=None))
        pool = [b for b in self.backups() if b.kind is policy.kind]
        if any(policy.period(b.created) == current for b in pool):
            return None
        path = self.create_backup(policy.kind)
        self._rotate(policy)
        return path

    def _rotate(self, policy: RotationPolicy) -> None:
        """Лишає ``keep`` найновіших копій пулу; копії інших видів не зачіпає."""
        pool = [b for b in self.backups() if b.kind is policy.kind]
        for stale in pool[policy.keep :]:
            try:
                stale.path.unlink(missing_ok=True)
            except OSError:
                # Напр., файл тимчасово зайнятий на Windows: лишається до наступної ротації.
                log.warning("Не вдалося видалити застарілу копію %s", stale.path, exc_info=True)


def _timestamp(clock: Clock) -> str:
    return clock.now().strftime(_TIMESTAMP_FORMAT)


class RecoveryService:
    def __init__(self, database_path: Path, clock: Clock) -> None:
        self._database_path = database_path
        self._clock = clock

    def quarantine_corrupted(self) -> Path:
        """Зберігає пошкоджену базу під новою назвою, нічого в неї не записуючи."""
        return quarantine_database(self._database_path, _timestamp(self._clock))

    def restore(self, backup_path: Path) -> None:
        restore_from_backup(backup_path, self._database_path)
