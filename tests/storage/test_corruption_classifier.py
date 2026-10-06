"""Класифікатор пошкодження бази: лише коди SQLite, не текст (Block C3)."""

import sqlite3

import pytest

from budget.storage.integrity import corruption_code, is_corruption_error


def raised(action) -> sqlite3.Error:
    try:
        action()
    except sqlite3.Error as error:
        return error
    raise AssertionError("помилки не було")


def with_code(cls: type[sqlite3.Error], code: int, message: str = "") -> sqlite3.Error:
    """Синтетична помилка з кодом SQLite — для кодів, які складно відтворити."""
    error = cls(message)
    error.sqlite_errorcode = code
    return error


def corrupted_database(tmp_path) -> sqlite3.Connection:
    """Справжнє пошкодження сторінок файла при відкритому з'єднанні."""
    path = tmp_path / "budget.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, payload TEXT)")
    connection.executemany("INSERT INTO t (payload) VALUES (?)", [("x" * 400,)] * 200)
    connection.commit()
    connection.execute("PRAGMA cache_size = 0")
    pages = path.stat().st_size // 4096
    with path.open("r+b") as handle:
        for page in range(2, pages):
            handle.seek(page * 4096)
            handle.write(b"\xa5" * 4096)
    return connection


# Пошкодження ---------------------------------------------------------------------------------


def test_real_sqlite_corrupt_is_corruption(tmp_path):
    connection = corrupted_database(tmp_path)
    error = raised(lambda: connection.execute("SELECT * FROM t").fetchall())
    connection.close()
    assert error.sqlite_errorcode & 0xFF == sqlite3.SQLITE_CORRUPT
    assert is_corruption_error(error) and corruption_code(error) == error.sqlite_errorcode


def test_real_sqlite_notadb_is_corruption(tmp_path):
    path = tmp_path / "garbage.db"
    path.write_bytes(b"not a database" * 600)
    connection = sqlite3.connect(path)
    error = raised(lambda: connection.execute("SELECT * FROM sqlite_master").fetchall())
    connection.close()
    assert error.sqlite_errorcode == sqlite3.SQLITE_NOTADB
    assert is_corruption_error(error)


@pytest.mark.parametrize(
    "code",
    [sqlite3.SQLITE_CORRUPT_VTAB, sqlite3.SQLITE_CORRUPT_SEQUENCE, sqlite3.SQLITE_CORRUPT_INDEX],
)
def test_extended_corruption_codes_use_primary_code(code):
    # Python 3.12 повертає розширені коди (напр., IntegrityError — 1299 NOT NULL).
    assert code & 0xFF == sqlite3.SQLITE_CORRUPT
    assert corruption_code(with_code(sqlite3.DatabaseError, code)) == code


def test_corruption_hidden_behind_a_failed_rollback_is_found():
    try:
        try:
            raise with_code(sqlite3.DatabaseError, sqlite3.SQLITE_CORRUPT)
        except sqlite3.DatabaseError:
            raise with_code(sqlite3.OperationalError, sqlite3.SQLITE_ERROR) from None
    except sqlite3.OperationalError as error:
        outer = error
    # «from None» прибирає лише показ контексту; сам контекст лишається.
    assert outer.__context__ is not None
    assert is_corruption_error(outer)


# Не пошкодження ------------------------------------------------------------------------------


def test_real_ordinary_sqlite_errors_are_not_corruption(tmp_path):
    memory = sqlite3.connect(":memory:")
    memory.execute("CREATE TABLE u (x NOT NULL UNIQUE)")
    memory.execute("INSERT INTO u VALUES (1)")
    errors = {
        "SQLITE_ERROR": raised(lambda: memory.execute("SELEC 1")),
        "SQLITE_CONSTRAINT_NOTNULL": raised(lambda: memory.execute("INSERT INTO u VALUES (NULL)")),
        "SQLITE_CONSTRAINT_UNIQUE": raised(lambda: memory.execute("INSERT INTO u VALUES (1)")),
    }
    path = tmp_path / "locked.db"
    owner = sqlite3.connect(path, isolation_level=None, timeout=0)
    owner.execute("CREATE TABLE t (x)")
    owner.execute("BEGIN EXCLUSIVE")
    other = sqlite3.connect(path, timeout=0)
    errors["SQLITE_BUSY"] = raised(lambda: other.execute("SELECT * FROM t").fetchall())
    owner.execute("ROLLBACK")
    reader = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    errors["SQLITE_READONLY"] = raised(lambda: reader.execute("INSERT INTO t VALUES (1)"))
    for connection in (memory, owner, other, reader):
        connection.close()
    for name, error in errors.items():
        assert error.sqlite_errorname == name
        assert not is_corruption_error(error), name


@pytest.mark.parametrize(
    "code",
    [
        sqlite3.SQLITE_ERROR,
        sqlite3.SQLITE_INTERNAL,
        sqlite3.SQLITE_PERM,
        sqlite3.SQLITE_ABORT,
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
        sqlite3.SQLITE_NOMEM,
        sqlite3.SQLITE_READONLY,
        sqlite3.SQLITE_INTERRUPT,
        sqlite3.SQLITE_IOERR,
        sqlite3.SQLITE_IOERR_WRITE,
        sqlite3.SQLITE_FULL,
        sqlite3.SQLITE_CANTOPEN,
        sqlite3.SQLITE_CONSTRAINT,
        sqlite3.SQLITE_MISMATCH,
    ],
)
def test_non_corruption_codes(code):
    assert not is_corruption_error(with_code(sqlite3.OperationalError, code))


def test_message_text_is_not_a_criterion():
    # Текст про пошкодження, але код — «база заблокована».
    locked = with_code(
        sqlite3.OperationalError, sqlite3.SQLITE_BUSY, "database disk image is malformed"
    )
    assert not is_corruption_error(locked)
    # Текст про блокування, але код — пошкодження.
    corrupt = with_code(sqlite3.DatabaseError, sqlite3.SQLITE_CORRUPT, "database is locked")
    assert is_corruption_error(corrupt)


def test_errors_without_sqlite_code_are_not_corruption():
    assert not is_corruption_error(sqlite3.DatabaseError("database disk image is malformed"))
    assert not is_corruption_error(ValueError("malformed"))
    assert not is_corruption_error(RuntimeError("SQLITE_CORRUPT"))
    # Код на не-SQLite винятку не враховується.
    stray = ValueError("x")
    stray.sqlite_errorcode = sqlite3.SQLITE_CORRUPT
    assert not is_corruption_error(stray)
