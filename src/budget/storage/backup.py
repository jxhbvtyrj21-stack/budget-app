"""Узгоджені резервні копії через вбудоване API SQLite (DS-4, DS-5).

Порядок: повний ``integrity_check`` вихідної бази → ``Connection.backup()`` (узгоджена
копія й під час роботи в режимі WAL) у тимчасовий файл поруч → повна перевірка самої
копії → скидання на диск → атомарне перейменування на остаточну назву. Розклад,
ротація й назви копій — відповідальність сервісного шару.
"""

import logging
import os
import sqlite3
import tempfile
from os import replace
from pathlib import Path

from budget.errors import DatabaseCorruptedError, StorageError
from budget.storage.integrity import integrity_check
from budget.storage.recovery import discard_database

log = logging.getLogger(__name__)

# Тимчасова копія: ``<остаточна назва>.<унікальна частина>.partial``. Назва не
# закінчується на ``.db``, тож копією її не вважають ні пошук, ні ротація, ні відновлення.
PARTIAL_SUFFIX = ".partial"
# ``replace`` прив'язано під час імпорту: підміни ``os.replace`` в інших модулях (напр., у
# тестах невдалого відновлення) не зачіпають публікацію копії.


def backup_database(connection: sqlite3.Connection, destination: Path) -> Path:
    """Копіює базу в новий файл і перевіряє цілісність копії.

    Пошкоджена вихідна база — ``DatabaseCorruptedError``: копія не створюється, а
    наявні копії не змінюються. Невдала чи пошкоджена нова копія видаляється.
    Будь-яка інша невдача самої копії (тека копій недоступна, файл копії не
    створюється чи не відкривається, копіювання, перевірка, скидання на диск чи
    перейменування не вдалися, остаточна назва вже зайнята) — ``StorageError``;
    з'єднання з вихідною базою при цьому не закривається. Пошкодження вихідної бази
    перевіряється першим і не маскується помилкою копії.

    Копія пишеться в тимчасовий файл у тій самій теці (``PARTIAL_SUFFIX``) і лише
    перевірена, закрита й скинута на диск атомарно отримує остаточну назву (``replace``).
    Тож ні невдача, ні аварія процесу не лишають неповної копії під назвою справжньої.
    """
    if destination.exists():
        raise StorageError(detail=f"Файл резервної копії вже існує: {destination}")
    if not integrity_check(connection):
        raise DatabaseCorruptedError(
            detail=f"Вихідна база не пройшла integrity_check; копію {destination.name} не створено"
        )
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(
            prefix=f"{destination.name}.", suffix=PARTIAL_SUFFIX, dir=destination.parent
        )
        os.close(handle)
    except OSError as exc:
        raise StorageError(
            detail=f"Не вдалося створити файл резервної копії {destination}: {exc}"
        ) from exc
    temporary = Path(name)
    try:
        target = sqlite3.connect(temporary)
        try:
            connection.backup(target)
            valid = integrity_check(target)
        finally:
            target.close()
        if not valid:
            raise StorageError(
                detail=f"Резервна копія не пройшла перевірку цілісності: {destination}"
            )
        with temporary.open("r+b") as copy:
            os.fsync(copy.fileno())
        if destination.exists():
            raise StorageError(detail=f"Файл резервної копії вже існує: {destination}")
        replace(temporary, destination)
    except sqlite3.Error as exc:
        _discard(temporary)
        raise StorageError(detail=f"Резервне копіювання не вдалося: {exc}") from exc
    except OSError as exc:
        _discard(temporary)
        raise StorageError(detail=f"Резервну копію {destination} не збережено: {exc}") from exc
    except BaseException:
        _discard(temporary)
        raise
    return destination


def _discard(temporary: Path) -> None:
    """Прибирає невдалу тимчасову копію разом із її ``-wal``/``-shm``/``-journal``; якщо
    файл не видаляється, лишається первинна помилка."""
    try:
        discard_database(temporary)
    except OSError:
        log.warning("Не вдалося видалити невдалу копію %s", temporary, exc_info=True)
