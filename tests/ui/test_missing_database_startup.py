"""Запуск без робочої бази, але з ознаками попередньої (R1).

Справжній ``start_session`` (і для T2/T7 — справжній ``run_application``), справжні
файли теки даних. До рішення користувача в діалозі — жодного відкриття ``budget.db``,
міграцій, автоматичних копій і ротації. Діалог керується через його ``exec``.
"""

import errno
import logging
import os
import shutil
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox
from tests.sqlite_targets import connect_target

import budget.app as app_module
import budget.services.backup as backup_service_module
import budget.services.startup as startup_module
import budget.storage.recovery as recovery_module
from budget.app import (
    DATA_DIRECTORY_UNKNOWN_MESSAGE,
    EXIT_DATA_CORRUPTED,
    EXIT_OK,
    EXIT_STARTUP_FAILED,
    ApplicationSession,
    run_application,
    start_session,
)
from budget.domain.calendar import FixedClock
from budget.domain.money import Money
from budget.errors import StorageError
from budget.platform.identity import load_product_identity
from budget.platform.paths import DataPaths
from budget.services.backup import BackupKind, BackupService, RecoveryService, RestoreError
from budget.services.facade import AppServices
from budget.services.setup import InitialSetupService, SetupDraft
from budget.services.startup import ORPHANS_NOT_SET_ASIDE_MESSAGE
from budget.storage.database import close_without_checkpoint
from budget.ui.dialogs.recovery_dialog import (
    MISSING_DATABASE_EXPLANATION,
    RUNTIME_EXPLANATION,
    START_EMPTY,
    START_EMPTY_CONFIRMATION,
    STARTUP_EXPLANATION,
    RecoveryDialog,
)
from budget.ui.main_window import MainWindow

START = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
IDENTITY = load_product_identity()


@pytest.fixture
def clock():
    return FixedClock(START)


@pytest.fixture
def paths(tmp_path):
    return DataPaths(tmp_path / "Users" / "Олена Коваль" / "AppData" / "Local" / "Budget")


def is_closed(connection: sqlite3.Connection) -> bool:
    try:
        connection.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return True
    return False


def make_history(paths, clock, *, wal_balance: int | None = None):
    """Робоча база з даними й копією «на вимогу». ``wal_balance`` — останню зміну лишити
    лише у WAL (закриття без checkpoint, як ``close_after_corruption``)."""
    session = ApplicationSession(paths, clock)
    connection = session.open()
    services = AppServices.create(connection, clock, paths.backups)
    services.setup.complete(SetupDraft(general_remainder=Money(250_000)))
    services.incomes.create("Зарплата", None, Money(40_000))
    clock.set(clock.now() + timedelta(hours=1))
    services.backups.create_backup(BackupKind.ON_DEMAND)
    if wal_balance is None:
        session.close()
        return
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.execute("UPDATE general_remainder SET balance = ?", (wal_balance,))
    session.close_after_corruption()


def quarantine(paths, clock):
    """Те, що робить A (``RuntimeRecoveryFlow``) і B перед діалогом: карантин бази."""
    return RecoveryService(paths.database, paths.backups, clock).quarantine_corrupted()


def files(folder) -> dict[str, bytes]:
    return {
        p.relative_to(folder).as_posix(): p.read_bytes()
        for p in sorted(folder.rglob("*"))
        if p.is_file() and p.suffix != ".log" and p.name != "budget.lock"
    }


def backup_files(paths) -> dict[str, bytes]:
    return files(paths.backups) if paths.backups.exists() else {}


@pytest.fixture
def watch(monkeypatch, paths):
    """Що відбувається з робочою базою: з'єднання, міграції, автокопії, ротація, карантин."""
    seen = {"connect": [], "migrate": [], "automatic": [], "rotate": [], "quarantine": []}
    original_connect = sqlite3.connect

    def connect(target, *args, **kwargs):
        seen["connect"].append(connect_target(target, kwargs.get("uri", False)))
        return original_connect(target, *args, **kwargs)

    def spy(key, owner, name):
        original = getattr(owner, name)

        def wrapper(*args, **kwargs):
            seen[key].append(args)
            return original(*args, **kwargs)

        monkeypatch.setattr(owner, name, wrapper)

    monkeypatch.setattr(sqlite3, "connect", connect)
    spy("migrate", startup_module, "migrate")
    spy("automatic", BackupService, "run_automatic")
    spy("rotate", BackupService, "_rotate")
    spy("quarantine", RecoveryService, "quarantine_corrupted")
    seen["database_opened"] = lambda: str(paths.database) in seen["connect"]

    def reset():  # підготовка історії відкривала базу; рахуємо лише запуск
        for key in ("connect", "migrate", "automatic", "rotate", "quarantine"):
            seen[key].clear()

    seen["reset"] = reset
    return seen


class Dialogs:
    """Записує кожен показаний ``RecoveryDialog`` і діє як користувач."""

    def __init__(self, monkeypatch, action) -> None:
        self.shown: list[dict] = []
        self.action = action
        recorder = self

        def exec_(dialog):
            texts = [label.text() for label in dialog.findChildren(type(dialog.empty))]
            recorder.shown.append(
                {
                    "mode": dialog_mode(texts),
                    "title": (dialog.windowTitle(), headings(dialog)),
                    "start_empty": dialog.start_empty_button is not None,
                    "candidates": dialog.list.count(),
                    "empty_visible": not dialog.empty.isHidden(),
                }
            )
            return recorder.action(dialog)

        monkeypatch.setattr(RecoveryDialog, "exec", exec_)


def headings(dialog) -> list[str]:
    """Тексти верхнього рядка діалогу (роль ``heading``)."""
    return [
        label.text()
        for label in dialog.findChildren(type(dialog.empty))
        if label.property("textRole") == "heading"
    ]


