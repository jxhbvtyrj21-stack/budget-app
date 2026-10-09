"""Помилка запису майстра (IA 12, «Помилка збереження»): класифікація в сервісі.

Звичайна ``sqlite3.OperationalError`` без пошкодження — ``DataWriteError`` (той самий
початковий виняток у ``__cause__``); пошкодження й усе інше — той самий об'єкт далі.
"""

import sqlite3

import pytest

from budget.domain.money import Money
from budget.errors import DataReadError, DataWriteError, DomainRuleError, StorageError
from budget.services.read_errors import read_failure, write_failure
from budget.services.setup import InitialAccumulation, InitialSetupService, SetupDraft
from budget.storage.integrity import corruption_code
from budget.storage.repositories import AccumulationRepository
from budget.storage.setup_repository import SetupStateRepository


def with_code(cls, code: int, message: str = "введена помилка") -> sqlite3.Error:
    error = cls(message)
    error.sqlite_errorcode = code
    return error


def ordinary() -> sqlite3.Error:
    return with_code(sqlite3.OperationalError, sqlite3.SQLITE_FULL, "database or disk is full")


def corrupted() -> sqlite3.Error:
    return with_code(sqlite3.DatabaseError, sqlite3.SQLITE_CORRUPT, "malformed")


DRAFT = SetupDraft(
    general_remainder=Money(70_000),
    accumulations=(InitialAccumulation("Подорож", None, Money(30_000)),),
)


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(ordinary(), id="FULL"),
        pytest.param(with_code(sqlite3.OperationalError, sqlite3.SQLITE_BUSY), id="BUSY"),
        pytest.param(with_code(sqlite3.OperationalError, sqlite3.SQLITE_IOERR), id="IOERR"),
        pytest.param(corrupted(), id="CORRUPT"),
        pytest.param(with_code(sqlite3.OperationalError, sqlite3.SQLITE_NOTADB), id="NOTADB"),
        pytest.param(with_code(sqlite3.DatabaseError, sqlite3.SQLITE_ERROR), id="DatabaseError"),
        pytest.param(with_code(sqlite3.IntegrityError, 19), id="IntegrityError"),
        pytest.param(RuntimeError("bug"), id="RuntimeError"),
    ],
)
def test_write_and_read_use_the_same_rule(error):
    """Одне правило для читання й запису: звичайна OperationalError без пошкодження."""
    write, read = write_failure(error), read_failure(error)
    assert (write is None) == (read is None)
    if write is not None:
        assert isinstance(write, DataWriteError) and isinstance(write, StorageError)
        assert not isinstance(write, DataReadError) and write.__cause__ is error
        assert corruption_code(error) is None


def inject(monkeypatch, owner, name, error):
    def failing(*args, **kwargs):
        raise error

    monkeypatch.setattr(owner, name, failing)


@pytest.mark.parametrize(
    ("action", "owner", "name"),
    [
        pytest.param(lambda s: s.save_draft(DRAFT), SetupStateRepository, "save_draft", id="save"),
        pytest.param(lambda s: s.reset(), SetupStateRepository, "clear_draft", id="reset"),
        pytest.param(lambda s: s.complete(DRAFT), AccumulationRepository, "insert", id="complete"),
    ],
)
def test_ordinary_write_failure_becomes_data_write_error(
    db, clock, monkeypatch, action, owner, name
):
    service = InitialSetupService(db, clock)
    service.save_draft(DRAFT)
    error = ordinary()
    inject(monkeypatch, owner, name, error)
    with pytest.raises(DataWriteError) as raised:
        action(service)
    assert raised.value.__cause__ is error
    assert raised.value.user_message == "Не вдалося зберегти дані."
    assert not db.in_transaction  # транзакцію відкочено


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(corrupted(), id="CORRUPT"),
        pytest.param(RuntimeError("bug"), id="RuntimeError"),
        pytest.param(with_code(sqlite3.IntegrityError, 19), id="IntegrityError"),
    ],
)
def test_other_errors_pass_through_unchanged(db, clock, monkeypatch, error):
    service = InitialSetupService(db, clock)
    inject(monkeypatch, SetupStateRepository, "save_draft", error)
    with pytest.raises(type(error)) as raised:
        service.save_draft(DRAFT)
    assert raised.value is error  # той самий об'єкт — до guard


def test_failed_complete_is_atomic_and_can_be_retried_once(db, clock, monkeypatch):
    service = InitialSetupService(db, clock)
    service.save_draft(DRAFT)
    calls = []
    original = AccumulationRepository.insert

    def insert_then_fail(repository, accumulation):
        calls.append(accumulation)
        original(repository, accumulation)
        raise ordinary()  # запис уже зроблено в транзакції — має бути відкочено

    monkeypatch.setattr(AccumulationRepository, "insert", insert_then_fail)
    with pytest.raises(DataWriteError):
        service.complete(DRAFT)
    assert calls and not service.is_completed()
    assert db.execute("SELECT count(*) FROM accumulations").fetchone() == (0,)
    monkeypatch.undo()
    service.complete(DRAFT)  # повтор
    assert service.is_completed()
    assert db.execute("SELECT count(*) FROM accumulations").fetchone() == (1,)
    with pytest.raises(DomainRuleError):
        service.complete(DRAFT)  # дубля немає
