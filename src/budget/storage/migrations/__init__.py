"""Нумеровані міграції лише вперед; версія схеми — ``PRAGMA user_version`` (DS-7)."""

import logging
import sqlite3
from collections.abc import Callable

from budget.errors import DatabaseCorruptedError, StorageError
from budget.storage.integrity import corruption_code
from budget.storage.migrations import m0001_initial

log = logging.getLogger(__name__)

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

    Невдала міграція відкочується, лише якщо транзакція ще активна, і нової версії схеми
    не фіксує. Помилка SQLite стає ``StorageError``, а пошкодження бази
    (``corruption_code`` первинної помилки чи невдалого ``ROLLBACK``) —
    ``DatabaseCorruptedError``; первинна помилка лишається в ланцюжку. Інший виняток іде
    далі тим самим об'єктом.
    """
    applied = []
    scripts = dict(MIGRATIONS)
    for version in pending_migrations(connection):
        if before_migration is not None:
            before_migration(version)
        script = f"BEGIN IMMEDIATE;\n{scripts[version]}\nPRAGMA user_version = {version};\nCOMMIT;"
        try:
            connection.executescript(script)
        except BaseException as error:
            failure = _roll_back(connection, error)
            if not isinstance(error, sqlite3.Error):
                raise
            detail = f"Міграція {version} не вдалася: {error}"
            if failure is not None:
                detail += f"; ROLLBACK також не вдався: {failure}"
            if corruption_code(error) is not None:
                raise DatabaseCorruptedError(detail=detail) from error
            if failure is not None and corruption_code(failure) is not None:
                # Пошкодження виявив відкат; первинна помилка — його __context__.
                raise DatabaseCorruptedError(detail=detail) from failure
            raise StorageError(detail=detail) from error
        applied.append(version)
    return applied


def _roll_back(connection: sqlite3.Connection, error: BaseException) -> sqlite3.Error | None:
    """``ROLLBACK`` після ``error``, лише якщо транзакція ще активна: після деяких помилок
    (напр., I/O чи ``SQLITE_BUSY`` на ``BEGIN``) її вже немає.

    Невдалий відкат не підміняє ``error``: він у журналі (зі стеком) і в нотатці до
    ``error`` і повертається для класифікації. Транзакція тоді може лишитися активною —
    її закриває власник з'єднання (``prepare_database``).
    """
    if not connection.in_transaction:
        return None
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error as failure:
        log.error(
            "ROLLBACK failed after migration error %s", type(error).__name__, exc_info=failure
        )
        error.add_note(f"ROLLBACK також не вдався: {type(failure).__name__}: {failure}")
        return failure
    return None