def dialog_mode(texts: list[str]) -> str:
    """Яке пояснення показано: без бази (R1), пошкодження під час запуску чи роботи."""
    if MISSING_DATABASE_EXPLANATION in texts:
        return "missing"
    for mode, template in (("startup", STARTUP_EXPLANATION), ("runtime", RUNTIME_EXPLANATION)):
        if any(t.startswith(template.split("{name}")[0]) for t in texts):
            return mode
    return "unknown"


def cancel(dialog) -> int:
    return 0


def restore_first(dialog) -> int:
    dialog.restore_selected()
    return dialog.result()


def start_empty(dialog) -> int:
    dialog.start_empty_selected()
    return dialog.result()


@pytest.fixture
def confirmations(monkeypatch):
    """Підтвердження (відновлення й порожнього старту): тексти записуються, «так»."""
    texts = []
    monkeypatch.setattr(QMessageBox, "exec", lambda box: texts.append(box.text()) or 0)
    monkeypatch.setattr(
        QMessageBox,
        "clickedButton",
        lambda box: next(
            b for b in box.buttons() if box.buttonRole(b) == QMessageBox.ButtonRole.DestructiveRole
        ),
    )
    return texts


@pytest.fixture
def messages(monkeypatch):
    shown = []
    monkeypatch.setattr(app_module, "_show_message", lambda title, text: shown.append(text))
    return shown


def launch(paths, clock):
    session = ApplicationSession(paths, clock)
    result = start_session(IDENTITY, session, paths, clock)
    return session, result


# T1. Чистий перший запуск ---------------------------------------------------------------------


@pytest.mark.parametrize("extras", [False, True], ids=["empty-folder", "logs-lock-settings"])
def test_clean_first_run_is_unchanged(qtbot, paths, clock, monkeypatch, extras):
    if extras:
        (paths.logs).mkdir(parents=True)
        (paths.logs / "budget.log").write_text("x")
        paths.lock.write_text("1")
        paths.settings.write_text("{}")
    dialogs = Dialogs(monkeypatch, lambda d: pytest.fail("діалогу не має бути"))
    session, result = launch(paths, clock)
    try:
        assert result == (None, False) and dialogs.shown == []
        assert paths.database.exists() and session.is_open
        assert not InitialSetupService(session.connection).is_completed()
        kinds = {b.kind for b in BackupService(session.connection, paths.backups, clock).backups()}
        assert kinds == {BackupKind.DAILY, BackupKind.WEEKLY, BackupKind.MONTHLY}
    finally:
        session.close()


# T2, T3. Після карантину A чи B і скасування — наступний запуск -----------------------------


def test_next_launch_after_runtime_quarantine_and_cancel(qtbot, paths, clock, monkeypatch, watch):
    make_history(paths, clock, wal_balance=4242)
    quarantined = quarantine(paths, clock)  # A: close_after_corruption → карантин → скасування
    before = files(paths.root)
    watch["reset"]()
    dialogs = Dialogs(monkeypatch, cancel)
    session, result = launch(paths, clock)
    assert result == (EXIT_DATA_CORRUPTED, False) and not session.is_open
    assert len(dialogs.shown) == 1
    assert dialogs.shown[0]["mode"] == "missing"
    assert dialogs.shown[0]["start_empty"] is True and dialogs.shown[0]["candidates"] >= 1
    assert not paths.database.exists()
    assert files(paths.root) == before  # карантин і копії — ті самі байти
    assert quarantined.exists()
    assert not watch["database_opened"]()
    assert watch["migrate"] == watch["automatic"] == watch["rotate"] == watch["quarantine"] == []


def test_next_launch_after_startup_recovery_cancel(qtbot, paths, clock, monkeypatch, watch):
    make_history(paths, clock)
    paths.database.write_bytes(b"corrupted" * 1000)  # B: пошкодження до запуску
    dialogs = Dialogs(monkeypatch, cancel)
    _, first = launch(paths, clock)
    assert first == (EXIT_DATA_CORRUPTED, False)
    assert dialogs.shown[0]["mode"] == "startup"
    assert dialogs.shown[0]["start_empty"] is False  # чинний діалог B без нової дії
    assert dialogs.shown[0]["title"] == ("Дані пошкоджено", ["Дані пошкоджено"])
    before = files(paths.root)
    watch["reset"]()
    session, result = launch(paths, clock)  # наступний запуск
    assert result == (EXIT_DATA_CORRUPTED, False) and not session.is_open
    assert dialogs.shown[1]["mode"] == "missing"
    assert dialogs.shown[1]["title"] == ("Базу даних не знайдено", ["Базу даних не знайдено"])
    assert not paths.database.exists() and files(paths.root) == before
    assert not watch["database_opened"]()
    assert watch["migrate"] == watch["automatic"] == watch["rotate"] == watch["quarantine"] == []


def test_next_launch_through_run_application_exits_data_corrupted(
    qapp, paths, clock, monkeypatch, messages
):
    make_history(paths, clock)
    quarantine(paths, clock)
    before = backup_files(paths)
    Dialogs(monkeypatch, cancel)
    assert run_application(IDENTITY, paths, clock, qapp) == EXIT_DATA_CORRUPTED
    assert not paths.database.exists() and backup_files(paths) == before and messages == []


# T4–T6. Жодного відкриття, міграцій, автокопій і ротації; багато запусків поспіль -----------


