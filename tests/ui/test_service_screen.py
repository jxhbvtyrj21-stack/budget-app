"""Екран «Сервіс»: таблиця, копія на вимогу й «Про програму» (IA 8, 12; design-system.md 6.5)."""

import inspect
import logging
import sys
from datetime import UTC, datetime, timedelta
from importlib.metadata import version as package_version

import pytest
from PySide6.QtWidgets import QApplication

import budget.ui.screens.service as service_module
from budget.app import open_application_database
from budget.domain.calendar import FixedClock
from budget.domain.money import Money
from budget.errors import DatabaseCorruptedError, StorageError
from budget.platform.identity import load_product_identity
from budget.platform.paths import DataPaths
from budget.services.backup import BackupKind, BackupService, find_backups
from budget.services.facade import AppServices
from budget.services.setup import SetupDraft
from budget.ui.dialogs.recovery_dialog import RecoveryDialog
from budget.ui.formatting import format_size
from budget.ui.main_window import MainWindow
from budget.ui.screens.placeholders import PlaceholderPage
from budget.ui.screens.service import (
    BACKUP_COLUMNS,
    BACKUP_FAILED,
    CREATE_BACKUP,
    EMPTY_BACKUPS,
    RESTORE_BACKUP,
    ServicePage,
)

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
    widget = MainWindow(load_product_identity().name, services)
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
    page = ServicePage(
        BackupService(connection, tmp_path / "немає копій", clock), load_product_identity().name
    )
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


# Копія на вимогу (S1; IA 8, 12; DS-5) --------------------------------------------------------


@pytest.fixture
def previous_hook(monkeypatch):
    """Жоден виняток не доходить до ``sys.excepthook`` (і, отже, до відновлення)."""
    calls = []
    monkeypatch.setattr(sys, "excepthook", lambda *args: calls.append(args[1]))
    return calls


def visible_recovery_dialogs() -> list:
    return [
        w for w in QApplication.topLevelWidgets() if isinstance(w, RecoveryDialog) and w.isVisible()
    ]


def quarantined(paths) -> list[str]:
    return [p.name for p in paths.root.iterdir() if ".corrupted-" in p.name]


def make_backups_unavailable(paths) -> None:
    """Тека копій недоступна: на її місці звичайний файл (справжня помилка ОС)."""
    for path in paths.backups.iterdir():
        path.unlink()
    paths.backups.rmdir()
    paths.backups.write_bytes(b"not a directory")


def test_buttons_follow_the_screen_contract(window):
    page = window.service
    assert page.create_button.text() == CREATE_BACKUP and page.create_button.isEnabled()
    # «Відновити з копії» є на екрані (IA 8), але недоступна до окремого блоку відновлення.
    assert page.restore_button.text() == RESTORE_BACKUP and not page.restore_button.isEnabled()
    assert page.failure.isHidden()


def test_create_backup_adds_protected_on_demand_copy(window, services, paths, clock):
    window.navigate("service")
    page = window.service
    before = {p.name for p in paths.backups.iterdir()}
    clock.set(START + timedelta(hours=3))
    page.create_button.click()
    after = {p.name for p in paths.backups.iterdir()}
    (created,) = after - before
    assert created == "budget-20261006-150000-on-demand.db"
    assert before < after  # наявні копії лишилися
    # Нова копія видима одразу, найновіша, вид «На вимогу».
    assert rows(page)[0][:2] == ["6 жовтня 2026, 15:00", "На вимогу"]
    assert page.candidates[0].backup.kind is BackupKind.ON_DEMAND
    assert page.failure.isHidden()
    # Ротація автоматичних копій (7/4/12) копію на вимогу не видаляє.
    for day in range(1, 400, 3):
        clock.set(START + timedelta(days=day))
        services.backups.run_automatic()
    on_demand = [b.path.name for b in find_backups(paths.backups) if b.kind is BackupKind.ON_DEMAND]
    assert on_demand == [created]


