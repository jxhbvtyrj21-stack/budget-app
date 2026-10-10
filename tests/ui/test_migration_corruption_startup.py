"""Пошкодження, виявлене міграцією під час запуску (M2/F5), — наявний шлях DS-6.

Потребує Qt (діалог «Дані пошкоджено»), тому лежить у ``tests/ui``, а не в
``tests/storage`` (Linux-набір ``core-tests`` без Qt). Решта сценаріїв невдалої міграції —
у ``tests/storage/test_migration_failures.py``.
"""

from datetime import UTC, datetime

import budget.app as app_module
import budget.storage.migrations as migrations
from budget.app import ApplicationSession, open_application_database, start_session
from budget.domain.calendar import FixedClock
from budget.errors import DatabaseCorruptedError
from budget.platform.identity import load_product_identity
from budget.platform.paths import DataPaths
from budget.storage.integrity import corruption_code
from budget.ui.dialogs.recovery_dialog import RecoveryDialog

START = datetime(2026, 10, 9, 9, 0, tzinfo=UTC)

# Справжнє SQLITE_CORRUPT усередині міграції: таблиця вказує на сторінку за межами файлу.
CORRUPTING_SQL = """
CREATE TABLE probe (a);
INSERT INTO probe VALUES (1);
PRAGMA writable_schema = ON;
UPDATE sqlite_master SET rootpage = 999999 WHERE name = 'probe';
PRAGMA writable_schema = RESET;
SELECT * FROM probe;
"""


def test_start_session_sends_migration_corruption_to_recovery(qtbot, tmp_path, monkeypatch):
    paths = DataPaths(tmp_path / "Мої дані" / "Budget")
    clock = FixedClock(START)
    open_application_database(paths, clock).close()  # наявна база версії 1
    messages: list[str] = []
    monkeypatch.setattr(app_module, "_show_message", lambda title, text: messages.append(text))
    monkeypatch.setattr(migrations, "MIGRATIONS", (*migrations.MIGRATIONS, (2, CORRUPTING_SQL)))
    monkeypatch.setattr(migrations, "LATEST_VERSION", 2)
    errors = []
    recover = app_module._recover_corrupted_database

    def spy(identity, paths, clock, error):
        errors.append(error)
        return recover(identity, paths, clock, error)

    monkeypatch.setattr(app_module, "_recover_corrupted_database", spy)
    shown = []
    monkeypatch.setattr(RecoveryDialog, "exec", lambda self: shown.append(self) or 0)
    session = ApplicationSession(paths, clock)
    result = start_session(load_product_identity(), session, paths, clock)
    assert result == (app_module.EXIT_DATA_CORRUPTED, False)
    [error] = errors
    assert isinstance(error, DatabaseCorruptedError) and corruption_code(error) is not None
    assert len(shown) == 1 and messages == [] and not session.is_open
    assert not paths.database.exists()  # наявний шлях DS-6: карантин і діалог відновлення
    assert [p for p in paths.root.iterdir() if p.name.startswith("budget.db.corrupted-")]