def test_blocked_startups_over_eight_days_change_nothing(qtbot, paths, clock, monkeypatch, watch):
    make_history(paths, clock)
    quarantine(paths, clock)
    before = files(paths.root)
    watch["reset"]()
    dialogs = Dialogs(monkeypatch, cancel)
    for day in range(8):
        clock.set(START + timedelta(days=day + 1, hours=3))
        _, result = launch(paths, clock)
        assert result == (EXIT_DATA_CORRUPTED, False)
    assert len(dialogs.shown) == 8
    assert files(paths.root) == before  # жодної нової копії, жодного видалення
    assert not paths.database.exists() and not watch["database_opened"]()
    assert watch["migrate"] == watch["automatic"] == watch["rotate"] == []


# T7. Відновлення з копії --------------------------------------------------------------------


def test_restore_from_backup_continues_like_startup_recovery(
    qtbot, paths, clock, monkeypatch, watch, confirmations
):
    make_history(paths, clock)
    quarantine(paths, clock)
    clock.set(START + timedelta(days=3))
    returned = []
    original = app_module.restore_and_open
    monkeypatch.setattr(
        app_module,
        "restore_and_open",
        lambda *a, **k: returned.append(original(*a, **k)) or returned[-1],
    )
    opened_before_choice = []

    def restore_after_check(dialog):
        opened_before_choice.append(watch["database_opened"]() or bool(watch["migrate"]))
        return restore_first(dialog)

    Dialogs(monkeypatch, restore_after_check)
    watch["reset"]()
    session, result = launch(paths, clock)
    try:
        assert opened_before_choice == [False]  # до рішення — ні бази, ні міграцій
        assert result == (None, True)
        assert session.connection is returned[0]
        balance = session.connection.execute("SELECT balance FROM general_remainder").fetchone()
        assert balance == (250_000,)
        assert watch["quarantine"] == []  # бази не було — карантину немає
        assert len(watch["automatic"]) == 1  # автокопії — лише для відновленої бази
        newest = BackupService(session.connection, paths.backups, clock).backups()[0]
        assert newest.created.date() == (START + timedelta(days=3)).date()
        assert [c for c in watch["connect"] if c == str(paths.database)]  # відкрито відновлену
    finally:
        session.close()


def test_restore_through_run_application_shows_the_restored_window(
    qapp, paths, clock, monkeypatch, confirmations, messages
):
    make_history(paths, clock)
    quarantine(paths, clock)
    Dialogs(monkeypatch, restore_first)
    windows = []

    def poll():
        visible = [w for w in QApplication.topLevelWidgets() if isinstance(w, MainWindow)]
        visible = [w for w in visible if w.isVisible()]
        if visible:
            windows.extend(visible)
            qapp.exit(EXIT_OK)
        else:
            QTimer.singleShot(10, poll)

    QTimer.singleShot(0, poll)
    try:
        assert run_application(IDENTITY, paths, clock, qapp) == EXIT_OK
    finally:
        for window in windows:
            window.close()
            window.deleteLater()
    assert len(windows) == 1 and messages == []
    assert windows[0].wizard is None  # відновлена база з налаштуванням, не майстер
    assert paths.database.exists()


# T8. Ознаки є, справних копій немає --------------------------------------------------------


def test_no_valid_backups_is_a_controlled_state(qtbot, paths, clock, monkeypatch, watch):
    paths.root.mkdir(parents=True)
    (paths.root / "budget.db.corrupted-20261005-120000").write_bytes(b"corrupted" * 100)
    paths.backups.mkdir()
    (paths.backups / "budget-20261005-120000-daily.db").write_bytes(b"broken" * 100)
    dialogs = Dialogs(monkeypatch, cancel)
    watch["reset"]()
    _, result = launch(paths, clock)
    assert result == (EXIT_DATA_CORRUPTED, False)
    (shown,) = dialogs.shown
    assert shown["candidates"] == 0 and shown["empty_visible"] and shown["start_empty"]
    assert not paths.database.exists() and not watch["database_opened"]()


# T9. F7: лише -wal/-shm без бази ------------------------------------------------------------


@pytest.fixture
def split_quarantine(paths, clock, tmp_path):
    """Карантин розщеплено: ``.db`` уже перенесено (тут — за межі теки даних), а
    ``-wal``/``-shm`` лишилися під старими назвами. Останні дані — лише у WAL."""
    make_history(paths, clock, wal_balance=4242)
    moved = tmp_path / "elsewhere" / "budget.db"
    moved.parent.mkdir()
    shutil.move(paths.database, moved)
    wal = paths.root / "budget.db-wal"
    shm = paths.root / "budget.db-shm"
    assert wal.stat().st_size > 0 and shm.exists()
    return moved, {"-wal": wal.read_bytes(), "-shm": shm.read_bytes()}


def balance_with(db_file, wal_bytes, folder) -> int:
    """Баланс бази разом із WAL (копії в окремій теці): WAL справді містить дані."""
    folder.mkdir()
    shutil.copy(db_file, folder / "check.db")
    (folder / "check.db-wal").write_bytes(wal_bytes)
    connection = sqlite3.connect(folder / "check.db")
    try:
        return connection.execute("SELECT balance FROM general_remainder").fetchone()[0]
    finally:
        connection.close()


def test_leftover_wal_is_not_destroyed_by_startup(
    qtbot, paths, clock, monkeypatch, watch, split_quarantine, tmp_path
):
    moved, leftovers = split_quarantine
    shutil.rmtree(paths.backups)  # лише -wal/-shm — єдина ознака
    Dialogs(monkeypatch, cancel)
    watch["reset"]()
    _, result = launch(paths, clock)
    assert result == (EXIT_DATA_CORRUPTED, False) and not paths.database.exists()
    for side, data in leftovers.items():
        assert (paths.root / f"budget.db{side}").read_bytes() == data
    assert not watch["database_opened"]()
    assert balance_with(moved, leftovers["-wal"], tmp_path / "check") == 4242


