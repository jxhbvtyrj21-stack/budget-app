import pytest

from budget.storage.database import open_database
from budget.storage.migrations import migrate


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "data" / "budget.db"


@pytest.fixture
def connection(db_path):
    connection = open_database(db_path)
    migrate(connection)
    yield connection
    connection.close()
