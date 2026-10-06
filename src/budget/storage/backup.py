"""Узгоджені резервні копії через вбудоване API SQLite (DS-5).

Розклад, ротація й назви копій — відповідальність сервісного шару.
"""

import sqlite3
from pathlib import Path

from budget.errors import StorageError
from budget.storage.integrity import integrity_check


def backup_database(connection: sqlite3.Connection, destination: Path) -> Path:
    """Копіює базу в новий файл і перевіряє цілісність копії."""
    if destination.exists():
        raise StorageError(detail=f"Файл резервної копії вже існує: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(destination)
    try:
        connection.backup(target)
        valid = integrity_check(target)
    except sqlite3.Error as exc:
        target.close()
        destination.unlink(missing_ok=True)
        raise StorageError(detail=f"Резервне копіювання не вдалося: {exc}") from exc
    target.close()
    if not valid:
        destination.unlink(missing_ok=True)
        raise StorageError(detail=f"Резервна копія не пройшла перевірку цілісності: {destination}")
    return destination
