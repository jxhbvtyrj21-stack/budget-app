"""Підготовка бази під час запуску: перевірка, обов'язкова копія перед міграцією, міграції."""

import sqlite3
from pathlib import Path

from budget.domain.calendar import Clock
from budget.errors import DatabaseCorruptedError
from budget.services.backup import BackupService
from budget.storage.database import open_database
from budget.storage.integrity import quick_check
from budget.storage.migrations import migrate, schema_version


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
                backups.create_backup(f"before-migration-{version}")

        migrate(connection, before_migration=backup_before)
    except BaseException:
        connection.close()
        raise
    return connection
