"""Обробка пошкодженої бази й відновлення з резервної копії (DS-6).

База в режимі WAL — це три файли: ``budget.db``, ``budget.db-wal`` і ``budget.db-shm``.
Їх переносять лише разом: WAL, що лишився біля іншої бази, змішався б із нею.
Резервну копію читають у режимі ``mode=ro&immutable=1``: без блокувань і без
створення поруч ``-wal``/``-shm``, тож файл копії не змінюється.
"""

import logging
import os
import sqlite3
from pathlib import Path

from budget.errors import StorageError
from budget.storage import migrations
from budget.storage.integrity import integrity_check

log = logging.getLogger(__name__)

_SIDE_FILES = ("-wal", "-shm")


def database_files(database_path: Path) -> list[Path]:
    """Основний файл і супутні файли WAL у порядку перенесення."""
    return [database_path, *(database_path.with_name(database_path.name + s) for s in _SIDE_FILES)]


def move_database_files(source: Path, target: Path) -> Path:
    """Переносить ``.db`` разом із ``-wal``/``-shm`` з ``source`` на ``target``.

    Нічого не записує в базу. Жоден із файлів ``target`` не повинен існувати. Якщо
    якийсь файл перенести не вдалося (напр., його тримає інший процес у Windows),
    уже перенесені повертаються назад — пара не розривається — і виникає ``StorageError``.
    """
    if any(p.exists() for p in database_files(target)):
        raise StorageError(detail=f"Файли бази вже існують: {target}")
    moves = [
        (old, new)
        for old, new in zip(database_files(source), database_files(target), strict=True)
        if old.exists()
    ]
    if not moves:
        raise StorageError(detail=f"Немає файлів бази для перенесення: {source}")
    done: list[tuple[Path, Path]] = []
    try:
        for old, new in moves:
            old.rename(new)
            done.append((old, new))
    except OSError as exc:
        for old, new in reversed(done):
            try:
                new.rename(old)
            except OSError:
                log.exception("Не вдалося повернути %s на місце", new)
        raise StorageError(detail=f"Не вдалося перенести файли бази {source}: {exc}") from exc
    return target


def set_aside_database(database_path: Path, label: str) -> Path:
    """Переносить файли бази вбік під назву ``<назва>.<label>`` (вільну)."""
    target = _free_name(database_path.with_name(f"{database_path.name}.{label}"))
    return move_database_files(database_path, target)


def _free_name(path: Path) -> Path:
    """Назва, не зайнята жодним із трьох файлів бази (додає ``-2``, ``-3``…)."""
    candidate, number = path, 1
    while any(p.exists() for p in database_files(candidate)):
        number += 1
        candidate = path.with_name(f"{path.name}-{number}")
    return candidate


def quarantine_database(database_path: Path, suffix: str) -> Path:
    """Перейменовує пошкоджену базу (і файли WAL) без запису в неї.

    ``suffix`` — унікальна мітка, наприклад час виявлення пошкодження.
    """
    if not database_path.exists():
        raise StorageError(detail=f"Немає бази для карантину: {database_path}")
    return set_aside_database(database_path, f"corrupted-{suffix}")


def open_backup_read_only(backup_path: Path) -> sqlite3.Connection:
    """Відкриває копію лише для читання, без блокувань і супутніх файлів."""
    uri = backup_path.resolve().as_uri() + "?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


def _is_restorable(connection: sqlite3.Connection) -> bool:
    """Повна перевірка цілісності й версія схеми, яку застосунок уміє відкрити."""
    if not integrity_check(connection):
        return False
    try:
        version = migrations.schema_version(connection)
    except sqlite3.DatabaseError:
        return False
    return 0 < version <= migrations.LATEST_VERSION


def verify_backup(backup_path: Path) -> bool:
    """Чи придатна копія для відновлення. Файл копії не змінюється."""
    try:
        connection = open_backup_read_only(backup_path)
    except sqlite3.Error:
        return False
    try:
        return _is_restorable(connection)
    finally:
        connection.close()


def discard_database(database_path: Path) -> None:
    """Видаляє файл бази разом із ``-wal``/``-shm`` (лише для власних тимчасових файлів)."""
    for path in database_files(database_path):
        path.unlink(missing_ok=True)


def restore_from_backup(backup_path: Path, database_path: Path) -> None:
    """Відновлює базу з перевіреної копії.

    Ні бази, ні її ``-wal``/``-shm`` на місці бути не повинно: попередній стан
    переносить сервісний шар. Копія спершу записується в тимчасовий файл поруч,
    перевіряється й скидається на диск, а тоді атомарно стає на місце бази
    (``os.replace``). Невдача — ``StorageError``, тимчасовий файл видаляється.
    """
    if any(p.exists() for p in database_files(database_path)):
        raise StorageError(detail=f"Перед відновленням бази не має бути: {database_path}")
    temporary = database_path.with_name(database_path.name + ".restoring")
    discard_database(temporary)
    try:
        source = open_backup_read_only(backup_path)
    except sqlite3.Error as exc:
        raise StorageError(detail=f"Не вдалося відкрити копію {backup_path}: {exc}") from exc
    try:
        if not _is_restorable(source):
            raise StorageError(detail=f"Резервна копія пошкоджена: {backup_path}")
        target = sqlite3.connect(temporary)
        try:
            source.backup(target)
            valid = _is_restorable(target)
        finally:
            target.close()
        if not valid:
            raise StorageError(detail=f"Відновлена копія не пройшла перевірку: {temporary}")
        if any(p.exists() for p in database_files(temporary)[1:]):
            raise StorageError(detail=f"Після запису лишилися файли WAL: {temporary}")
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        if any(p.exists() for p in database_files(database_path)):
            raise StorageError(detail=f"Під час відновлення з'явилася база: {database_path}")
        os.replace(temporary, database_path)
    except (sqlite3.Error, OSError) as exc:
        discard_database(temporary)
        raise StorageError(detail=f"Відновлення не вдалося: {exc}") from exc
    except BaseException:
        discard_database(temporary)
        raise
    finally:
        source.close()
