"""Резервні копії й відновлення (DS-4, DS-5, DS-6).

Вид і час копії визначаються лише з назви файлу ``budget-YYYYMMDD-HHMMSS-<вид>.db``;
окремої таблиці чи метаданих у базі немає. Час у назві — за Europe/Kyiv (``Clock``).
Відновлення після пошкодження — ``RecoveryService`` (DS-6; IA 10.1).
"""

import logging
import re
import sqlite3
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from budget.domain.calendar import CalendarMonth, Clock
from budget.errors import DatabaseCorruptedError, StorageError
from budget.storage.backup import backup_database
from budget.storage.database import open_database
from budget.storage.integrity import integrity_check
from budget.storage.recovery import (
    database_files,
    discard_database,
    move_database_files,
    quarantine_database,
    restore_from_backup,
    set_aside_database,
    verify_backup,
)

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


def find_backups(backups_dir: Path) -> list[BackupInfo]:
    """Розпізнані за назвою копії від найновішої; нерозпізнані файли не враховуються."""
    if not backups_dir.is_dir():
        return []
    found = (parse_backup_name(p) for p in backups_dir.glob(f"budget-*{BACKUP_SUFFIX}"))
    return newest_first([b for b in found if b is not None and b.path.is_file()])


@dataclass(frozen=True, slots=True)
class RestoreCandidate:
    """Копія, придатна для відновлення: пройшла повну перевірку цілісності."""

    backup: BackupInfo
    size: int  # байти


def restore_candidates(backups_dir: Path) -> list[RestoreCandidate]:
    """Справні копії всіх видів від найновішої (DS-6; IA 10.1).

    Кожна копія відкривається лише для читання й проходить ``integrity_check``;
    пошкоджені не пропонуються. Нічого не видаляється й не змінюється.
    """
    candidates = []
    for backup in find_backups(backups_dir):
        if not verify_backup(backup.path):
            log.warning("Копія %s не пройшла перевірку й не пропонується", backup.path.name)
            continue
        try:
            size = backup.path.stat().st_size
        except OSError:
            continue
        candidates.append(RestoreCandidate(backup, size))
    return candidates


@dataclass(frozen=True, slots=True)
class RotationPolicy:
    """Автоматичний пул копій: одна копія на календарний період, зберігається ``keep``."""

    kind: BackupKind
    keep: int
    period: Callable[[datetime], Hashable]


def calendar_day(moment: datetime) -> Hashable:
    return moment.date()


def calendar_week(moment: datetime) -> Hashable:
    """Календарний тиждень ISO 8601 (з понеділка) за київським часом, не ковзні 7 днів."""
    year, week, _ = moment.isocalendar()
    return year, week


def calendar_month(moment: datetime) -> Hashable:
    """Календарний місяць (ADR 0009); час у назві копії вже київський."""
    return CalendarMonth(moment.year, moment.month)


DAILY = RotationPolicy(BackupKind.DAILY, 7, calendar_day)
WEEKLY = RotationPolicy(BackupKind.WEEKLY, 4, calendar_week)
MONTHLY = RotationPolicy(BackupKind.MONTHLY, 12, calendar_month)
AUTOMATIC_POLICIES: tuple[RotationPolicy, ...] = (DAILY, WEEKLY, MONTHLY)


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
        return find_backups(self._backups_dir)

    def list_backups(self) -> list[Path]:
        return [b.path for b in self.backups()]

    def candidates(self) -> list[RestoreCandidate]:
        """Справні копії для показу й відновлення (ті самі правила, що й DS-6)."""
        return restore_candidates(self._backups_dir)

    def run_automatic(self) -> list[Path]:
        """Створює автоматичні копії, яких ще немає за поточний період (DS-5).

        Для кожного пулу: якщо копії цього виду за поточний календарний період немає —
        створити й перевірити нову, і лише після цього застосувати ротацію саме цього
        пулу. Звичайна помилка копіювання (напр., бракує місця на диску) записується в
        журнал і не зупиняє запуск: ротація цього пулу не виконується, наявні копії
        лишаються. Пошкодження вихідної бази (``DatabaseCorruptedError``) пробрасується.
        """
        created = []
        for policy in AUTOMATIC_POLICIES:
            try:
                path = self._ensure_period_backup(policy)
            except DatabaseCorruptedError:
                raise
            except StorageError:
                log.exception("Автоматична копія «%s» не створена", policy.kind.value)
                continue
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


class RestoreError(StorageError):
    """Відновлення не завершене; попередній стан файлів бази повернуто (DS-6)."""

    default_message = (
        "Не вдалося відновити дані з вибраної копії. Попередній стан файлів даних "
        "залишився без змін."
    )


ROLLBACK_FAILED_MESSAGE = (
    "Не вдалося відновити дані з вибраної копії, а попередній стан файлів даних повернути "
    "не вдалося. Резервні копії не змінено."
)


@dataclass(frozen=True, slots=True)
class RestoreOutcome:
    """Що зроблено під час заміни бази; потрібне, щоб завершити або відкотити відновлення."""

    backup: Path
    before_restore: Path | None  # перевірена копія поточного стану (BEFORE_RESTORE)
    set_aside: Path | None  # попередні файли бази, перенесені вбік разом із -wal/-shm
    current_was_corrupted: bool


