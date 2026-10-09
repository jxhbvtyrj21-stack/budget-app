"""Невдала міграція (M1, M2/F5): відкат лише активної транзакції, первинна помилка не
підміняється, пошкодження — ``DatabaseCorruptedError``.

Розділи:

* справжній SQLite: помилка SQL у тілі, невдалий ``COMMIT`` через відкладене FK-порушення,
  ``SQLITE_BUSY`` на ``BEGIN`` (без зайвого ``ROLLBACK``), справжнє ``SQLITE_CORRUPT`` усередині
  міграції (``writable_schema`` указує таблицю на неіснуючу сторінку; до міграції база ціла,
  після відкату — знову ціла);
* ін'єкція (підклас ``sqlite3.Connection`` через ``factory=``): первинна помилка після
  ``BEGIN`` і/або невдалий ``ROLLBACK`` — детерміновано не відтворюється справжнім SQLite;
* межа запуску: ``prepare_database`` (закриття без checkpoint лише для пошкодження) і
  ``start_session`` (звичайна помилка — повідомлення й код 2, пошкодження — карантин і діалог).

Справжня помилка диска (SQLite відкочує транзакцію сама) тут не дублюється: її покриває
``tests/storage/test_transaction.py``, а ``migrate`` у цьому разі йде тією самою гілкою
«транзакція вже неактивна», що й ``SQLITE_BUSY`` на ``BEGIN`` нижче.
"""

import hashlib
import logging
import sqlite3
from datetime import UTC, datetime

import pytest

import budget.app as app_module
import budget.storage.migrations as migrations
from budget.app import ApplicationSession, open_application_database, start_session
from budget.domain.calendar import FixedClock
from budget.errors import DatabaseCorruptedError, StorageError
from budget.platform.identity import load_product_identity
from budget.platform.paths import DataPaths
from budget.services.startup import prepare_database
from budget.storage.database import close_without_checkpoint, open_database
from budget.storage.integrity import corruption_code, integrity_check
from budget.storage.migrations import migrate
from budget.storage.recovery import database_files
from budget.ui.dialogs.recovery_dialog import RecoveryDialog

# Справжнє SQLITE_CORRUPT усередині міграції: таблиця вказує на сторінку за межами файлу.
CORRUPTING_SQL = """
CREATE TABLE probe (a);
INSERT INTO probe VALUES (1);
PRAGMA writable_schema = ON;
UPDATE sqlite_master SET rootpage = 999999 WHERE name = 'probe';
PRAGMA writable_schema = RESET;
SELECT * FROM probe;
"""
FAILING_SQL = "CREATE TABLE probe (a);\nSELECT * FROM no_such_table;"
DEFERRED_FK_SQL = """
CREATE TABLE probe (id INTEGER PRIMARY KEY);
CREATE TABLE probe_child (
    probe_id INTEGER REFERENCES probe(id) DEFERRABLE INITIALLY DEFERRED
);
INSERT INTO probe_child VALUES (5);
"""


def with_code(cls, code: int, message: str = "введена помилка") -> sqlite3.Error:
    error = cls(message)
    error.sqlite_errorcode = code
    return error


def ordinary() -> sqlite3.Error:
    return with_code(sqlite3.OperationalError, sqlite3.SQLITE_FULL, "database or disk is full")


def pending(monkeypatch, sql: str) -> None:
    """Наступна міграція 2 з заданим SQL (``migrate`` бере перелік із модуля)."""
    monkeypatch.setattr(migrations, "MIGRATIONS", (*migrations.MIGRATIONS, (2, sql)))
    monkeypatch.setattr(migrations, "LATEST_VERSION", 2)


def committed(path) -> tuple[int, int]:
    """Зафіксований стан з окремого з'єднання: версія схеми й кількість об'єктів ``probe*``."""
    other = sqlite3.connect(path)
    try:
        version = other.execute("PRAGMA user_version").fetchone()[0]
        probes = other.execute(
            "SELECT count(*) FROM sqlite_master WHERE name LIKE 'probe%'"
        ).fetchone()[0]
    finally:
        other.close()
    return version, probes


def chain(error: BaseException) -> list[BaseException]:
    found, current = [], error
    while current is not None and current not in found:
        found.append(current)
        current = current.__cause__ or current.__context__
    return found


# Справжній SQLite --------------------------------------------------------------------------


@pytest.fixture
def statements(connection):
    """Усі інструкції, які виконало з'єднання (зокрема всередині ``executescript``)."""
    executed: list[str] = []
    connection.set_trace_callback(executed.append)
    yield executed
    connection.set_trace_callback(None)


