"""Явні транзакції (DS-2): відкат лише активної транзакції, первинний виняток — основний.

Розділи:

* справжній SQLite (усі платформи): успіх, помилка тіла, невдалий ``COMMIT`` через
  відкладене FK-порушення (транзакція лишається активною), стан «SQLite уже відкотила»;
* справжня помилка диска (лише Linux, окремий процес з ``RLIMIT_FSIZE`` на тимчасових
  файлах): SQLite відкочує транзакцію сама — без зайвого ``ROLLBACK``;
* ін'єкція (проксі-з'єднання): невдалий ``ROLLBACK`` — звичайна помилка чи пошкодження.
"""

import json
import logging
import sqlite3
import subprocess
import sys
import textwrap

import pytest

from budget.errors import DomainRuleError
from budget.services.read_errors import read_failure, write_failure
from budget.storage.integrity import corruption_code
from budget.storage.transaction import transaction


def with_code(cls, code: int, message: str = "введена помилка") -> sqlite3.Error:
    error = cls(message)
    error.sqlite_errorcode = code
    return error


@pytest.fixture
def connection(tmp_path):
    """Режим, як у застосунку: autocommit модуля sqlite3, WAL, FULL, зовнішні ключі."""
    connection = sqlite3.connect(tmp_path / "t.db", isolation_level=None)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
    connection.execute(
        "CREATE TABLE child (parent_id INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)"
    )
    connection.execute("CREATE TABLE t (x)")
    yield connection
    connection.close()


def rows(connection) -> int:
    return connection.execute("SELECT count(*) FROM t").fetchone()[0]


# Справжній SQLite --------------------------------------------------------------------------


def test_successful_commit_keeps_the_data(connection):
    with transaction(connection):
        connection.execute("INSERT INTO t VALUES (1)")
    assert rows(connection) == 1 and not connection.in_transaction


@pytest.mark.parametrize(
    "error",
    [RuntimeError("bug"), DomainRuleError("правило"), KeyboardInterrupt()],
    ids=["RuntimeError", "DomainRuleError", "KeyboardInterrupt"],
)
def test_error_in_body_rolls_back_and_reraises_the_same_object(connection, error):
    with pytest.raises(type(error)) as raised, transaction(connection):
        connection.execute("INSERT INTO t VALUES (1)")
        raise error
    assert raised.value is error and not getattr(raised.value, "__notes__", None)
    assert rows(connection) == 0 and not connection.in_transaction
    with transaction(connection):  # з'єднання придатне
        connection.execute("INSERT INTO t VALUES (2)")
    assert rows(connection) == 1


def test_failed_commit_with_active_transaction_is_rolled_back(connection):
    """Відкладене FK-порушення: ``COMMIT`` падає, а SQLite лишає транзакцію активною."""
    with pytest.raises(sqlite3.IntegrityError) as raised, transaction(connection):
        connection.execute("INSERT INTO t VALUES (1)")
        connection.execute("INSERT INTO child VALUES (42)")  # перевіряється лише на COMMIT
    assert "FOREIGN KEY" in str(raised.value)
    assert not connection.in_transaction  # не «застрягла»
    assert rows(connection) == 0  # нічого з тієї транзакції не записано
    with transaction(connection):  # наступна незалежна транзакція працює
        connection.execute("INSERT INTO t VALUES (2)")
    assert rows(connection) == 1


def test_without_the_fix_a_failed_commit_leaves_the_connection_stuck(connection):
    """Контрольний дослід (без ``transaction``): саме цей стан виправлено."""
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("INSERT INTO child VALUES (42)")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("COMMIT")
    assert connection.in_transaction
    with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
        connection.execute("BEGIN IMMEDIATE")
    connection.execute("ROLLBACK")


def test_already_rolled_back_transaction_is_not_rolled_back_again(connection):
    """Як після I/O-помилки: SQLite відкотила сама, далі — первинний виняток, а не
    «cannot rollback - no transaction is active»."""
    error = with_code(sqlite3.OperationalError, sqlite3.SQLITE_IOERR, "disk I/O error")
    with pytest.raises(sqlite3.OperationalError) as raised, transaction(connection):
        connection.execute("INSERT INTO t VALUES (1)")
        connection.execute("ROLLBACK")  # стан, який лишає SQLite після такої помилки
        raise error
    assert raised.value is error and raised.value.__context__ is None
    assert not getattr(raised.value, "__notes__", None)
    assert rows(connection) == 0 and not connection.in_transaction