def test_leftover_wal_is_set_aside_when_restoring(
    qtbot, paths, clock, monkeypatch, split_quarantine, confirmations
):
    _, leftovers = split_quarantine
    Dialogs(monkeypatch, restore_first)
    session, result = launch(paths, clock)
    try:
        assert result == (None, True)
        balance = session.connection.execute("SELECT balance FROM general_remainder").fetchone()
        assert balance == (250_000,)  # стан копії
    finally:
        session.close()
    orphaned = sorted(p.name for p in paths.root.iterdir() if ".orphaned-" in p.name)
    assert [n[-4:] for n in orphaned] == ["-shm", "-wal"]
    for name in orphaned:
        assert (paths.root / name).read_bytes() == leftovers[name[-4:]]


def test_leftover_wal_is_set_aside_before_starting_empty(
    qtbot, paths, clock, monkeypatch, split_quarantine, confirmations, tmp_path
):
    moved, leftovers = split_quarantine
    Dialogs(monkeypatch, start_empty)
    session, result = launch(paths, clock)
    try:
        assert result == (None, False) and paths.database.exists()
        assert not InitialSetupService(session.connection).is_completed()
    finally:
        session.close()
    orphaned = sorted(p for p in paths.root.iterdir() if ".orphaned-" in p.name)
    assert [p.name[-4:] for p in orphaned] == ["-shm", "-wal"]
    for path in orphaned:
        assert path.read_bytes() == leftovers[path.name[-4:]]
    assert balance_with(moved, orphaned[1].read_bytes(), tmp_path / "check") == 4242


# Журнал залишків і відновлення (F8-S2.1) -------------------------------------------------------


def logged(caplog, logger: str, level: int, prefix: str) -> list[logging.LogRecord]:
    """Записи логера ``logger`` рівня ``level``, що починаються з ``prefix``."""
    return [
        r
        for r in caplog.records
        if r.name == logger and r.levelno == level and r.getMessage().startswith(prefix)
    ]


def assert_without_paths(entries, *folders) -> None:
    """Нові записи — лише назви файлів і причини, без повних шляхів і трасувань."""
    for entry in entries:
        text = entry.getMessage()
        for folder in folders:
            assert str(folder) not in text
        assert entry.exc_info is None


def deny_rename(monkeypatch, name: str) -> None:
    """``Path.rename`` для файлу ``name`` — як на Windows, коли файл тримає інший процес."""
    original = Path.rename

    def rename(self, target):
        if self.name == name:
            raise PermissionError(13, "Permission denied", str(self))
        return original(self, target)

    monkeypatch.setattr(Path, "rename", rename)


def set_aside_names(paths) -> str:
    """Назва відкладеного комплекту (спільний префікс ``-wal``/``-shm``)."""
    (wal,) = paths.root.glob("budget.db.orphaned-*-wal")
    return wal.name[: -len("-wal")]


SET_ASIDE_EMPTY = "Leftover database files set aside before starting empty: "
NOT_SET_ASIDE = "Leftover database files were not set aside; no new database is created: "
SET_ASIDE_RESTORE = "Файли бази відкладено вбік перед відновленням: "
NOT_RESTORED = "Restore from "


def restore_recording(chosen: list):
    """Відновлення з першої копії; назва вибраної копії — у ``chosen``."""

    def action(dialog):
        chosen.append(dialog.selected().backup.path.name)
        dialog.restore_selected()
        return dialog.result()

    return action


def test_start_empty_logs_the_set_aside_files(
    qtbot, paths, clock, monkeypatch, split_quarantine, confirmations, caplog, tmp_path
):
    caplog.set_level(logging.INFO)
    Dialogs(monkeypatch, start_empty)
    session, result = launch(paths, clock)
    session.close()
    assert result == (None, False)
    (entry,) = logged(caplog, "budget.app", logging.INFO, SET_ASIDE_EMPTY)
    assert entry.getMessage() == SET_ASIDE_EMPTY + set_aside_names(paths)
    assert not logged(caplog, "budget.app", logging.WARNING, NOT_SET_ASIDE)
    assert_without_paths([entry], paths.root, tmp_path)


def test_failed_set_aside_before_start_empty_is_logged(
    qtbot, paths, clock, monkeypatch, split_quarantine, confirmations, caplog, tmp_path
):
    _, leftovers = split_quarantine
    caplog.set_level(logging.INFO)
    deny_rename(monkeypatch, "budget.db-wal")
    failures = []

    def try_start_empty(dialog):
        dialog.start_empty_selected()
        failures.append(dialog.failure.body.text())
        return dialog.result()

    Dialogs(monkeypatch, try_start_empty)
    _, result = launch(paths, clock)
    assert failures == [ORPHANS_NOT_SET_ASIDE_MESSAGE]  # повідомлення користувачу те саме
    assert result == (EXIT_DATA_CORRUPTED, False) and not paths.database.exists()
    for side, data in leftovers.items():
        assert (paths.root / f"budget.db{side}").read_bytes() == data
    (entry,) = logged(caplog, "budget.app", logging.WARNING, NOT_SET_ASIDE)
    assert entry.getMessage() == NOT_SET_ASIDE + (
        "StorageError <- StorageError <- PermissionError [errno 13 EACCES]"
    )
    assert not logged(caplog, "budget.app", logging.INFO, SET_ASIDE_EMPTY)
    assert_without_paths([entry], paths.root, tmp_path)


