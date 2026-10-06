"""Явні транзакції: одна дія користувача — одна транзакція (DS-2)."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """``BEGIN IMMEDIATE`` … ``COMMIT``; відкат у разі будь-якої помилки.

    Відкривають транзакції лише сервіси; репозиторії сховища самі не фіксують зміни.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")
