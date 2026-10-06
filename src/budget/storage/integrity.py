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
