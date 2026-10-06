"""Обробка пошкодженої бази й відновлення з резервної копії (DS-6)."""

import sqlite3
from pathlib import Path

from budget.errors import StorageError
from budget.storage.integrity import integrity_check

_SIDE_FILES = ("-wal", "-shm")


def quarantine_database(database_path: Path, suffix: str) -> Path:
    """Перейменовує пошкоджену базу (і файли WAL) без запису в неї.

    ``suffix`` — унікальна мітка, наприклад час виявлення пошкодження.
    """
    quarantined = database_path.with_name(f"{database_path.name}.corrupted-{suffix}")
    if quarantined.exists():
        raise StorageError(detail=f"Файл уже існує: {quarantined}")
    database_path.rename(quarantined)
    for side in _SIDE_FILES:
        side_path = database_path.with_name(database_path.name + side)
        if side_path.exists():
            side_path.rename(quarantined.with_name(quarantined.name + side))
    return quarantined


def restore_from_backup(backup_path: Path, database_path: Path) -> None:
    """Відновлює базу з перевіреної копії. Цільового файлу не повинно існувати."""
    if database_path.exists():
        raise StorageError(detail=f"Перед відновленням база має бути відсутня: {database_path}")
    source = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
    try:
        if not integrity_check(source):
            raise StorageError(detail=f"Резервна копія пошкоджена: {backup_path}")
        target = sqlite3.connect(database_path)
        try:
            source.backup(target)
        finally:
            target.close()
    except sqlite3.Error as exc:
        database_path.unlink(missing_ok=True)
        raise StorageError(detail=f"Відновлення не вдалося: {exc}") from exc
    finally:
        source.close()