class RecoveryService:
    """Карантин і відновлення бази з резервної копії (DS-5, DS-6).

    Перед відновленням усі з'єднання з базою мають бути закриті. Відновлення
    складається з ``restore`` (заміна файлів), повторного відкриття бази звичайним
    шляхом запуску і ``finish`` або ``rollback`` залежно від його результату.
    """

    def __init__(self, database_path: Path, backups_dir: Path, clock: Clock) -> None:
        self._database_path = database_path
        self._backups_dir = backups_dir
        self._clock = clock

    def quarantine_corrupted(self) -> Path:
        """Зберігає пошкоджену базу під новою назвою, нічого в неї не записуючи."""
        return quarantine_database(self._database_path, _timestamp(self._clock))

    def candidates(self) -> list[RestoreCandidate]:
        return restore_candidates(self._backups_dir)

    def restore(self, backup_path: Path) -> RestoreOutcome:
        """Замінює базу вибраною копією.

        1. Копія має пройти повну перевірку цілісності.
        2. Якщо поточна база є, спершу створюється копія ``BEFORE_RESTORE`` з тими
           самими перевірками, що й будь-яка копія (DS-4). Пошкоджену поточну базу
           копією не видають: її файли зберігаються як карантин (DS-6).
        3. Поточні ``.db``/``-wal``/``-shm`` переносяться вбік разом.
        4. Копія атомарно стає на місце бази. Невдача — файли повертаються на місце.
        """
        if not verify_backup(backup_path):
            raise RestoreError(
                "Вибрана копія пошкоджена або не підходить для відновлення. Оберіть іншу копію.",
                detail=f"Копія не пройшла перевірку: {backup_path}",
            )
        stamp = _timestamp(self._clock)
        before_restore, corrupted = None, False
        if self._database_path.exists():
            try:
                before_restore = self._backup_current()
            except DatabaseCorruptedError:
                corrupted = True
            except StorageError as exc:
                raise RestoreError(
                    "Не вдалося створити резервну копію поточних даних, тому відновлення "
                    "не виконано. Поточні дані не змінено.",
                    detail=f"Копію перед відновленням не створено: {exc.detail}",
                ) from exc
        set_aside = None
        if any(p.exists() for p in database_files(self._database_path)):
            if not self._database_path.exists():
                label = f"orphaned-{stamp}"  # залишки WAL без бази: зберегти, але прибрати
            else:
                label = f"{'corrupted' if corrupted else 'replaced'}-{stamp}"
            try:
                set_aside = set_aside_database(self._database_path, label)
            except StorageError as exc:
                raise RestoreError(detail=exc.detail) from exc
        try:
            restore_from_backup(backup_path, self._database_path)
        except StorageError as exc:
            try:
                self._put_back(set_aside)
            except RestoreError as failure:
                log.exception("Не вдалося повернути попередні файли бази")
                raise RestoreError(
                    ROLLBACK_FAILED_MESSAGE, detail=f"{exc.detail}; повернення: {failure.detail}"
                ) from exc
            raise RestoreError(detail=exc.detail) from exc
        return RestoreOutcome(backup_path, before_restore, set_aside, corrupted)

    def check_restored(self, connection: sqlite3.Connection) -> None:
        """Повна перевірка відкритої відновленої бази."""
        if not integrity_check(connection):
            raise RestoreError(detail="Відновлена база не пройшла integrity_check")

    def finish(self, outcome: RestoreOutcome) -> None:
        """Відновлення вдалося. Замінену справну базу, збережену копією, прибрати."""
        if outcome.set_aside is not None and outcome.before_restore is not None:
            try:
                discard_database(outcome.set_aside)
            except OSError:
                log.warning("Не вдалося прибрати %s", outcome.set_aside, exc_info=True)

    def rollback(self, outcome: RestoreOutcome) -> None:
        """Відновлення не вдалося після заміни: повернути попередні файли бази.

        Відновлений файл — лише копія резервної копії, яка лишається на місці.
        Невдалий відкат — ``RestoreError`` із ``ROLLBACK_FAILED_MESSAGE``: користувачу
        не можна казати, що попередній стан збережено без змін.
        """
        try:
            discard_database(self._database_path)
        except OSError as exc:
            raise RestoreError(
                ROLLBACK_FAILED_MESSAGE, detail=f"Не вдалося прибрати відновлену базу: {exc}"
            ) from exc
        try:
            self._put_back(outcome.set_aside)
        except RestoreError as exc:
            raise RestoreError(ROLLBACK_FAILED_MESSAGE, detail=exc.detail) from exc

    def _backup_current(self) -> Path:
        connection = open_database(self._database_path)
        try:
            return BackupService(connection, self._backups_dir, self._clock).create_backup(
                BackupKind.BEFORE_RESTORE
            )
        finally:
            connection.close()

    def _put_back(self, set_aside: Path | None) -> None:
        if set_aside is None:
            return
        try:
            move_database_files(set_aside, self._database_path)
        except StorageError as exc:
            raise RestoreError(detail=exc.detail) from exc