def test_successful_migration_is_unchanged(connection, db_path, monkeypatch):
    pending(monkeypatch, "CREATE TABLE probe (a);")
    assert migrate(connection) == [2]
    assert committed(db_path) == (2, 1) and not connection.in_transaction


def test_sql_error_is_storage_error_and_the_migration_is_rolled_back(
    connection, db_path, monkeypatch, statements
):
    pending(monkeypatch, FAILING_SQL)
    with pytest.raises(StorageError) as raised:
        migrate(connection)
    error = raised.value
    assert type(error) is StorageError
    assert isinstance(error.__cause__, sqlite3.OperationalError)
    assert error.__cause__.sqlite_errorcode == sqlite3.SQLITE_ERROR
    assert "Міграція 2 не вдалася" in error.detail and "no_such_table" in error.detail
    assert "ROLLBACK" in statements  # транзакція була активна — відкат потрібен
    assert not connection.in_transaction
    assert committed(db_path) == (1, 0)  # стара версія, часткових об'єктів немає
    assert not getattr(error.__cause__, "__notes__", None)


def test_failed_commit_by_deferred_foreign_key_is_rolled_back(connection, db_path, monkeypatch):
    pending(monkeypatch, DEFERRED_FK_SQL)
    with pytest.raises(StorageError) as raised:
        migrate(connection)
    assert type(raised.value) is StorageError
    assert isinstance(raised.value.__cause__, sqlite3.IntegrityError)
    assert not connection.in_transaction  # COMMIT лишив транзакцію активною — її відкочено
    assert committed(db_path) == (1, 0)
    connection.execute("BEGIN IMMEDIATE")  # з'єднання придатне до наступної транзакції
    connection.execute("ROLLBACK")


def test_busy_at_begin_is_storage_error_without_a_rollback(
    connection, db_path, monkeypatch, statements
):
    pending(monkeypatch, "CREATE TABLE probe (a);")
    connection.execute("PRAGMA busy_timeout = 0")  # без очікування блокування
    holder = sqlite3.connect(db_path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(StorageError) as raised:
            migrate(connection)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert type(raised.value) is StorageError
    assert raised.value.__cause__.sqlite_errorcode == sqlite3.SQLITE_BUSY
    assert "ROLLBACK" not in statements  # транзакція не почалася — відкочувати нічого
    assert not connection.in_transaction
    assert committed(db_path) == (1, 0)


def test_real_corruption_inside_a_migration_is_database_corrupted_error(
    connection, db_path, monkeypatch
):
    assert integrity_check(connection)
    pending(monkeypatch, CORRUPTING_SQL)
    with pytest.raises(DatabaseCorruptedError) as raised:
        migrate(connection)
    cause = raised.value.__cause__
    assert isinstance(cause, sqlite3.DatabaseError)
    assert cause.sqlite_errorcode & 0xFF == sqlite3.SQLITE_CORRUPT
    assert corruption_code(raised.value) == cause.sqlite_errorcode
    assert not connection.in_transaction
    assert committed(db_path) == (1, 0)
    assert integrity_check(connection)  # пошкодження жило лише у відкоченій транзакції


# Ін'єкція: первинна помилка й невдалий ROLLBACK ---------------------------------------------


class FailingConnection(sqlite3.Connection):
    """Справжнє з'єднання із заданими збоями. ``primary`` кидається з ``executescript`` уже
    після ``BEGIN IMMEDIATE`` (транзакція активна, як після помилки в тілі міграції);
    ``rollback_failure`` — на ``ROLLBACK`` (транзакція при цьому лишається активною)."""

    primary: BaseException | None = None
    rollback_failure: sqlite3.Error | None = None

    def executescript(self, script):
        if self.primary is None:
            return super().executescript(script)
        super().execute("BEGIN IMMEDIATE")
        raise self.primary

    def execute(self, sql, *args):
        if sql == "ROLLBACK" and self.rollback_failure is not None:
            raise self.rollback_failure
        return super().execute(sql, *args)


@pytest.fixture
def faulty(tmp_path, monkeypatch):
    path = tmp_path / "f.db"
    connection = sqlite3.connect(path, isolation_level=None, factory=FailingConnection)
    connection.execute("PRAGMA journal_mode=WAL")
    migrate(connection)
    pending(monkeypatch, "CREATE TABLE probe (a);")
    yield connection
    connection.rollback_failure = None
    if connection.in_transaction:
        connection.execute("ROLLBACK")
    connection.close()


def fail(faulty, primary, rollback_failure=None):
    faulty.primary, faulty.rollback_failure = primary, rollback_failure
    with pytest.raises(BaseException) as raised:
        migrate(faulty)
    return raised.value


def rollback_records(caplog):
    return [r for r in caplog.records if r.name == migrations.__name__ and "ROLLBACK" in r.message]


def test_successful_rollback_keeps_the_primary_error_as_cause(faulty):
    primary = ordinary()
    error = fail(faulty, primary)
    assert type(error) is StorageError and error.__cause__ is primary
    assert not faulty.in_transaction and "ROLLBACK" not in error.detail


def test_failed_rollback_does_not_replace_the_primary_error(faulty, caplog):
    primary = ordinary()
    failure = with_code(sqlite3.OperationalError, sqlite3.SQLITE_IOERR, "rollback I/O")
    with caplog.at_level(logging.ERROR, logger=migrations.__name__):
        error = fail(faulty, primary, failure)
    assert type(error) is StorageError and error.__cause__ is primary
    assert "ROLLBACK також не вдався" in error.detail and "rollback I/O" in error.detail
    assert any("rollback I/O" in note for note in primary.__notes__)
    [record] = rollback_records(caplog)
    assert record.exc_info[1] is failure  # трасування помилки відкату — у журналі
    # Невдалий відкат не гарантує закриття транзакції: її закриває власник з'єднання.
    assert faulty.in_transaction


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(with_code(sqlite3.DatabaseError, sqlite3.SQLITE_CORRUPT), id="CORRUPT"),
        pytest.param(with_code(sqlite3.OperationalError, sqlite3.SQLITE_NOTADB), id="NOTADB"),
        pytest.param(with_code(sqlite3.DatabaseError, 779), id="CORRUPT_INDEX"),
    ],
)
def test_corruption_found_by_rollback_is_database_corrupted_error(faulty, caplog, failure):
    """Q1: первинна помилка звичайна, пошкодження виявив відкат — ``DatabaseCorruptedError``."""
    primary = ordinary()
    with caplog.at_level(logging.ERROR, logger=migrations.__name__):
        error = fail(faulty, primary, failure)
    assert type(error) is DatabaseCorruptedError
    assert error.__cause__ is failure and failure.__context__ is primary  # обидві доступні
    assert chain(error)[:3] == [error, failure, primary]
    assert corruption_code(error) == failure.sqlite_errorcode
    assert "database or disk is full" in error.detail and "ROLLBACK" in error.detail
    assert len(rollback_records(caplog)) == 1


