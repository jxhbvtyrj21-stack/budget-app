"""Класифікація помилки читання для стану екрана «Помилка» (IA 12).

Звичайна помилка читання — лише ``sqlite3.OperationalError`` (зайнята чи заблокована
база, помилка введення-виведення, переповнений диск, файл не відкривається), у
ланцюжку якої немає пошкодження. Пошкодження визначає та сама
``storage.integrity.corruption_code``, що й ``RuntimeCorruptionGuard``: для нього, як і
для будь-якого іншого винятку, результат — ``None``, і виняток іде далі без змін.
"""

import sqlite3

from budget.errors import DataReadError
from budget.storage.integrity import corruption_code


def read_failure(error: BaseException) -> DataReadError | None:
    """``DataReadError`` для звичайної помилки читання, інакше ``None``.

    Чиста функція: без введення-виведення, журналу й зміни стану. Початковий виняток —
    ``__cause__`` результату (як ``raise DataReadError(...) from error``).
    """
    if not isinstance(error, sqlite3.OperationalError):
        return None
    if corruption_code(error) is not None:
        return None
    failure = DataReadError(detail=f"{type(error).__name__}: {error}")
    failure.__cause__ = error
    return failure