def test_unavailable_destination_shows_error_and_keeps_working(
    window, connection, paths, clock, caplog, previous_hook
):
    window.navigate("service")
    page = window.service
    shown = rows(page)
    make_backups_unavailable(paths)
    clock.set(START + timedelta(hours=1))
    with caplog.at_level(logging.ERROR):
        page.create_button.click()
    # «Помилка» з причиною, без трасування; перелік не перебудовано руйнівно.
    assert not page.failure.isHidden()
    assert page.failure.title.text() == "Помилка"
    assert page.failure.body.text() == f"{BACKUP_FAILED} {StorageError().user_message}"
    assert "Traceback" not in page.failure.body.text()
    assert rows(page) == shown
    # Повний виняток — у журналі.
    (record,) = [r for r in caplog.records if r.getMessage() == "On-demand backup failed"]
    assert isinstance(record.exc_info[1], StorageError)
    assert not isinstance(record.exc_info[1], DatabaseCorruptedError)
    # Застосунок працює далі на тому самому з'єднанні; відновлення й карантину немає.
    assert connection.execute("SELECT count(*) FROM general_remainder").fetchone() == (1,)
    window.navigate("overview")
    window.refresh()
    assert visible_recovery_dialogs() == [] and quarantined(paths) == []
    assert previous_hook == []


def test_corrupted_source_is_reported_not_backed_up(
    window, connection, paths, clock, caplog, previous_hook
):
    window.navigate("service")
    page = window.service
    before = sorted(p.name for p in paths.backups.iterdir())
    # Справжнє пошкодження сторінок бази, яку перевіряє копія.
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.execute("PRAGMA cache_size = 0")
    pages = paths.database.stat().st_size // 4096
    with paths.database.open("r+b") as handle:
        for number in range(2, pages):
            handle.seek(number * 4096)
            handle.write(b"\xa5" * 4096)
    clock.set(START + timedelta(hours=1))
    with caplog.at_level(logging.ERROR):
        page.create_button.click()
    # Не успіх і не звичайна помилка копії: пошкодження з причиною, копії немає.
    assert not page.failure.isHidden()
    assert page.failure.body.text() == f"{BACKUP_FAILED} {DatabaseCorruptedError().user_message}"
    (record,) = [r for r in caplog.records if r.getMessage() == "On-demand backup failed"]
    assert isinstance(record.exc_info[1], DatabaseCorruptedError)
    assert sorted(p.name for p in paths.backups.iterdir()) == before
    # Копія на вимогу відновлення під час роботи сама не запускає.
    assert visible_recovery_dialogs() == [] and quarantined(paths) == []
    assert previous_hook == []


def test_backup_works_again_after_a_failure(window, connection, paths, clock):
    window.navigate("service")
    page = window.service
    make_backups_unavailable(paths)
    clock.set(START + timedelta(hours=1))
    page.create_button.click()
    assert not page.failure.isHidden()
    paths.backups.unlink()  # тека знову доступна
    page.create_button.click()  # та сама мить — та сама назва: файл після невдачі не лишився
    assert page.failure.isHidden()
    assert [b.backup.kind for b in page.candidates] == [BackupKind.ON_DEMAND]
    assert rows(page)[0][1] == "На вимогу"
    assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)


# «Про програму» (S2; IA 8) ---------------------------------------------------------------------


def test_about_shows_product_name_and_package_version(window):
    page = window.service
    identity = load_product_identity()
    assert page.product_label.text() == identity.name  # з product.toml через головне вікно
    metadata_version = package_version("budget")  # метадані встановленого пакета
    assert metadata_version
    assert service_module.application_version() == metadata_version
    assert page.version_label.text() == f"Версія {metadata_version}"


def test_about_has_no_hardcoded_version():
    source = inspect.getsource(service_module)
    assert package_version("budget") not in source