def test_failed_set_aside_during_startup_restore_is_logged(
    qtbot, paths, clock, monkeypatch, split_quarantine, confirmations, caplog, tmp_path
):
    _, leftovers = split_quarantine
    caplog.set_level(logging.INFO)
    deny_rename(monkeypatch, "budget.db-wal")
    chosen = []
    dialogs = Dialogs(monkeypatch, restore_recording(chosen))
    _, result = launch(paths, clock)
    assert dialogs.shown and result == (EXIT_DATA_CORRUPTED, False)
    assert not paths.database.exists()
    for side, data in leftovers.items():
        assert (paths.root / f"budget.db{side}").read_bytes() == data
    (entry,) = logged(caplog, "budget.app", logging.WARNING, NOT_RESTORED)
    assert entry.getMessage() == (
        f"Restore from {chosen[0]} was not performed: "
        "RestoreError <- StorageError <- PermissionError [errno 13 EACCES]"
    )
    assert not logged(caplog, "budget.services.backup", logging.INFO, SET_ASIDE_RESTORE)
    assert_without_paths([entry], paths.root, tmp_path)


def test_failed_install_with_rollback_during_startup_restore_is_logged(
    qtbot, paths, clock, monkeypatch, split_quarantine, confirmations, caplog, tmp_path
):
    _, leftovers = split_quarantine
    caplog.set_level(logging.INFO)

    def no_space(backup_path, database_path):
        try:
            raise OSError(28, "No space left on device")
        except OSError as exc:
            raise StorageError(detail=f"Відновлення не вдалося: {exc}") from exc

    monkeypatch.setattr(backup_service_module, "restore_from_backup", no_space)
    chosen = []
    Dialogs(monkeypatch, restore_recording(chosen))
    _, result = launch(paths, clock)
    assert result == (EXIT_DATA_CORRUPTED, False) and not paths.database.exists()
    # Відкат повернув залишки під старі назви — як до спроби.
    for side, data in leftovers.items():
        assert (paths.root / f"budget.db{side}").read_bytes() == data
    assert not [p for p in paths.root.iterdir() if ".orphaned-" in p.name]
    (entry,) = logged(caplog, "budget.app", logging.WARNING, NOT_RESTORED)
    assert entry.getMessage() == (
        f"Restore from {chosen[0]} was not performed: "
        "RestoreError <- StorageError <- OSError [errno 28 ENOSPC]"
    )
    assert_without_paths([entry], paths.root, tmp_path)


def test_startup_restore_logs_the_set_aside_files(
    qtbot, paths, clock, monkeypatch, split_quarantine, confirmations, caplog, tmp_path
):
    caplog.set_level(logging.INFO)
    Dialogs(monkeypatch, restore_first)
    session, result = launch(paths, clock)
    session.close()
    assert result == (None, True)
    (entry,) = logged(caplog, "budget.services.backup", logging.INFO, SET_ASIDE_RESTORE)
    assert entry.getMessage() == SET_ASIDE_RESTORE + set_aside_names(paths)
    assert not logged(caplog, "budget.app", logging.WARNING, NOT_RESTORED)
    assert_without_paths([entry], paths.root, tmp_path)


def test_shared_restore_logs_nothing_new_without_the_startup_flag(
    paths, clock, monkeypatch, split_quarantine, caplog
):
    """Ручне відновлення й відновлення під час роботи викликають ``restore_and_open`` без
    ``log_not_performed`` і журналюють збій самі: нового запису тут немає. Виняток той
    самий, що й із прапорцем."""
    _, leftovers = split_quarantine
    caplog.set_level(logging.INFO)
    deny_rename(monkeypatch, "budget.db-wal")
    backup = RecoveryService(paths.database, paths.backups, clock).candidates()[0].backup.path
    raised = []
    for flag in (False, True):
        caplog.clear()
        with pytest.raises(RestoreError) as failure:
            app_module.restore_and_open(paths, clock, backup, log_not_performed=flag)
        error = failure.value
        raised.append((type(error), error.user_message, error.detail, type(error.__cause__)))
        assert len(logged(caplog, "budget.app", logging.WARNING, NOT_RESTORED)) == int(flag)
    assert raised[0] == raised[1]
    for side, data in leftovers.items():
        assert (paths.root / f"budget.db{side}").read_bytes() == data


def test_failure_cause_keeps_only_typed_codes(tmp_path):
    """Шляхи (з кирилицею й пробілом), SQL і дані з повідомлень у журнал не потрапляють."""
    secret = tmp_path / "Олена Коваль" / "budget.db"
    try:
        try:
            try:
                raise OSError(errno.EACCES, f"Permission denied: {secret}", str(secret))
            except OSError as exc:
                message = f"near \"SELECT amount FROM incomes WHERE name='Зарплата'\": {secret}"
                raise sqlite3.OperationalError(message) from exc
        except sqlite3.Error as exc:
            raise StorageError(detail=f"Не вдалося відкрити базу {secret}") from exc
    except StorageError as error:
        text = app_module._failure_cause(error)
    assert text == "StorageError <- OperationalError <- PermissionError [errno 13 EACCES]"

    try:  # справжня помилка SQLite: код SQLITE_CANTOPEN замість тексту повідомлення
        sqlite3.connect((tmp_path / "немає" / "x.db").as_uri() + "?mode=rw", uri=True)
    except sqlite3.OperationalError as error:
        real = app_module._failure_cause(error)
    assert real == "OperationalError [SQLITE_CANTOPEN]"
    for fragment in (str(tmp_path), "Олена", "SELECT", "Зарплата", "Permission denied", "unable"):
        assert fragment not in text and fragment not in real


def test_failure_cause_respects_exception_chain_visibility():
    """Прихований контекст ігнорується, явна причина має пріоритет."""
    hidden = RuntimeError("outer")
    hidden.__context__ = ValueError("hidden secret")
    hidden.__suppress_context__ = True

    assert app_module._failure_cause(hidden) == "RuntimeError"

    visible = RuntimeError("outer")
    visible.__context__ = ValueError("internal")

    assert app_module._failure_cause(visible) == "RuntimeError <- ValueError"

    explicit = RuntimeError("outer")
    explicit.__context__ = ValueError("context")
    explicit.__cause__ = KeyError("explicit")

    assert app_module._failure_cause(explicit) == "RuntimeError <- KeyError"

