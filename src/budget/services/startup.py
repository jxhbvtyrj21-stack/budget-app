"""Підготовка бази під час запуску: перевірка, обов'язкова копія перед міграцією, міграції.

Також ознаки попередньої бази, коли робочої бази немає (R1): тоді нова база не
створюється, доки користувач не відновить дані з копії або явно не почне з порожніми.
"""

import os
import sqlite3
from pathlib import Path

from budget.domain.calendar import Clock
from budget.errors import DatabaseCorruptedError, StorageError
from budget.services.backup import BackupKind, BackupService, file_timestamp, find_backups
from budget.storage.database import close_without_checkpoint, open_database
from budget.storage.integrity import quick_check
from budget.storage.migrations import migrate, schema_version
from budget.storage.recovery import database_files, set_aside_database

# Файли, які лишає карантин чи відновлення поруч із базою (``quarantine_database``,
# ``RecoveryService.restore``, ``restore_from_backup``), разом із їхніми -wal/-shm.
_SET_ASIDE_LABELS = ("corrupted-", "replaced-", "orphaned-", "restoring")

ORPHANS_NOT_SET_ASIDE_MESSAGE = (
    "Не вдалося відкласти залишкові файли попередньої бази, тому нову базу не створено."
)


def startup_recovery_indicators(root: Path, database: Path, backups_dir: Path) -> list[Path]:
    """Ознаки попередньої бази, якщо робочої бази ``database`` немає (R1).

    Ознаки: у ``root`` — файли карантину чи відновлення (``<база>.corrupted-*``,
    ``.replaced-*``, ``.orphaned-*``, ``.restoring*``) або залишкові ``<база>-wal`` /
    ``<база>-shm``; у ``backups_dir`` — хоч одна копія, розпізнана ``find_backups`` за
    назвою (без перевірки цілісності). Порожній результат — база є або це перший запуск.

    Лише читання: нічого не створює й не відкриває. Відсутні ``root`` чи ``backups_dir``
    — ознак немає; будь-яка інша ``OSError`` пробрасується: невідомий стан не означає,
    що ознак немає.
    """
    if database.exists():
        return []
    name = database.name.casefold()
    prefixes = tuple(f"{name}.{label}" for label in _SET_ASIDE_LABELS)
    leftovers = {p.name.casefold() for p in database_files(database)[1:]}
    found = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                candidate = entry.name.casefold()
                if candidate in leftovers or candidate.startswith(prefixes):
                    found.append(Path(entry.path))
    except FileNotFoundError:
        return []
    try:
        with os.scandir(backups_dir):  # помилку доступу не сприймати як «копій немає»
            pass
    except FileNotFoundError:
        return sorted(found)
    return sorted(found) + [b.path for b in find_backups(backups_dir)]


def set_aside_orphaned_files(database: Path, clock: Clock) -> Path | None:
    """Перед явним «почати з порожніми даними»: залишкові ``-wal``/``-shm`` без бази
    відкладаються як ``<база>.orphaned-<час>`` (``set_aside_database``), щоб нова база
    їх не поглинула. Невдача — ``StorageError``; тоді нову базу створювати не можна."""
    try:
        if database.exists():  # робоча база є — нічого не відкладати
            return None
        if not any(p.exists() for p in database_files(database)[1:]):
            return None
        return set_aside_database(database, f"orphaned-{file_timestamp(clock)}")
    except StorageError as exc:
        raise StorageError(ORPHANS_NOT_SET_ASIDE_MESSAGE, detail=exc.detail) from exc
    except OSError as exc:
        raise StorageError(ORPHANS_NOT_SET_ASIDE_MESSAGE, detail=str(exc)) from exc


def prepare_database(database_path: Path, backups_dir: Path, clock: Clock) -> sqlite3.Connection:
    """Відкриває базу, виконує ``quick_check`` і міграції.

    Пошкоджена база — ``DatabaseCorruptedError``; у такому разі нічого не записується.
    Перед міграцією наявної бази робиться резервна копія (DS-5).
    """
    connection = open_database(database_path)
    try:
        if not quick_check(connection):
            raise DatabaseCorruptedError(detail=f"quick_check не пройдено: {database_path}")
        backups = BackupService(connection, backups_dir, clock)

        def backup_before(version: int) -> None:
            if schema_version(connection) > 0:
                backups.create_backup(BackupKind.BEFORE_MIGRATION, version)

        migrate(connection, before_migration=backup_before)
    except DatabaseCorruptedError:
        close_without_checkpoint(connection)
        raise
    except BaseException:
        connection.close()
        raise
    return connection
