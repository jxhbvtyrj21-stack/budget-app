"""Підключення до SQLite з обов'язковими параметрами надійності (розділ 5.3, DS-3)."""

import sqlite3
from pathlib import Path

from budget.errors import DatabaseCorruptedError, StorageError


def open_database(path: Path, *, create: bool = True) -> sqlite3.Connection:
    """Відкриває базу з WAL, ``synchronous=FULL`` і зовнішніми ключами.

    Транзакціями керує лише сервісний шар (``storage.transaction``): підключення
    працює в режимі автокоміту без неявних транзакцій модуля ``sqlite3``.

    ``create=False`` — база вже має існувати: відкриття в режимі SQLite ``mode=rw``, який
    не створює відсутній файл. Якщо файл зник після попередньої перевірки, — ``StorageError``
    із причиною, а не нова порожня база (і SQLite не прибирає ``-wal``/``-journal``, що
    лишилися від зниклої бази). ``create=True`` — лише перший запуск і явне створення.
    """
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        target, uri = str(path), False
    else:
        # ``as_uri`` кодує пробіли, Unicode, ``?``, ``#`` і ``%`` у шляху (UTF-8, %XX).
        target, uri = path.absolute().as_uri() + "?mode=rw", True
    try:
        connection = sqlite3.connect(target, isolation_level=None, uri=uri)
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