@pytest.mark.parametrize(
    "primary",
    [
        pytest.param(with_code(sqlite3.DatabaseError, sqlite3.SQLITE_CORRUPT), id="CORRUPT"),
        pytest.param(with_code(sqlite3.OperationalError, sqlite3.SQLITE_NOTADB), id="NOTADB"),
    ],
)
def test_primary_corruption_is_database_corrupted_error(faulty, primary):
    error = fail(faulty, primary)
    assert type(error) is DatabaseCorruptedError and error.__cause__ is primary
    assert not faulty.in_transaction


def test_primary_corruption_stays_the_cause_when_rollback_also_fails(faulty):
    primary = with_code(sqlite3.DatabaseError, sqlite3.SQLITE_CORRUPT)
    failure = with_code(sqlite3.OperationalError, sqlite3.SQLITE_IOERR, "rollback I/O")
    error = fail(faulty, primary, failure)
    assert type(error) is DatabaseCorruptedError and error.__cause__ is primary
    assert "rollback I/O" in error.detail


@pytest.mark.parametrize(
    "primary",
    [
        pytest.param(with_code(sqlite3.OperationalError, sqlite3.SQLITE_BUSY), id="BUSY"),
        pytest.param(with_code(sqlite3.OperationalError, sqlite3.SQLITE_IOERR), id="IOERR"),
        pytest.param(with_code(sqlite3.IntegrityError, 787), id="FOREIGNKEY"),
        pytest.param(with_code(sqlite3.DatabaseError, sqlite3.SQLITE_ERROR), id="ERROR"),
    ],
)
@pytest.mark.parametrize("rollback_fails", [False, True], ids=["rollback-ok", "rollback-fails"])
def test_ordinary_sqlite_errors_are_storage_errors_never_raw(faulty, primary, rollback_fails):
    """Жодна помилка SQLite не виходить із ``migrate`` сирою (M1) і не стає пошкодженням."""
    failure = ordinary() if rollback_fails else None
    error = fail(faulty, primary, failure)
    assert type(error) is StorageError and error.__cause__ is primary
    assert corruption_code(error) is None