class FailingStatAfterSetAside:
    """D1: ``Path.exists`` для вже відкладених файлів дає ``OSError`` (EIO) — лише у вікні
    між поверненням ``set_aside_database`` і наступним кроком основного потоку. Поза вікном
    і для інших файлів — справжній ``Path.exists``. ``hits`` — спроби діагностики в
    цьому вікні; ``events`` — порядок основних операцій."""

    def __init__(self, monkeypatch, module, *, restore: bool = False) -> None:
        self.open = False
        self.hits: list[str] = []
        self.events: list[str] = []
        original_set_aside = module.set_aside_database

        def set_aside(*args, **kwargs):
            result = original_set_aside(*args, **kwargs)
            self.events.append("set_aside")
            self.open = True
            return result

        monkeypatch.setattr(module, "set_aside_database", set_aside)
        original_exists = Path.exists

        def exists(path, *args, **kwargs):
            if self.open and ".orphaned-" in path.name and os.path.lexists(path):
                self.hits.append(path.name)
                raise OSError(errno.EIO, "Input/output error", str(path))
            return original_exists(path, *args, **kwargs)

        monkeypatch.setattr(Path, "exists", exists)
        if restore:
            original_put_back = RecoveryService._put_back

            def put_back(service, set_aside_path):
                self.events.append("put_back")
                return original_put_back(service, set_aside_path)

            monkeypatch.setattr(RecoveryService, "_put_back", put_back)

    def close(self) -> None:
        self.open = False

    def assert_would_fail(self, path) -> None:
        """Сама імітація справжня: у вікні ``exists`` відкладеного файлу — ``OSError``."""
        self.open = True
        with pytest.raises(OSError):
            path.exists()
        self.open = False
        self.hits.pop()


def test_start_empty_succeeds_without_stat_of_set_aside_files(
    qtbot, paths, clock, monkeypatch, split_quarantine, confirmations, caplog, tmp_path
):
    """D1: після вдалого перенесення журнал не звертається до файлової системи, тож збій
    ``stat`` відкладених файлів не змінює успішного порожнього старту."""
    _, leftovers = split_quarantine
    caplog.set_level(logging.INFO)
    probe = FailingStatAfterSetAside(monkeypatch, startup_module)
    Dialogs(monkeypatch, start_empty)
    session, result = launch(paths, clock)
    probe.close()
    session.close()
    assert result == (None, False) and paths.database.exists()
    assert probe.events == ["set_aside"] and probe.hits == []
    orphaned = sorted(p for p in paths.root.iterdir() if ".orphaned-" in p.name)
    assert {p.name[-4:]: p.read_bytes() for p in orphaned} == leftovers
    (entry,) = logged(caplog, "budget.app", logging.INFO, SET_ASIDE_EMPTY)
    assert entry.getMessage() == SET_ASIDE_EMPTY + set_aside_names(paths)
    assert_without_paths([entry], paths.root, tmp_path)
    probe.assert_would_fail(orphaned[0])


def test_restore_keeps_its_order_without_stat_of_set_aside_files(
    qtbot, paths, clock, monkeypatch, split_quarantine, confirmations, caplog, tmp_path
):
    """D1: після відкладення діагностика не перериває ``set_aside`` →
    ``restore_from_backup`` → ``_put_back``; невдача встановлення лишається тією самою
    ``RestoreError``, а відкат повертає залишки під старі назви."""
    _, leftovers = split_quarantine
    caplog.set_level(logging.INFO)
    probe = FailingStatAfterSetAside(monkeypatch, backup_service_module, restore=True)

    def no_space(backup_path, database_path):
        probe.events.append("restore_from_backup")
        probe.close()  # наступний крок основного потоку: далі — справжній ``exists``
        try:
            raise OSError(28, "No space left on device")
        except OSError as exc:
            raise StorageError(detail=f"Відновлення не вдалося: {exc}") from exc

    monkeypatch.setattr(backup_service_module, "restore_from_backup", no_space)
    chosen = []
    Dialogs(monkeypatch, restore_recording(chosen))
    _, result = launch(paths, clock)
    assert probe.events == ["set_aside", "restore_from_backup", "put_back"]
    assert probe.hits == []
    assert result == (EXIT_DATA_CORRUPTED, False) and not paths.database.exists()
    for side, data in leftovers.items():
        assert (paths.root / f"budget.db{side}").read_bytes() == data
    assert not [p for p in paths.root.iterdir() if ".orphaned-" in p.name]
    (aside,) = logged(caplog, "budget.services.backup", logging.INFO, SET_ASIDE_RESTORE)
    assert aside.getMessage().startswith(SET_ASIDE_RESTORE + "budget.db.orphaned-")
    (entry,) = logged(caplog, "budget.app", logging.WARNING, NOT_RESTORED)
    assert entry.getMessage() == (
        f"Restore from {chosen[0]} was not performed: "
        "RestoreError <- StorageError <- OSError [errno 28 ENOSPC]"
    )
    assert_without_paths([aside, entry], paths.root, tmp_path)


# T10. Кожна ознака окремо --------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "budget.db.corrupted-20261005-120000",
        "budget.db.replaced-20261005-120000",
        "budget.db.orphaned-20261005-120000-wal",
        "budget.db.restoring",
        "budget.db-wal",
        "budget.db-shm",
        "backups/budget-20261005-120000-on-demand.db",
    ],
)
def test_each_indicator_alone_blocks_a_new_database(qtbot, paths, clock, monkeypatch, name):
    target = paths.root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"previous state")
    dialogs = Dialogs(monkeypatch, cancel)
    _, result = launch(paths, clock)
    assert result == (EXIT_DATA_CORRUPTED, False) and len(dialogs.shown) == 1
    assert not paths.database.exists() and target.read_bytes() == b"previous state"