# Класифікація не змінюється -------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        with_code(sqlite3.DatabaseError, sqlite3.SQLITE_CORRUPT, "malformed"),
        with_code(sqlite3.DatabaseError, sqlite3.SQLITE_NOTADB, "not a database"),
        RuntimeError("bug"),
        DomainRuleError("правило"),
    ],
    ids=["CORRUPT", "NOTADB", "RuntimeError", "DomainRuleError"],
)
def test_corruption_and_foreign_errors_are_not_ordinary_write_failures(connection, error):
    with pytest.raises(type(error)) as raised, transaction(connection):
        raise error
    assert raised.value is error
    assert write_failure(raised.value) is None and read_failure(raised.value) is None


def test_ordinary_error_is_still_an_ordinary_write_failure(connection):
    error = with_code(sqlite3.OperationalError, sqlite3.SQLITE_FULL, "database or disk is full")
    with pytest.raises(sqlite3.OperationalError) as raised, transaction(connection):
        raise error
    assert raised.value is error and write_failure(raised.value).__cause__ is error


# Справжня помилка диска (Linux, окремий процес) ------------------------------------------------

DISK_FULL_SCRIPT = textwrap.dedent(
    """
    import json, os, resource, signal, sqlite3, sys
    from budget.storage.transaction import transaction

    mode, folder = sys.argv[1], sys.argv[2]
    path = os.path.join(folder, mode + ".db")
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)  # EFBIG замість завершення процесу
    c = sqlite3.connect(path, isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA synchronous=FULL")
    c.execute("CREATE TABLE t(x)"); c.execute("INSERT INTO t VALUES ('seed')")
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    if mode == "body":
        c.execute("PRAGMA cache_size=1")  # сторінки йдуть у WAL ще під час вставок
    limit = os.path.getsize(path) + 64 * 1024
    # Лише м'яке обмеження цього процесу й лише файлів, які він пише.
    resource.setrlimit(resource.RLIMIT_FSIZE, (limit, resource.RLIM_INFINITY))
    stage = "body"
    result = {}
    try:
        with transaction(c):
            for _ in range(200):
                c.execute("INSERT INTO t VALUES (?)", (os.urandom(8000),))
            stage = "commit"
    except sqlite3.Error as e:
        result = {
            "stage": stage,
            "type": type(e).__name__,
            "code": getattr(e, "sqlite_errorcode", None),
            "message": str(e),
            "context": repr(e.__context__),
            "notes": getattr(e, "__notes__", []),
        }
    resource.setrlimit(resource.RLIMIT_FSIZE, (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
    result["in_transaction"] = c.in_transaction
    with transaction(c):
        c.execute("INSERT INTO t VALUES ('after')")
    result["rows"] = c.execute("SELECT count(*) FROM t").fetchone()[0]
    result["integrity"] = c.execute("PRAGMA integrity_check").fetchone()[0]
    c.close()
    print(json.dumps(result))
    """
)


@pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_FSIZE — лише Linux")
@pytest.mark.parametrize("mode", ["commit", "body"])
def test_real_disk_error_is_reported_without_a_second_rollback(tmp_path, mode):
    """Справжня помилка I/O: SQLite відкочує транзакцію сама. Первинна помилка
    піднімається без підміни на «cannot rollback», з'єднання придатне, база ціла."""
    script = tmp_path / "disk_full.py"
    script.write_text(DISK_FULL_SCRIPT, encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    completed = subprocess.run(
        [sys.executable, str(script), mode, str(data)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=data,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["stage"] == mode  # помилка саме там, де очікувалось
    assert result["type"] == "OperationalError"
    assert result["code"] & 0xFF == sqlite3.SQLITE_IOERR  # первинна, а не SQLITE_ERROR відкату
    assert "cannot rollback" not in result["message"] and result["notes"] == []
    assert result["in_transaction"] is False
    assert result["rows"] == 2 and result["integrity"] == "ok"  # seed + after, без часткового


# Ін'єкція: невдалий ROLLBACK ---------------------------------------------------------------------


class FailingRollback:
    """Проксі справжнього з'єднання: ``ROLLBACK`` кидає задану помилку (транзакція при
    цьому лишається активною — як буває, коли відкат не вдався)."""

    def __init__(self, connection: sqlite3.Connection, failure: BaseException) -> None:
        self._connection = connection
        self._failure = failure
        self.statements: list[str] = []

    @property
    def in_transaction(self) -> bool:
        return self._connection.in_transaction

    def execute(self, sql: str, *args):
        self.statements.append(sql)
        if sql == "ROLLBACK":
            raise self._failure
        return self._connection.execute(sql, *args)


def test_failed_rollback_keeps_the_primary_error_with_diagnostics(connection, caplog):
    rollback_error = with_code(sqlite3.OperationalError, sqlite3.SQLITE_IOERR, "rollback I/O")
    proxy = FailingRollback(connection, rollback_error)
    primary = RuntimeError("первинна помилка")
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError) as raised:
        with transaction(proxy):
            raise primary
    assert raised.value is primary  # основний — первинний
    assert raised.value.__notes__ == ["ROLLBACK також не вдався: OperationalError: rollback I/O"]
    (record,) = [r for r in caplog.records if r.name == "budget.storage.transaction"]
    assert record.exc_info[1] is rollback_error  # вторинна — у журналі з traceback
    assert proxy.statements == ["BEGIN IMMEDIATE", "ROLLBACK"]
    connection.execute("ROLLBACK")


def test_failed_rollback_after_a_failed_commit_keeps_the_commit_error(connection):
    rollback_error = with_code(sqlite3.OperationalError, sqlite3.SQLITE_BUSY, "busy")
    proxy = FailingRollback(connection, rollback_error)
    with pytest.raises(sqlite3.IntegrityError) as raised, transaction(proxy):
        connection.execute("INSERT INTO child VALUES (42)")
    assert "FOREIGN KEY" in str(raised.value)
    assert raised.value.__notes__ == ["ROLLBACK також не вдався: OperationalError: busy"]
    assert proxy.statements == ["BEGIN IMMEDIATE", "COMMIT", "ROLLBACK"]
    connection.execute("ROLLBACK")


def test_corruption_found_by_rollback_reaches_the_guard(connection):
    """D2-1: пошкодження, виявлене саме відкатом, — далі воно; первинна — у ланцюжку."""
    corrupted = with_code(sqlite3.DatabaseError, sqlite3.SQLITE_CORRUPT, "malformed")
    proxy = FailingRollback(connection, corrupted)
    primary = with_code(sqlite3.OperationalError, sqlite3.SQLITE_FULL, "disk is full")
    with pytest.raises(sqlite3.DatabaseError) as raised, transaction(proxy):
        raise primary
    assert raised.value is corrupted and raised.value.__context__ is primary
    assert corruption_code(raised.value) is not None
    assert write_failure(raised.value) is None  # не «звичайна помилка запису»
    connection.execute("ROLLBACK")


def test_primary_corruption_stays_primary_when_rollback_also_fails(connection):
    """Первинний виняток уже є пошкодженням — він і лишається основним (guard)."""
    rollback_error = with_code(sqlite3.OperationalError, sqlite3.SQLITE_IOERR, "rollback I/O")
    proxy = FailingRollback(connection, rollback_error)
    primary = with_code(sqlite3.DatabaseError, sqlite3.SQLITE_CORRUPT, "malformed")
    with pytest.raises(sqlite3.DatabaseError) as raised, transaction(proxy):
        raise primary
    assert raised.value is primary and corruption_code(raised.value) is not None
    connection.execute("ROLLBACK")


def test_no_rollback_statement_when_sqlite_already_rolled_back(connection):
    proxy = FailingRollback(connection, AssertionError("ROLLBACK не мав виконуватися"))
    error = with_code(sqlite3.OperationalError, sqlite3.SQLITE_IOERR, "disk I/O error")
    with pytest.raises(sqlite3.OperationalError) as raised, transaction(proxy):
        connection.execute("ROLLBACK")  # SQLite уже відкотила
        raise error
    assert raised.value is error
    assert proxy.statements == ["BEGIN IMMEDIATE"]
