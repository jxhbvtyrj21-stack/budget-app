"""Нумеровані міграції лише вперед; версія схеми — ``PRAGMA user_version`` (DS-7)."""

import sqlite3
from collections.abc import Callable

from budget.errors import StorageError
from budget.storage.migrations import m0001_initial

MIGRATIONS: tuple[tuple[int, str], ...] = ((m0001_initial.VERSION, m0001_initial.SQL),)
LATEST_VERSION = MIGRATIONS[-1][0]


def schema_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def pending_migrations(connection: sqlite3.Connection) -> list[int]:
    current = schema_version(connection)
    if current > LATEST_VERSION:
        raise StorageError(
            "Дані створено новішою версією застосунку. Оновіть застосунок.",
            detail=f"Версія схеми {current} новіша за підтримувану {LATEST_VERSION}",
        )
    return [version for version, _ in MIGRATIONS if version > current]


def migrate(
    connection: sqlite3.Connection,
    before_migration: Callable[[int], None] | None = None,
) -> list[int]:
    """Застосовує відсутні міграції, кожну в окремій транзакції.

    ``before_migration(version)`` викликається перед кожною міграцією; через нього
    сервісний шар робить обов'язкову резервну копію (DS-5).
    """
    applied = []
    scripts = dict(MIGRATIONS)
    for version in pending_migrations(connection):
        if before_migration is not None:
            before_migration(version)
        script = f"BEGIN IMMEDIATE;\n{scripts[version]}\nPRAGMA user_version = {version};\nCOMMIT;"
        try:
            connection.executescript(script)
        except sqlite3.Error as exc:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise StorageError(detail=f"Міграція {version} не вдалася: {exc}") from exc
        applied.append(version)
    return applied
