"""Класифікація помилки читання для стану екрана «Помилка» (IA 12, E2).

``read_failure`` бере звичайну помилку читання лише з ``sqlite3.OperationalError`` без
пошкодження в ланцюжку; пошкодження визначає та сама ``corruption_code``, що й
``RuntimeCorruptionGuard``. Для всього іншого — ``None``: виняток іде далі без змін.
"""

import sqlite3

import pytest

from budget.errors import BudgetError, DataReadError, DomainRuleError, StorageError
from budget.services.read_errors import read_failure
from budget.storage.integrity import corruption_code

IOERR_CORRUPTFS = getattr(sqlite3, "SQLITE_IOERR_CORRUPTFS", sqlite3.SQLITE_IOERR | (33 << 8))


def with_code(cls, code: int | None, message: str = "введена помилка") -> sqlite3.Error:
    error = cls(message)
    if code is not None:
        error.sqlite_errorcode = code
    return error


CORRUPTION = [
    pytest.param(sqlite3.DatabaseError, sqlite3.SQLITE_CORRUPT, id="SQLITE_CORRUPT"),
    pytest.param(sqlite3.DatabaseError, sqlite3.SQLITE_NOTADB, id="SQLITE_NOTADB"),
    pytest.param(sqlite3.DatabaseError, sqlite3.SQLITE_CORRUPT_INDEX, id="SQLITE_CORRUPT_INDEX"),
    pytest.param(
        sqlite3.DatabaseError, sqlite3.SQLITE_CORRUPT_SEQUENCE, id="SQLITE_CORRUPT_SEQUENCE"
    ),
    pytest.param(sqlite3.DatabaseError, sqlite3.SQLITE_CORRUPT_VTAB, id="SQLITE_CORRUPT_VTAB"),
    # Навіть як OperationalError: код пошкодження — не звичайна помилка читання.
    pytest.param(sqlite3.OperationalError, sqlite3.SQLITE_CORRUPT, id="OperationalError+CORRUPT"),
    pytest.param(sqlite3.OperationalError, sqlite3.SQLITE_NOTADB, id="OperationalError+NOTADB"),
]

ORDINARY = [
    pytest.param(sqlite3.SQLITE_BUSY, id="SQLITE_BUSY"),
    pytest.param(sqlite3.SQLITE_LOCKED, id="SQLITE_LOCKED"),
    pytest.param(sqlite3.SQLITE_IOERR, id="SQLITE_IOERR"),
    pytest.param(sqlite3.SQLITE_IOERR_READ, id="SQLITE_IOERR_READ"),
    pytest.param(IOERR_CORRUPTFS, id="SQLITE_IOERR_CORRUPTFS"),
    pytest.param(sqlite3.SQLITE_FULL, id="SQLITE_FULL"),
    pytest.param(sqlite3.SQLITE_CANTOPEN, id="SQLITE_CANTOPEN"),
    pytest.param(None, id="OperationalError-without-code"),
]


@pytest.mark.parametrize(("cls", "code"), CORRUPTION)
def test_corruption_is_never_a_read_failure(cls, code):
    error = with_code(cls, code)
    assert corruption_code(error) is not None  # те саме рішення, що в guard
    assert read_failure(error) is None


@pytest.mark.parametrize("code", ORDINARY)
def test_ordinary_operational_error_is_a_read_failure(code):
    error = with_code(sqlite3.OperationalError, code)
    assert corruption_code(error) is None
    failure = read_failure(error)
    assert isinstance(failure, DataReadError) and isinstance(failure, StorageError)
    assert failure.user_message == "Не вдалося прочитати дані."
    assert failure.__cause__ is error  # як ``raise DataReadError(...) from error``
    assert failure.__suppress_context__


def test_ioerr_corruptfs_follows_the_guard_classification():
    """Основний код — IOERR: guard (``code & 0xFF``) не вважає це пошкодженням бази."""
    assert IOERR_CORRUPTFS & 0xFF == sqlite3.SQLITE_IOERR
    assert read_failure(with_code(sqlite3.OperationalError, IOERR_CORRUPTFS)) is not None


@pytest.mark.parametrize("link", ["__cause__", "__context__"])
@pytest.mark.parametrize("code", [sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB])
def test_corruption_in_the_chain_is_not_a_read_failure(link, code):
    """Наприклад, невдалий ROLLBACK після пошкодження: ланцюжок важить, як і для guard."""
    corrupted = with_code(sqlite3.DatabaseError, code)
    outer = with_code(sqlite3.OperationalError, sqlite3.SQLITE_BUSY)
    setattr(outer, link, corrupted)
    assert corruption_code(outer) is not None
    assert read_failure(outer) is None