@pytest.mark.parametrize(
    "primary", [KeyboardInterrupt(), RuntimeError("помилка в коді")], ids=["interrupt", "bug"]
)
def test_other_exceptions_are_rolled_back_and_raised_unchanged(faulty, primary):
    """Q2: відкат активної транзакції, далі — той самий об'єкт, без перекласифікації."""
    error = fail(faulty, primary)
    assert error is primary and not getattr(error, "__notes__", None)
    assert not faulty.in_transaction
    assert committed(faulty.execute("PRAGMA database_list").fetchone()[2]) == (1, 0)


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(with_code(sqlite3.OperationalError, sqlite3.SQLITE_IOERR, "rb"), id="IOERR"),
        pytest.param(with_code(sqlite3.DatabaseError, sqlite3.SQLITE_CORRUPT, "rb"), id="CORRUPT"),
    ],
)
def test_failed_rollback_after_another_exception_keeps_that_exception(faulty, caplog, failure):
    """Q2: виняток не від SQLite лишається зовнішнім і не стає помилкою сховища; помилка
    відкату — у нотатці й журналі."""
    primary = RuntimeError("помилка в коді")
    with caplog.at_level(logging.ERROR, logger=migrations.__name__):
        error = fail(faulty, primary, failure)
    assert error is primary
    assert any("ROLLBACK також не вдався" in note for note in error.__notes__)
    [record] = rollback_records(caplog)
    assert record.exc_info[1] is failure


# Межа запуску ------------------------------------------------------------------------------


START = datetime(2026, 10, 9, 9, 0, tzinfo=UTC)


def digest(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def wal_only_database(tmp_path):
    """База версії 1, остання зафіксована зміна якої лежить лише у WAL."""
    path = tmp_path / "data" / "budget.db"
    connection = open_database(path)
    migrate(connection)
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.execute("UPDATE general_remainder SET balance = 4242")  # лише у WAL
    close_without_checkpoint(connection)
    assert database_files(path)[1].stat().st_size > 0
    return path


def test_prepare_database_closes_without_checkpoint_on_corruption(wal_only_database, monkeypatch):
    path = wal_only_database
    before = digest(path)
    pending(monkeypatch, CORRUPTING_SQL)
    with pytest.raises(DatabaseCorruptedError):
        prepare_database(path, path.parent / "backups", FixedClock(START))
    assert digest(path) == before  # кадри WAL не перенесено в основний файл
    assert database_files(path)[1].exists()


def test_prepare_database_keeps_ordinary_close_on_other_errors(wal_only_database, monkeypatch):
    path = wal_only_database
    before = digest(path)
    pending(monkeypatch, FAILING_SQL)
    with pytest.raises(StorageError) as raised:
        prepare_database(path, path.parent / "backups", FixedClock(START))
    assert not isinstance(raised.value, DatabaseCorruptedError)
    assert digest(path) != before  # звичайне закриття з checkpoint, як і раніше
    assert not database_files(path)[1].exists()
    assert committed(path) == (1, 0)


@pytest.fixture
def startup(tmp_path, monkeypatch):
    """Наявна база версії 1 і перехоплені повідомлення запуску."""
    paths = DataPaths(tmp_path / "Мої дані" / "Budget")
    clock = FixedClock(START)
    open_application_database(paths, clock).close()
    messages: list[str] = []
    monkeypatch.setattr(app_module, "_show_message", lambda title, text: messages.append(text))
    return paths, clock, messages


def test_start_session_reports_an_ordinary_migration_failure(startup, monkeypatch):
    paths, clock, messages = startup
    pending(monkeypatch, FAILING_SQL)
    session = ApplicationSession(paths, clock)
    result = start_session(load_product_identity(), session, paths, clock)
    assert result == (app_module.EXIT_STARTUP_FAILED, False)
    assert messages == [StorageError.default_message] and not session.is_open
    assert paths.database.exists() and committed(paths.database) == (1, 0)


def test_start_session_sends_migration_corruption_to_recovery(qtbot, startup, monkeypatch):
    paths, clock, messages = startup
    pending(monkeypatch, CORRUPTING_SQL)
    shown = []
    monkeypatch.setattr(RecoveryDialog, "exec", lambda self: shown.append(self) or 0)
    session = ApplicationSession(paths, clock)
    result = start_session(load_product_identity(), session, paths, clock)
    assert result == (app_module.EXIT_DATA_CORRUPTED, False)
    assert len(shown) == 1 and messages == [] and not session.is_open
    assert not paths.database.exists()  # наявний шлях DS-6: карантин і діалог відновлення
    assert [p for p in paths.root.iterdir() if p.name.startswith("budget.db.corrupted-")]
