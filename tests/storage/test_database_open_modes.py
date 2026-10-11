"""``open_database``: створення бази дозволене лише явно (F8, S1).

Ізольована перевірка функції сховища (не шлях застосунку): ``create=False`` відкриває через
URI ``mode=rw`` саме той файл, що за шляхом — і з пробілами, Unicode та символами, значущими
в URI (``#``, ``%``, ``[]``; ``?`` у назвах файлів Windows заборонений), — а відсутній файл
не створює й не чіпає його ``-wal``.
"""

import os
import sqlite3

import pytest

from budget.errors import DatabaseCorruptedError, StorageError
from budget.storage.database import open_database

NAMES = ["Мої дані", "with space", "q#x%20z[1]"]


@pytest.fixture(params=NAMES)
def folder(tmp_path, request):
    return tmp_path / "Користувач Олена" / request.param


def test_existing_database_opens_the_same_file_without_create(folder):
    database = folder / "budget.db"
    connection = open_database(database)  # створення — явне
    connection.execute("CREATE TABLE marker (value TEXT)")
    connection.close()

    connection = open_database(database, create=False)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        connection.execute("INSERT INTO marker VALUES ('через mode=rw')")
        # SQLite повертає звичайний шлях (не URI): той самий файл і та сама нормалізована
        # форма шляху (роздільники, регістр — за правилами платформи).
        (path,) = [row[2] for row in connection.execute("PRAGMA database_list")]
        assert os.path.isabs(path)
        assert os.path.normcase(os.path.normpath(path)) == os.path.normcase(
            os.path.normpath(database)
        )
        assert os.path.samefile(path, database)
    finally:
        connection.close()
    assert sorted(p.name for p in folder.iterdir()) == ["budget.db"]  # жодних інших файлів
    check = sqlite3.connect(database)
    try:
        assert check.execute("SELECT value FROM marker").fetchall() == [("через mode=rw",)]
    finally:
        check.close()


def test_missing_database_is_an_error_and_nothing_is_created(folder):
    folder.mkdir(parents=True)
    database = folder / "budget.db"
    wal = folder / "budget.db-wal"
    wal.write_bytes(b"leftover wal of a vanished database")
    with pytest.raises(StorageError) as raised:
        open_database(database, create=False)
    error = raised.value
    assert type(error) is StorageError and not isinstance(error, DatabaseCorruptedError)
    assert str(database) in error.detail
    assert isinstance(error.__cause__, sqlite3.OperationalError)
    assert not database.exists()
    assert wal.read_bytes() == b"leftover wal of a vanished database"
    assert sorted(p.name for p in folder.iterdir()) == ["budget.db-wal"]


def test_missing_folder_is_not_created_without_create(tmp_path):
    database = tmp_path / "немає" / "budget.db"
    with pytest.raises(StorageError):
        open_database(database, create=False)
    assert not database.parent.exists()


def test_create_is_the_default_and_makes_a_new_database(tmp_path):
    database = tmp_path / "нова тека" / "budget.db"
    connection = open_database(database)
    connection.close()
    assert database.exists()
