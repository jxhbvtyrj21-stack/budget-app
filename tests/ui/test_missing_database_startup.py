"""Запуск без робочої бази, але з ознаками попередньої (R1).

Справжній ``start_session`` (і для T2/T7 — справжній ``run_application``), справжні
файли теки даних. До рішення користувача в діалозі — жодного відкриття ``budget.db``,
міграцій, автоматичних копій і ротації. Діалог керується через його ``exec``.
"""

import os
import shutil
import sqlite3
import sys
from datetime import UTC, datetime, timedelta

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

import budget.app as app_module
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
from budget.services.backup import BackupKind, BackupService, RecoveryService
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
        seen["connect"].append(str(target))
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
                    "start_empty": dialog.start_empty_button is not None,
                    "candidates": dialog.list.count(),
                    "empty_visible": not dialog.empty.isHidden(),
                }
            )
            return recorder.action(dialog)

        monkeypatch.setattr(RecoveryDialog, "exec", exec_)


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
    before = files(paths.root)
    watch["reset"]()
    session, result = launch(paths, clock)  # наступний запуск
    assert result == (EXIT_DATA_CORRUPTED, False) and not session.is_open
    assert dialogs.shown[1]["mode"] == "missing"
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
        app_module, "restore_and_open", lambda *a: returned.append(original(*a)) or returned[-1]
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
