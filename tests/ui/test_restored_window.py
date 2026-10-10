"""Після відновлення відкривається саме стан копії: без переходу місяця й без
автоматичного діалогу тривалої перерви. Звичайний запуск — без змін (ADR 0009)."""

import sqlite3
from datetime import UTC, datetime

import pytest

from budget.app import open_application_database, restore_and_open, show_main_window
from budget.domain.calendar import FixedClock
from budget.domain.money import Money
from budget.platform.identity import load_product_identity
from budget.platform.paths import DataPaths
from budget.services.backup import BackupKind, BackupService, RecoveryService
from budget.services.facade import AppServices
from budget.services.setup import SetupDraft
from budget.ui.dialogs.long_gap_dialog import LongGapDialog

OCTOBER = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
NOVEMBER = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)  # звичайний перехід: залишки переносяться
JANUARY = datetime(2027, 1, 12, 9, 0, tzinfo=UTC)  # тривала перерва: рішення в діалозі


def snapshot(connection: sqlite3.Connection) -> dict[str, list]:
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
    ]
    return {t: connection.execute(f'SELECT * FROM "{t}" ORDER BY 1').fetchall() for t in tables}


@pytest.fixture
def paths(tmp_path):
    return DataPaths(tmp_path / "Мої дані" / "Budget")


@pytest.fixture(params=[NOVEMBER, JANUARY], ids=["november", "january"])
def after_corruption(paths, request):
    """Копія з жовтневим доходом; пізніше база пошкоджена й перенесена в карантин."""
    clock = FixedClock(OCTOBER)
    connection = open_application_database(paths, clock)
    services = AppServices.create(connection, clock)
    services.setup.complete(SetupDraft(general_remainder=Money(100_000)))
    services.incomes.create("Жовтнева зарплата", None, Money(40_000))
    backup = BackupService(connection, paths.backups, clock).create_backup(BackupKind.ON_DEMAND)
    expected = snapshot(connection)
    connection.close()
    clock.set(request.param)
    paths.database.write_bytes(b"corrupted" * 1000)
    RecoveryService(paths.database, paths.backups, clock).quarantine_corrupted()
    return clock, backup, expected


@pytest.fixture
def long_gap_dialogs(monkeypatch):
    opened = []
    monkeypatch.setattr(LongGapDialog, "exec", lambda self: opened.append(self) or 0)
    return opened


def test_restored_state_is_shown_without_month_transition(
    qtbot, paths, after_corruption, long_gap_dialogs
):
    clock, backup, expected = after_corruption
    connection = restore_and_open(paths, clock, backup)
    window = show_main_window(
        load_product_identity(), connection, clock, paths.backups, restored=True
    )
    qtbot.addWidget(window)
    # Жовтневий дохід не архівовано, залишки не перенесено: дані — точно як у копії.
    assert snapshot(connection) == expected
    assert long_gap_dialogs == []
    # Рішення щодо залишків лишається доступним на Огляді (кнопка «Вирішити»).
    assert window.overview is not None
    connection.close()


def test_normal_startup_still_runs_transition_and_long_gap_dialog(
    qtbot, paths, after_corruption, long_gap_dialogs
):
    clock, backup, expected = after_corruption
    restore_and_open(paths, clock, backup).close()
    connection = open_application_database(paths, clock)  # наступний звичайний запуск
    window = show_main_window(
        load_product_identity(), connection, clock, paths.backups, restored=False
    )
    qtbot.addWidget(window)
    if clock.now().month == 11:
        assert snapshot(connection) != expected  # залишок перенесено, дохід архівовано
        assert long_gap_dialogs == []
    else:
        assert len(long_gap_dialogs) == 1  # тривала перерва: один діалог без вибору
    connection.close()
