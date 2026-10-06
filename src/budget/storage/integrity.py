"""Перевірки цілісності бази (DS-4)."""

import sqlite3


def quick_check(connection: sqlite3.Connection) -> bool:
    """Швидка перевірка під час запуску."""
    try:
        rows = connection.execute("PRAGMA quick_check").fetchall()
    except sqlite3.DatabaseError:
        return False
    return rows == [("ok",)]


def integrity_check(connection: sqlite3.Connection) -> bool:
    """Повна перевірка перед і після резервного копіювання."""
    try:
        rows = connection.execute("PRAGMA integrity_check").fetchall()
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.DatabaseError:
        return False
    return rows == [("ok",)] and not violations


# Основні коди результату SQLite, що означають пошкоджену базу. Розширені коди
# (SQLITE_CORRUPT_VTAB, _SEQUENCE, _INDEX) мають той самий основний код у молодшому байті.
_CORRUPTION_CODES = frozenset({sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB})


def corruption_code(error: BaseException) -> int | None:
    """Код SQLite, якщо помилка (або її причина в ланцюжку) — пошкодження бази.

    Класифікація лише за ``sqlite_errorcode`` (не за текстом): основний код
    ``SQLITE_CORRUPT`` чи ``SQLITE_NOTADB``. Ланцюжок ``__cause__``/``__context__``
    враховується, бо невдалий ``ROLLBACK`` може замінити собою первинну помилку.
    """
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        code = getattr(current, "sqlite_errorcode", None)
        if isinstance(current, sqlite3.Error) and isinstance(code, int):
            if code & 0xFF in _CORRUPTION_CODES:
                return code
        current = current.__cause__ or current.__context__
    return None


def is_corruption_error(error: BaseException) -> bool:
    return corruption_code(error) is not None
