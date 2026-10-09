"""Явні транзакції: одна дія користувача — одна транзакція (DS-2)."""

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from budget.storage.integrity import corruption_code

log = logging.getLogger(__name__)


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """``BEGIN IMMEDIATE`` … ``COMMIT``; відкат у разі будь-якої помилки.

    Помилка в тілі й невдалий ``COMMIT`` обробляються однаково: відкат лише тоді, коли
    транзакція ще активна (``in_transaction``) — після деяких помилок (напр., I/O)
    SQLite відкочує її сама. Далі — той самий первинний виняток.

    Відкривають транзакції лише сервіси; репозиторії сховища самі не фіксують зміни.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
        connection.execute("COMMIT")
    except BaseException as error:
        _roll_back(connection, error)
        raise


def _roll_back(connection: sqlite3.Connection, error: BaseException) -> None:
    """Відкат після ``error``, якщо транзакція ще активна.

    Невдалий ``ROLLBACK`` не підміняє первинний виняток: він лишається основним, а
    вторинна помилка — у нотатці й журналі. Виняток — пошкодження бази, виявлене під
    час відкату (``corruption_code``): воно йде далі (до guard), а первинний виняток
    — у ланцюжку (``__context__``).
    """
    if not connection.in_transaction:
        return  # SQLite уже відкотила транзакцію — повторний ROLLBACK лише зашкодив би
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error as failure:
        # Пошкодження виявив саме відкат (первинний виняток ним не був) — до guard.
        if corruption_code(failure) is not None and corruption_code(error) is None:
            raise
        log.error("ROLLBACK failed after %s", type(error).__name__, exc_info=failure)
        error.add_note(f"ROLLBACK також не вдався: {type(failure).__name__}: {failure}")
