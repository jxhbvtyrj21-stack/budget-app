"""Підключення до SQLite з обов'язковими параметрами надійності (розділ 5.3, DS-3)."""

import sqlite3
from pathlib import Path

from budget.errors import DatabaseCorruptedError, StorageError


def open_database(path: Path) -> sqlite3.Connection:
    """Відкриває базу з WAL, ``synchronous=FULL`` і зовнішніми ключами.

    Транзакціями керує лише сервісний шар (``storage.transaction``): підключення
    працює в режимі автокоміту без неявних транзакцій модуля ``sqlite3``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        connection = sqlite3.connect(path, isolation_level=None)
    except sqlite3.Error as exc:
        raise StorageError(detail=f"Не вдалося відкрити базу {path}: {exc}") from exc
    try:
        mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
    except sqlite3.DatabaseError as exc:
        close_without_checkpoint(connection)
        raise DatabaseCorruptedError(detail=f"{path}: {exc}") from exc
    if str(mode).lower() != "wal":
        connection.close()
        raise StorageError(detail=f"Не вдалося ввімкнути WAL для {path}: {mode}")
    return connection


def close_without_checkpoint(connection: sqlite3.Connection) -> None:
    """Закриває з'єднання з пошкодженою базою, нічого не записуючи в її основний файл.

    Звичайне закриття останнього з'єднання переносить кадри WAL в основний файл
    (checkpoint) і прибирає ``-wal``/``-shm``. Для пошкодженої бази це запис у
    пошкоджені дані, тож checkpoint під час закриття вимикається: ``.db``, ``-wal`` і
    ``-shm`` лишаються такими, якими були в момент виявлення, і переносяться разом.
    """
    connection.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
    connection.close()
