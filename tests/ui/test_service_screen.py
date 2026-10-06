"""Екран «Сервіс»: таблиця резервних копій (IA 8; design-system.md 6.5)."""

from datetime import UTC, datetime, timedelta

import pytest

from budget.app import open_application_database
from budget.domain.calendar import FixedClock
from budget.domain.money import Money
from budget.platform.paths import DataPaths
from budget.services.backup import BackupKind, BackupService
from budget.services.facade import AppServices
from budget.services.setup import SetupDraft
from budget.ui.formatting import format_size
from budget.ui.main_window import MainWindow
from budget.ui.screens.placeholders import PlaceholderPage
from budget.ui.screens.service import BACKUP_COLUMNS, EMPTY_BACKUPS, ServicePage

START = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)  # 12:00 за Києвом


def plain(text: str) -> str:
    return text.replace(" ", " ").replace(" ", " ")


@pytest.fixture
def clock():
    return FixedClock(START)


@pytest.fixture
def paths(tmp_path):
    # Профіль Windows: пробіли й кирилиця; тека копій — <дані>\backups.
    return DataPaths(tmp_path / "Users" / "Олена Коваль" / "AppData" / "Local" / "Budget")


@pytest.fixture
def connection(paths, clock):
    connection = open_application_database(paths, clock)  # щоденна, щотижнева, щомісячна
    yield connection
    connection.close()


@pytest.fixture
def services(connection, paths, clock):
    services = AppServices.create(connection, clock, paths.backups)
    services.setup.complete(SetupDraft(general_remainder=Money(100_000)))
    return services


@pytest.fixture
def window(qtbot, services):
    widget = MainWindow("Budget", services)
    qtbot.addWidget(widget)
    return widget


def rows(page: ServicePage) -> list[list[str]]:
    table = page.table
    return [
        [plain(table.item(r, c).text()) for c in range(table.columnCount())]
        for r in range(table.rowCount())
    ]


def test_service_screen_opens_from_navigation(window):
    assert isinstance(window.service, ServicePage)
    window.sidebar._buttons["service"].click()
    assert window.current_route() == "service"
    table = window.service.table
    headers = [table.horizontalHeaderItem(c).text() for c in range(table.columnCount())]
    assert headers == list(BACKUP_COLUMNS)


def test_without_backups_directory_service_stays_placeholder(qtbot, connection, clock):
    services = AppServices.create(connection, clock)
    services.setup.complete(SetupDraft(general_remainder=Money(0)))
    window = MainWindow("Budget", services)
    qtbot.addWidget(window)
    assert window.service is None
    window.navigate("service")
    assert isinstance(window.pages.currentWidget(), PlaceholderPage)


def test_table_shows_every_kind_newest_first_with_date_and_size(window, services, clock):
    backups = services.backups
    for hours, kind, version in (
        (1, BackupKind.ON_DEMAND, None),
        (2, BackupKind.BEFORE_RESTORE, None),
        (3, BackupKind.BEFORE_MIGRATION, 1),
    ):
        clock.set(START + timedelta(hours=hours))
        backups.create_backup(kind, version)
    window.navigate("service")
    page = window.service
    assert [row[1] for row in rows(page)] == [
        "Перед міграцією",
        "Перед відновленням",
        "На вимогу",
        "Щотижнева",
        "Щомісячна",
        "Щоденна",
    ]
    assert [row[0] for row in rows(page)][:4] == [
        "6 жовтня 2026, 15:00",
        "6 жовтня 2026, 14:00",
        "6 жовтня 2026, 13:00",
        "6 жовтня 2026, 12:00",
    ]
    for candidate, row in zip(page.candidates, rows(page), strict=True):
        assert row[2] == plain(format_size(candidate.backup.path.stat().st_size))
    assert not page.table.isHidden() and page.empty.isHidden()


def test_invalid_and_unknown_files_are_not_shown(window, paths):
    good = sorted(paths.backups.iterdir())[0]
    (paths.backups / "budget-20261005-120000-daily.db").write_bytes(b"broken" * 500)
    (paths.backups / "budget-20261004-120000-manual.db").write_bytes(good.read_bytes())
    (paths.backups / "notes.txt").write_text("не копія", encoding="utf-8")
    window.navigate("service")
    assert len(rows(window.service)) == 3  # лише справні автоматичні копії
    assert {p.name for p in paths.backups.iterdir()} >= {
        "budget-20261005-120000-daily.db",
        "budget-20261004-120000-manual.db",
        "notes.txt",
    }  # нічого не видалено


def test_empty_state_is_neutral(qtbot, connection, tmp_path, clock):
    page = ServicePage(BackupService(connection, tmp_path / "немає копій", clock))
    qtbot.addWidget(page)
    assert page.table.isHidden() and not page.empty.isHidden()
    assert page.empty.text() == EMPTY_BACKUPS
    assert page.empty.property("tone") == "muted"


def test_table_refreshes_when_screen_is_opened(window, services, clock):
    window.navigate("service")
    before = len(rows(window.service))
    window.navigate("overview")
    clock.set(START + timedelta(hours=5))
    services.backups.create_backup(BackupKind.ON_DEMAND)
    window.navigate("service")
    assert len(rows(window.service)) == before + 1
    assert rows(window.service)[0][1] == "На вимогу"