# T11. Давній карантин поруч зі справною базою ------------------------------------------------


def test_old_quarantine_next_to_a_healthy_database_is_ignored(qtbot, paths, clock, monkeypatch):
    make_history(paths, clock)
    (paths.root / "budget.db.corrupted-20250101-120000").write_bytes(b"old incident")
    Dialogs(monkeypatch, lambda d: pytest.fail("діалогу не має бути"))
    session, result = launch(paths, clock)
    try:
        assert result == (None, False)
        assert InitialSetupService(session.connection).is_completed()
    finally:
        session.close()


# T12. Базу видалено вручну, копії лишилися -----------------------------------------------------


def test_manually_deleted_database_with_backups_opens_the_dialog(
    qtbot, paths, clock, monkeypatch, watch
):
    make_history(paths, clock)
    paths.database.unlink()
    assert not (paths.root / "budget.db-wal").exists()
    dialogs = Dialogs(monkeypatch, cancel)
    watch["reset"]()
    _, result = launch(paths, clock)
    assert result == (EXIT_DATA_CORRUPTED, False) and len(dialogs.shown) == 1
    assert not paths.database.exists() and not watch["database_opened"]()


# T13. Явне «Почати з порожніми даними» ------------------------------------------------------


def test_start_empty_needs_confirmation_and_keeps_previous_files(
    qtbot, paths, clock, monkeypatch, confirmations
):
    make_history(paths, clock, wal_balance=4242)
    quarantined = quarantine(paths, clock)
    (paths.root / "budget.db-wal").write_bytes(b"leftover wal")  # ще й залишок поруч
    kept_quarantine = {p.name: p.read_bytes() for p in paths.root.glob("budget.db.corrupted-*")}
    kept_backups = backup_files(paths)
    dialogs = Dialogs(monkeypatch, start_empty)
    clock.set(START + timedelta(days=2))
    session, result = launch(paths, clock)
    try:
        assert confirmations == [START_EMPTY_CONFIRMATION]  # саме підтвердження
        assert result == (None, False) and dialogs.shown[0]["start_empty"]
        assert paths.database.exists()
        assert not InitialSetupService(session.connection).is_completed()  # майстер
    finally:
        session.close()
    (orphan,) = list(paths.root.glob("budget.db.orphaned-*-wal"))
    assert orphan.read_bytes() == b"leftover wal"
    assert {
        p.name: p.read_bytes() for p in paths.root.glob("budget.db.corrupted-*")
    } == kept_quarantine and quarantined.exists()
    after = backup_files(paths)
    assert {name: after[name] for name in kept_backups} == kept_backups  # старі копії ті самі
    # Наступний запуск — звичайний: база є.
    Dialogs(monkeypatch, lambda d: pytest.fail("діалогу не має бути"))
    session, result = launch(paths, clock)
    session.close()
    assert result == (None, False)


def test_declined_start_empty_confirmation_creates_nothing(qtbot, paths, clock, monkeypatch):
    make_history(paths, clock)
    quarantine(paths, clock)
    monkeypatch.setattr(QMessageBox, "exec", lambda box: 0)
    monkeypatch.setattr(
        QMessageBox,
        "clickedButton",
        lambda box: next(
            b for b in box.buttons() if box.buttonRole(b) == QMessageBox.ButtonRole.RejectRole
        ),
    )
    accepted = []

    def decline_then_close(dialog):
        dialog.start_empty_selected()
        accepted.append(dialog.result())
        return 0

    Dialogs(monkeypatch, decline_then_close)
    _, result = launch(paths, clock)
    assert accepted == [0] and result == (EXIT_DATA_CORRUPTED, False)
    assert not paths.database.exists()


def test_start_empty_button_only_in_the_missing_database_dialog(qtbot):
    missing = RecoveryDialog(None, lambda: [], lambda c: c, start_empty=lambda: None)
    startup = RecoveryDialog("x", lambda: [], lambda c: c)
    runtime = RecoveryDialog("x", lambda: [], lambda c: c, runtime=True)
    for dialog in (missing, startup, runtime):
        qtbot.addWidget(dialog)
    assert missing.start_empty_button.text() == START_EMPTY
    assert startup.start_empty_button is None and runtime.start_empty_button is None
    texts = [label.text() for label in missing.findChildren(type(missing.empty))]
    assert MISSING_DATABASE_EXPLANATION in texts
    assert not any("зберіг його як" in t or "збережено як" in t for t in texts)


@pytest.mark.parametrize(
    ("mode", "title", "explanation"),
    [
        pytest.param(
            "missing", "Базу даних не знайдено", MISSING_DATABASE_EXPLANATION, id="missing"
        ),
        pytest.param(
            "startup", "Дані пошкоджено", STARTUP_EXPLANATION.format(name="x"), id="startup"
        ),
        pytest.param(
            "runtime", "Дані пошкоджено", RUNTIME_EXPLANATION.format(name="x"), id="runtime"
        ),
    ],
)
def test_dialog_title_matches_the_mode(qtbot, mode, title, explanation):
    """F11: заголовок вікна й верхній рядок — за режимом; пояснення й кнопки ті самі."""
    if mode == "missing":
        dialog = RecoveryDialog(None, lambda: [], lambda c: c, start_empty=lambda: None)
    else:
        dialog = RecoveryDialog("x", lambda: [], lambda c: c, runtime=mode == "runtime")
    qtbot.addWidget(dialog)
    assert dialog.windowTitle() == title
    assert headings(dialog) == [title]
    texts = [label.text() for label in dialog.findChildren(type(dialog.empty))]
    assert explanation in texts
    if mode == "missing":
        assert "Дані пошкоджено" not in texts and dialog.windowTitle() != "Дані пошкоджено"
    assert dialog.close_button.text() == "Закрити застосунок"
    assert dialog.restore_button.text() == "Відновити з вибраної копії"
    assert (dialog.start_empty_button is not None) == (mode == "missing")


