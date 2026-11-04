from datetime import UTC, datetime

import pytest

from budget.domain.calendar import FixedClock
from budget.services.startup import prepare_database
from budget.storage.transaction import transaction


@pytest.fixture
def clock():
    # 6 жовтня 2026 року, полудень за Києвом.
    return FixedClock(datetime(2026, 10, 6, 9, 0, tzinfo=UTC))


@pytest.fixture
def db(tmp_path, clock):
    connection = prepare_database(tmp_path / "budget.db", tmp_path / "backups", clock)
    yield connection
    connection.close()


@pytest.fixture
def completed_db(db):
    """База з уже завершеним первинним налаштуванням без стартових значень."""
    with transaction(db):
        db.execute(
            "UPDATE setup_state SET status = 'completed', completed_month = '2026-10' WHERE id = 1"
        )
    return db