def test_deeper_chain_with_corruption_is_not_a_read_failure():
    corrupted = with_code(sqlite3.DatabaseError, sqlite3.SQLITE_CORRUPT)
    middle = with_code(sqlite3.OperationalError, sqlite3.SQLITE_IOERR)
    middle.__context__ = corrupted
    outer = with_code(sqlite3.OperationalError, sqlite3.SQLITE_BUSY)
    outer.__cause__ = middle
    assert read_failure(outer) is None


@pytest.mark.parametrize(
    "error",
    [
        # Надто широкий критерій ``DatabaseError`` заборонений: жоден підклас, крім
        # OperationalError, і сам DatabaseError без пошкодження — не звичайне читання.
        pytest.param(with_code(sqlite3.DatabaseError, sqlite3.SQLITE_ERROR), id="DatabaseError"),
        pytest.param(with_code(sqlite3.DatabaseError, None), id="DatabaseError-without-code"),
        pytest.param(sqlite3.ProgrammingError("Cannot operate on a closed database."), id="Prog"),
        pytest.param(sqlite3.InterfaceError("bad parameter"), id="InterfaceError"),
        pytest.param(with_code(sqlite3.IntegrityError, 19), id="IntegrityError"),
        pytest.param(sqlite3.DataError("too big"), id="DataError"),
        pytest.param(sqlite3.NotSupportedError("no"), id="NotSupportedError"),
        pytest.param(with_code(sqlite3.InternalError, sqlite3.SQLITE_INTERNAL), id="InternalError"),
        pytest.param(sqlite3.Error("base"), id="sqlite3.Error"),
        pytest.param(BudgetError(), id="BudgetError"),
        pytest.param(StorageError(), id="StorageError"),
        pytest.param(DomainRuleError(), id="DomainRuleError"),
        pytest.param(RuntimeError("bug"), id="RuntimeError"),
        pytest.param(OSError("disk"), id="OSError"),
        pytest.param(KeyboardInterrupt(), id="KeyboardInterrupt"),
    ],
)
def test_other_exceptions_are_not_read_failures(error):
    assert read_failure(error) is None


def test_read_failure_is_pure(monkeypatch, caplog):
    """Без журналу й без зміни винятку: той самий об'єкт, ті самі атрибути."""
    error = with_code(sqlite3.OperationalError, sqlite3.SQLITE_BUSY)
    before = (error.args, error.__cause__, error.__context__, error.sqlite_errorcode)
    with caplog.at_level("DEBUG"):
        first = read_failure(error)
        second = read_failure(error)
    assert caplog.records == []
    assert first is not second and first.__cause__ is second.__cause__ is error
    assert (error.args, error.__cause__, error.__context__, error.sqlite_errorcode) == before


def test_real_sqlite_errors_are_classified(tmp_path):
    """Справжні винятки SQLite: заблокована база — читання; пошкодження й «не база» — ні."""
    locked = tmp_path / "locked.db"
    writer = sqlite3.connect(locked, isolation_level=None)
    writer.execute("CREATE TABLE t (x)")
    writer.execute("BEGIN EXCLUSIVE")
    reader = sqlite3.connect(locked, timeout=0)
    with pytest.raises(sqlite3.OperationalError) as busy:
        reader.execute("SELECT * FROM t").fetchall()
    assert busy.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
    assert isinstance(read_failure(busy.value), DataReadError)
    reader.close()
    writer.close()

    not_a_db = tmp_path / "not.db"
    not_a_db.write_bytes(b"x" * 8192)
    connection = sqlite3.connect(not_a_db)
    with pytest.raises(sqlite3.DatabaseError) as notadb:
        connection.execute("SELECT * FROM sqlite_master").fetchall()
    assert notadb.value.sqlite_errorcode == sqlite3.SQLITE_NOTADB
    assert read_failure(notadb.value) is None
    connection.close()

    corrupted = tmp_path / "corrupt.db"
    connection = sqlite3.connect(corrupted)
    connection.execute("CREATE TABLE t (x)")
    connection.executemany("INSERT INTO t VALUES (?)", [("a" * 500,)] * 400)
    connection.commit()
    connection.close()
    data = bytearray(corrupted.read_bytes())
    data[4096 * 2 : 4096 * 3] = b"\xab" * 4096
    corrupted.write_bytes(bytes(data))
    connection = sqlite3.connect(corrupted)
    with pytest.raises(sqlite3.DatabaseError) as corrupt:
        connection.execute("SELECT count(*), max(x) FROM t").fetchall()
    assert corrupt.value.sqlite_errorcode & 0xFF == sqlite3.SQLITE_CORRUPT
    assert read_failure(corrupt.value) is None
    connection.close()
