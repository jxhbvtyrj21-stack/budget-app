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
        connection.close()
        raise DatabaseCorruptedError(detail=f"{path}: {exc}") from exc
    if str(mode).lower() != "wal":
        connection.close()
        raise StorageError(detail=f"Не вдалося ввімкнути WAL для {path}: {mode}")
    return connection
