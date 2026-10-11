"""Класифікація звичайних помилок сховища: читання для стану екрана «Помилка» (IA 12)
і запису для повідомлення «Помилка» майстра (IA 12, «Помилка збереження»).

Звичайна помилка — лише ``sqlite3.OperationalError`` (зайнята чи заблокована база,
помилка введення-виведення, переповнений диск, файл не відкривається), у ланцюжку якої
немає пошкодження. Пошкодження визначає та сама ``storage.integrity.corruption_code``,
що й ``RuntimeCorruptionGuard``: для нього, як і для будь-якого іншого винятку,
результат — ``None``, і виняток іде далі без змін.
"""

import sqlite3

from budget.errors import DataReadError, DataWriteError
from budget.storage.integrity import corruption_code


def _is_ordinary(error: BaseException) -> bool:
    """``sqlite3.OperationalError`` без пошкодження в ланцюжку — єдине правило для обох."""
    return isinstance(error, sqlite3.OperationalError) and corruption_code(error) is None


def read_failure(error: BaseException) -> DataReadError | None:
    """``DataReadError`` для звичайної помилки читання, інакше ``None``.

    Чиста функція: без введення-виведення, журналу й зміни стану. Початковий виняток —
    ``__cause__`` результату (як ``raise DataReadError(...) from error``).
    """
    if not _is_ordinary(error):
        return None
    failure = DataReadError(detail=f"{type(error).__name__}: {error}")
    failure.__cause__ = error
    return failure


def write_failure(error: BaseException) -> DataWriteError | None:
    """``DataWriteError`` для звичайної помилки запису, інакше ``None`` (те саме правило,
    що й ``read_failure``). Чиста функція; початковий виняток — ``__cause__``."""
    if not _is_ordinary(error):
        return None
    failure = DataWriteError(detail=f"{type(error).__name__}: {error}")
    failure.__cause__ = error
    return failure