# T14. Невдале відкладення залишків ------------------------------------------------------------


def test_failed_set_aside_creates_no_database(qtbot, paths, clock, monkeypatch, confirmations):
    paths.root.mkdir(parents=True)
    (paths.root / "budget.db-wal").write_bytes(b"wal")

    def locked(source, target):
        raise StorageError(detail="файл зайнятий іншим процесом")

    monkeypatch.setattr(recovery_module, "move_database_files", locked)
    failures = []

    def try_start_empty(dialog):
        dialog.start_empty_selected()
        failures.append((dialog.result(), dialog.failure.isHidden(), dialog.failure.body.text()))
        return dialog.result()

    Dialogs(monkeypatch, try_start_empty)
    _, result = launch(paths, clock)
    assert failures == [(0, False, ORPHANS_NOT_SET_ASIDE_MESSAGE)]
    assert result == (EXIT_DATA_CORRUPTED, False)
    assert not paths.database.exists()
    assert (paths.root / "budget.db-wal").read_bytes() == b"wal"


# T15. Невідомий стан теки даних -----------------------------------------------------------------


@pytest.mark.parametrize("unreadable", ["root", "backups"])
def test_unreadable_data_directory_stops_without_a_database(
    qtbot, paths, clock, monkeypatch, watch, messages, unreadable
):
    paths.backups.mkdir(parents=True)
    target = paths.root if unreadable == "root" else paths.backups
    original = os.scandir

    def scandir(path="."):
        if os.fspath(path) == os.fspath(target):
            raise PermissionError(13, "доступ заборонено", os.fspath(path))
        return original(path)

    monkeypatch.setattr(startup_module.os, "scandir", scandir)
    dialogs = Dialogs(monkeypatch, lambda d: pytest.fail("діалогу не має бути"))
    watch["reset"]()
    session, result = launch(paths, clock)
    assert result == (EXIT_STARTUP_FAILED, False) and not session.is_open
    assert messages == [DATA_DIRECTORY_UNKNOWN_MESSAGE] and dialogs.shown == []
    assert not paths.database.exists() and not watch["database_opened"]()
    assert watch["migrate"] == watch["automatic"] == []


def test_backup_list_failing_after_the_precheck_stops_without_a_database(
    qtbot, paths, clock, monkeypatch, watch, messages
):
    """F9: попередня перевірка ``backups/`` пройшла, а читання переліку копій — ні. Це не
    «ознак немає»: контрольований вихід без нової порожньої бази."""
    paths.backups.mkdir(parents=True)
    (paths.backups / "budget-20261005-120000-daily.db").write_bytes(b"copy")  # єдина ознака
    original = os.scandir
    calls = []

    def scandir(path="."):
        if os.fspath(path) == os.fspath(paths.backups):
            calls.append(path)
            if len(calls) > 1:  # перша — попередня перевірка, друга — сам перелік
                raise PermissionError(13, "доступ заборонено", os.fspath(path))
        return original(path)

    monkeypatch.setattr(os, "scandir", scandir)
    dialogs = Dialogs(monkeypatch, lambda d: pytest.fail("діалогу не має бути"))
    watch["reset"]()
    session, result = launch(paths, clock)
    assert len(calls) == 2
    assert result == (EXIT_STARTUP_FAILED, False) and not session.is_open
    assert messages == [DATA_DIRECTORY_UNKNOWN_MESSAGE] and dialogs.shown == []
    assert not paths.database.exists() and not watch["database_opened"]()
    assert watch["migrate"] == watch["automatic"] == []


# T16. Windows: залишковий -wal заблоковано іншим процесом ----------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="блокування відкритого файлу — лише Windows")
def test_locked_leftover_wal_on_windows(qtbot, paths, clock, monkeypatch, confirmations):
    paths.root.mkdir(parents=True)
    wal = paths.root / "budget.db-wal"
    wal.write_bytes(b"wal bytes")
    (paths.root / "budget.db-shm").write_bytes(b"shm bytes")
    failures = []

    def try_start_empty(dialog):
        dialog.start_empty_selected()
        failures.append((dialog.result(), dialog.failure.isHidden()))
        return dialog.result()

    Dialogs(monkeypatch, try_start_empty)
    with wal.open("rb"):  # відкритий дескриптор не дає перейменувати файл
        _, result = launch(paths, clock)
    assert failures == [(0, False)] and result == (EXIT_DATA_CORRUPTED, False)
    assert not paths.database.exists()
    assert wal.read_bytes() == b"wal bytes"
    assert (paths.root / "budget.db-shm").read_bytes() == b"shm bytes"
    assert not list(paths.root.glob("budget.db.orphaned-*"))  # пара не розірвана


def test_close_without_checkpoint_helper_matches_production(paths, clock):
    """Самоперевірка фікстури: WAL справді лишається після закриття без checkpoint."""
    make_history(paths, clock, wal_balance=7)
    assert (paths.root / "budget.db-wal").stat().st_size > 0
    connection = sqlite3.connect(paths.database)
    try:
        assert connection.execute("SELECT balance FROM general_remainder").fetchone() == (7,)
    finally:
        close_without_checkpoint(connection)
