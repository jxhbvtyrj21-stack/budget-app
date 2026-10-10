"""Невдачі відновлення під час роботи (Blocks C5.3, C6).

Той самий справжній ланцюжок, що й у ``test_runtime_restore.py``: пошкодження з реального
слоту → C3 → карантин → ``RecoveryDialog`` → ``restore_and_open``. Невдачі вносяться лише
в конкретну точку виробничого коду, що відповідає етапу: заміна файлу (``os.replace``
для цільової бази), відкриття відновленої бази, ``check_restored``, відкат
(``discard_database`` цільової бази), ``adopt``, ``AppServices.create``,
``MainWindow.replace_services``.
"""

import errno
import logging
import sqlite3
import sys
from pathlib import Path

import pytest
import test_runtime_restore as c52
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

import budget.app as app_module
from budget.app import (
    APPLICATION_WILL_CLOSE,
    EXIT_DATA_CORRUPTED,
    RESTORE_ABORTED_MESSAGE,
    RESTORE_INCOMPLETE_MESSAGE,
    ApplicationSession,
    RuntimeCorruptionGuard,
)
from budget.errors import StorageError
from budget.services import backup as backup_service_module
from budget.services.backup import ROLLBACK_FAILED_MESSAGE, BackupKind, RestoreError, find_backups
from budget.services.facade import AppServices
from budget.storage import recovery as recovery_module
from budget.storage.recovery import database_files
from budget.ui.main_window import MainWindow

# Спільні фікстури й допоміжні функції успішного шляху (C5.2) — без змін.
running = c52.running
previous_hook = c52.previous_hook
confirmed = c52.confirmed
replaced = c52.replaced
transitions = c52.transitions
long_gap_dialogs = c52.long_gap_dialogs


def corrupt_and_recover(qtbot, app, action, seen, previous_hook) -> None:
    """Як у C5.2, але помилка в самій дії тесту не лишає модальний діалог відкритим:
    діалог закривається, а виняток доходить до ``previous_hook`` — тест падає, а не висне."""

    def guarded(dialog):
        try:
            action(dialog)
        except BaseException:
            if dialog.isVisible():
                dialog.reject()
            raise

    c52.corrupt_and_recover(qtbot, app, guarded, seen, previous_hook)


snapshot = c52.snapshot
state = c52.state
is_closed = c52.is_closed
visible_recovery_dialogs = c52.visible_recovery_dialogs

UNCHANGED = "залишився без змін"


@pytest.fixture
def attempts(monkeypatch):
    """Справжній ``restore_and_open``: шлях копії, повернуте з'єднання або виняток."""
    calls = []
    original = app_module.restore_and_open

    def restore_and_open(paths, clock, backup_path):
        calls.append(("call", backup_path))
        try:
            connection = original(paths, clock, backup_path)
        except BaseException as error:
            calls.append(("raised", error))
            raise
        calls.append(("returned", connection))
        return connection

    monkeypatch.setattr(app_module, "restore_and_open", restore_and_open)
    return calls


def values(calls, kind: str) -> list:
    return [value for k, value in calls if k == kind]


@pytest.fixture
def connections(monkeypatch):
    """Усі ``sqlite3.connect`` після початку тесту, у порядку відкриття."""
    opened = []
    original = sqlite3.connect

    def connect(target, *args, **kwargs):
        connection = original(target, *args, **kwargs)
        opened.append((str(target), connection))
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    return opened


@pytest.fixture
def rearms(monkeypatch):
    calls = []
    original = RuntimeCorruptionGuard.rearm

    def rearm(self):
        calls.append(self)
        original(self)

    monkeypatch.setattr(RuntimeCorruptionGuard, "rearm", rearm)
    return calls


def open_to_database(connections, app) -> list[sqlite3.Connection]:
    return [c for t, c in connections if t == str(app.paths.database) and not is_closed(c)]


def quarantined(app) -> list[str]:
    return sorted(p.name for p in app.paths.root.iterdir() if ".corrupted-" in p.name)


def suspended(app, guard_detected: bool = True) -> dict:
    """Інваріанти «до успішного відновлення»."""
    return {
        "window_enabled": app.window.isEnabled(),
        "session_open": app.session.is_open,
        "database_files": [p.name for p in database_files(app.paths.database) if p.exists()],
        "restoring_files": [
            p.name
            for p in database_files(app.paths.database.with_name("budget.db.restoring"))
            if p.exists()
        ],
        "quarantined": bool(quarantined(app)),
        "guard_detected": app.guard.detected,
        "visible_dialogs": len(visible_recovery_dialogs()),
    }


SUSPENDED = {
    "window_enabled": False,
    "session_open": False,
    "database_files": [],
    "restoring_files": [],
    "quarantined": True,
    "guard_detected": True,
    "visible_dialogs": 1,
}


def fail_once(monkeypatch, target, name, error_factory, when=lambda *a, **k: True):
    """Одна невдача в конкретній точці виробничого коду; далі — справжня поведінка."""
    original = getattr(target, name)
    used = []

    def failing(*args, **kwargs):
        if not used and when(*args, **kwargs):
            used.append(True)
            raise error_factory()
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, failing)
    return used


def inject_retryable(stage: str, app, monkeypatch) -> list:
    if stage == "replace":
        # Справжня заміна тимчасового файлу на місце бази не вдається (напр., Windows-блок).
        return fail_once(
            monkeypatch,
            recovery_module.os,
            "replace",
            lambda: PermissionError(errno.EACCES, "ціль зайнята"),
            when=lambda src, dst: Path(dst) == app.paths.database,
        )
    if stage == "open":
        return fail_once(
            monkeypatch,
            app_module,
            "open_application_database",
            lambda: StorageError(detail="відновлена база не відкрилася (введено тестом)"),
        )
    return fail_once(
        monkeypatch,
        backup_service_module.RecoveryService,
        "check_restored",
        lambda: RestoreError(detail="integrity_check не пройдено (введено тестом)"),
    )


def candidate_rows(dialog) -> list:
    return [dialog.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(dialog.list.count())]


# Повторна спроба після невдачі з успішним відкатом -------------------------------------------


@pytest.mark.parametrize("stage", ["replace", "open", "integrity"])
def test_failed_restore_keeps_dialog_and_another_backup_succeeds(
    qtbot,
    running,
    confirmed,
    attempts,
    replaced,
    previous_hook,
    connections,
    rearms,
    transitions,
    long_gap_dialogs,
    monkeypatch,
    stage,
):
    app = running
    injected = inject_retryable(stage, app, monkeypatch)
    between, chosen = {}, {}

    def fail_then_choose_another(dialog):
        chosen["first"] = candidate_rows(dialog)[1]
        dialog.list.setCurrentRow(1)
        dialog.restore_selected()  # невдача
        between["state"] = suspended(app)
        between["dialog_visible"] = dialog.isVisible()
        between["failure"] = dialog.failure.isVisible() and dialog.failure.body.text()
        between["rearms"] = len(rearms)
        between["open_connections"] = open_to_database(connections, app)
        # Інша копія — вересенева «на вимогу», вибрана в оновленому переліку.
        rows = candidate_rows(dialog)
        between["listed"] = [c.backup.path for c in rows]
        row = next(i for i, c in enumerate(rows) if c.backup.path == app.backup)
        chosen["second"] = rows[row]
        dialog.list.setCurrentRow(row)
        dialog.restore_selected()

    seen = []
    with app.guard:
        corrupt_and_recover(qtbot, app, fail_then_choose_another, seen, previous_hook)
    c52.flush_deletions()
    assert injected == [True]
    # Між спробами: діалог відкритий із причиною, решта — «до успішного відновлення».
    assert between["state"] == SUSPENDED and between["dialog_visible"] is True
    assert "Не вдалося відновити дані з вибраної копії" in between["failure"]
    assert between["rearms"] == 0 and between["open_connections"] == []
    # Друга спроба — саме з новим вибраним кандидатом; один діалог на все відновлення.
    assert values(attempts, "call") == [chosen["first"].backup.path, chosen["second"].backup.path]
    assert len(seen) == 1 and visible_recovery_dialogs() == []
    (returned,) = values(attempts, "returned")
    assert app.session.connection is returned
    assert open_to_database(connections, app) == [returned]  # рівно одне з'єднання
    (created,) = replaced["create"]
    assert created[0] is returned and replaced["replace"] == [created[1]]
    assert app.window.isEnabled() and not app.guard.detected and len(rearms) == 1
    assert app.exits == [] and app.errors == [] and previous_hook == []
    # Саме стан вересневої копії, без жовтневого переходу й діалогу тривалої перерви.
    assert transitions == [] and long_gap_dialogs == []
    assert state(created[1], app.month) == app.expected_state
    assert snapshot(returned) == app.expected


@pytest.mark.parametrize("stage", ["replace", "open", "integrity"])
def test_cancel_after_failed_restore_exits_data_corrupted(
    qtbot, running, confirmed, attempts, previous_hook, connections, rearms, monkeypatch, stage
):
    app = running
    inject_retryable(stage, app, monkeypatch)

    def fail_then_cancel(dialog):
        dialog.restore_selected()
        c52.cancel(dialog)

    with app.guard:
        corrupt_and_recover(qtbot, app, fail_then_cancel, [], previous_hook)
    assert app.exits == [EXIT_DATA_CORRUPTED] and app.errors == []
    assert len(values(attempts, "call")) == 1  # жодного додаткового відновлення
    assert {**suspended(app), "visible_dialogs": 0} == {**SUSPENDED, "visible_dialogs": 0}
    assert not app.paths.database.exists()  # порожньої бази в цьому процесі не створено
    assert open_to_database(connections, app) == [] and rearms == []
    assert previous_hook == []


@pytest.mark.parametrize("broken", ["missing", "garbage"])
def test_candidate_that_became_invalid_is_a_controlled_failure(
    qtbot, running, confirmed, attempts, previous_hook, connections, rearms, broken
):
    app = running
    seen, recorded = [], {}

    def break_selected_then_choose_another(dialog):
        selected = candidate_rows(dialog)[0]
        recorded["broken"] = selected.backup.path
        if broken == "missing":
            selected.backup.path.unlink()
        else:
            selected.backup.path.write_bytes(b"not a database" * 600)
        dialog.list.setCurrentRow(0)
        dialog.restore_selected()
        recorded["state"] = suspended(app)
        recorded["failure"] = dialog.failure.body.text()
        recorded["listed"] = [c.backup.path for c in candidate_rows(dialog)]
        dialog.list.setCurrentRow(0)
        recorded["second"] = dialog.selected().backup.path
        dialog.restore_selected()

    with app.guard:
        corrupt_and_recover(qtbot, app, break_selected_then_choose_another, seen, previous_hook)
    assert recorded["state"] == SUSPENDED
    assert "Вибрана копія пошкоджена або не підходить" in recorded["failure"]
    assert recorded["broken"] not in recorded["listed"]  # перелік оновлено
    assert values(attempts, "call") == [recorded["broken"], recorded["second"]]
    assert app.window.isEnabled() and app.session.is_open and len(rearms) == 1
    assert open_to_database(connections, app) == [app.session.connection]
    assert len(seen) == 1 and app.exits == [] and previous_hook == []


# Невдача після успішного restore_and_open ----------------------------------------------------


def inject_after_restore(stage: str, monkeypatch) -> None:
    def broken(*args, **kwargs):
        raise RuntimeError(f"введена невдача: {stage}")

    target = {
        "session.adopt": (ApplicationSession, "adopt"),
        "AppServices.create": (AppServices, "create"),
        "MainWindow.replace_services": (MainWindow, "replace_services"),
    }[stage]
    monkeypatch.setattr(*target, broken)


@pytest.mark.parametrize(
    "stage", ["session.adopt", "AppServices.create", "MainWindow.replace_services"]
)
def test_failure_after_restore_is_terminal_and_closes_the_connection(
    qtbot,
    running,
    confirmed,
    attempts,
    previous_hook,
    connections,
    rearms,
    transitions,
    long_gap_dialogs,
    monkeypatch,
    caplog,
    stage,
):
    app = running
    inject_after_restore(stage, monkeypatch)
    with caplog.at_level(logging.INFO), app.guard:
        corrupt_and_recover(qtbot, app, c52.choose(0), [], previous_hook)
    (returned,) = values(attempts, "returned")
    assert is_closed(returned) and not app.session.is_open
    assert open_to_database(connections, app) == []
    # Після повернення з restore_and_open жодного нового з'єднання з робочою базою.
    assert [t for t, _ in connections if t == str(app.paths.database)] == [str(app.paths.database)]
    assert app.exits == [EXIT_DATA_CORRUPTED] and app.errors == [RESTORE_INCOMPLETE_MESSAGE]
    assert not app.window.isEnabled() and app.guard.detected and rearms == []
    assert visible_recovery_dialogs() == [] and previous_hook == []
    assert transitions == [] and long_gap_dialogs == []
    # Журнал: невдача, етап, первинний виняток і traceback.
    failures = [r for r in caplog.records if "failed after the restore" in r.getMessage()]
    assert len(failures) == 1 and failures[0].exc_info is not None
    logged = caplog.text
    assert f"введена невдача: {stage}" in logged and "Traceback" in logged
    assert stage in logged
    assert app.paths.database.exists()  # відновлена база лишається на диску


# Відкат (C6) ---------------------------------------------------------------------------------


def test_rollback_failed_message_makes_no_claim_about_backups():
    """Невдала спроба може додати копії (перед міграцією, автоматичні) і запустити ротацію,
    тож незмінність резервних копій не є інваріантом і не стверджується."""
    for claim in ("Резервні копії", "резервні копії", "не змінено", "без змін"):
        assert claim not in ROLLBACK_FAILED_MESSAGE, claim
    assert "Не вдалося відновити дані з вибраної копії" in ROLLBACK_FAILED_MESSAGE
    assert "повернути не вдалося" in ROLLBACK_FAILED_MESSAGE


def test_rollback_failure_is_terminal_and_reported_honestly(
    qtbot, running, confirmed, attempts, previous_hook, connections, rearms, monkeypatch, caplog
):
    app = running
    inject_retryable("integrity", app, monkeypatch)
    # Відкат не може прибрати відновлену базу (напр., файл тримає інший процес).
    fail_once(
        monkeypatch,
        backup_service_module,
        "discard_database",
        lambda: PermissionError(errno.EACCES, "відновлена база зайнята"),
        when=lambda path: path == app.paths.database,
    )
    backups_before = sorted(p.name for p in app.paths.backups.iterdir())
    with caplog.at_level(logging.INFO), app.guard:
        corrupt_and_recover(qtbot, app, c52.choose(0), [], previous_hook)
    (error,) = values(attempts, "raised")
    # Невдала спроба справді змінила набір копій: до невдалої перевірки відновлення
    # встигло створити автоматичні копії поточного періоду (жовтень).
    backups_after = sorted(p.name for p in app.paths.backups.iterdir())
    added = sorted(set(backups_after) - set(backups_before))
    assert added and all(name.startswith("budget-20261006-") for name in added)
    # Тому повідомлення — лише гарантовані факти: ні «залишився без змін», ні
    # «резервні копії не змінено».
    assert error.user_message == ROLLBACK_FAILED_MESSAGE
    assert app.errors == [f"{ROLLBACK_FAILED_MESSAGE} {APPLICATION_WILL_CLOSE}"]
    for claim in (UNCHANGED, "не змінено", "Резервні копії"):
        assert claim not in app.errors[0], claim
    # Первинна причина — невдала перевірка; вторинна — невдалий відкат; обидві збережені.
    assert isinstance(error.__cause__, RestoreError)
    assert "integrity_check не пройдено" in error.__cause__.detail
    rollback = error.__context__
    assert isinstance(rollback, RestoreError) and isinstance(rollback.__cause__, PermissionError)
    assert "integrity_check не пройдено" in error.detail and "зайнята" in error.detail
    logged = caplog.text
    assert "Restored database failed the integrity check" in logged
    assert "Rollback after failed restore failed" in logged
    assert "Runtime restore failed and its rollback failed too" in logged
    # Кінцевий стан: діалог закрито, вихід, нічого не відновлено «понарошку».
    assert app.exits == [EXIT_DATA_CORRUPTED] and visible_recovery_dialogs() == []
    assert not app.window.isEnabled() and app.guard.detected and rearms == []
    assert not app.session.is_open and open_to_database(connections, app) == []
    assert quarantined(app) and previous_hook == []
    assert BackupKind.BEFORE_RESTORE not in {b.kind for b in find_backups(app.paths.backups)}


def test_successful_rollback_message_does_not_claim_old_state_returned(
    qtbot, running, confirmed, attempts, previous_hook, monkeypatch
):
    """Відкат удався: цільової бази немає, карантин цілий, повідомлення — про файли,
    а не про повернення робочого з'єднання; діалог і далі придатний."""
    app = running
    inject_retryable("integrity", app, monkeypatch)
    recorded = {}

    def fail_then_cancel(dialog):
        recorded["quarantine_before"] = quarantined(app)
        dialog.restore_selected()
        recorded["quarantine_after"] = quarantined(app)
        recorded["state"] = suspended(app)
        recorded["failure"] = dialog.failure.body.text()
        recorded["can_retry"] = dialog.restore_button.isEnabled() and dialog.list.count() > 0
        c52.cancel(dialog)

    with app.guard:
        corrupt_and_recover(qtbot, app, fail_then_cancel, [], previous_hook)
    (error,) = values(attempts, "raised")
    assert error.user_message != ROLLBACK_FAILED_MESSAGE and error.__context__ is None
    assert recorded["state"] == SUSPENDED and recorded["can_retry"] is True
    assert "з'єднання" not in recorded["failure"]
    assert (
        recorded["quarantine_before"]
        and recorded["quarantine_after"] == recorded["quarantine_before"]
    )
    assert app.exits == [EXIT_DATA_CORRUPTED]


def test_unexpected_error_during_restore_is_terminal(
    qtbot, running, confirmed, attempts, previous_hook, connections, rearms, monkeypatch, caplog
):
    """Не ``BudgetError`` усередині відновлення (тимчасовий файл не прибрати): стан
    файлів не гарантований — контрольований вихід, а не виняток до ``sys.excepthook``."""
    app = running
    fail_once(
        monkeypatch,
        recovery_module,
        "discard_database",
        lambda: PermissionError(errno.EACCES, "тимчасовий файл зайнятий"),
    )
    with caplog.at_level(logging.INFO), app.guard:
        corrupt_and_recover(qtbot, app, c52.choose(0), [], previous_hook)
    (error,) = values(attempts, "raised")
    assert isinstance(error, PermissionError)
    assert app.errors == [RESTORE_ABORTED_MESSAGE] and app.exits == [EXIT_DATA_CORRUPTED]
    assert "Runtime restore failed unexpectedly" in caplog.text and "Traceback" in caplog.text
    assert previous_hook == [] and visible_recovery_dialogs() == []
    assert not app.window.isEnabled() and app.guard.detected and rearms == []
    assert open_to_database(connections, app) == [] and not app.session.is_open


# Справжній життєвий цикл файлів WAL (Windows CI запускає ці тести повністю) ----------------


def test_real_wal_lifecycle_close_quarantine_restore(
    qtbot, running, confirmed, attempts, previous_hook, connections
):
    app = running
    # Справжній стан WAL у звичайній (production) конфігурації: зафіксовані кадри лише у WAL.
    app.old_connection.execute("UPDATE general_remainder SET balance = 4242")
    assert all(p.exists() for p in database_files(app.paths.database))
    wal_before = database_files(app.paths.database)[1].read_bytes()
    assert wal_before  # у WAL справді є кадри
    c52.corrupt_pages(app.paths.database)  # той самий детермінований вміст, що й далі
    db_before = app.paths.database.read_bytes()
    with app.guard:
        corrupt_and_recover(qtbot, app, c52.choose(0), [], previous_hook)
    assert app.exits == [] and previous_hook == [] and app.window.isEnabled()
    (kept,) = [n for n in quarantined(app) if not n.endswith(("-wal", "-shm"))]
    kept_files = database_files(app.paths.root / kept)
    assert all(p.exists() for p in kept_files)  # .db, -wal, -shm перенесено разом
    # Закриття без checkpoint: пошкоджений файл не змінено, кадри WAL не перенесено в нього.
    assert kept_files[0].read_bytes() == db_before
    assert kept_files[1].read_bytes() == wal_before
    # Відновлена база: саме стан копії — старий WAL до неї не підмішався.
    (returned,) = values(attempts, "returned")
    assert app.session.connection is returned
    assert snapshot(returned) == app.expected
    assert returned.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    assert open_to_database(connections, app) == [returned]


@pytest.mark.skipif(sys.platform != "win32", reason="блокування відкритого файлу — лише Windows")
def test_windows_open_session_blocks_rename_and_close_releases_it(running):
    """У Windows відкрита база не перейменовується; після закриття без checkpoint
    (``session.close_after_corruption()``) усі три файли вільні й не змінені."""
    app = running
    app.old_connection.execute("UPDATE general_remainder SET balance = 4242")
    c52.corrupt_pages(app.paths.database)
    before = [p.read_bytes() for p in database_files(app.paths.database)[:2]]
    target = app.paths.database.with_name("budget.db.windows-check")
    with pytest.raises(PermissionError):
        app.paths.database.rename(target)
    app.session.close_after_corruption()
    present = [p for p in database_files(app.paths.database) if p.exists()]
    assert len(present) == 3  # .db, -wal, -shm після закриття лишилися
    assert [p.read_bytes() for p in present[:2]] == before  # жодного checkpoint
    for path in present:
        path.rename(path.with_name(path.name.replace("budget.db", "budget.db.windows-check")))
    assert not any(p.exists() for p in database_files(app.paths.database))
    assert all(p.exists() for p in database_files(target))


def test_no_second_recovery_dialog_or_hook_after_terminal_failure(
    qtbot, running, confirmed, previous_hook, monkeypatch
):
    app = running
    hook_before = sys.excepthook
    inject_after_restore("MainWindow.replace_services", monkeypatch)
    seen = []
    with app.guard:
        corrupt_and_recover(qtbot, app, c52.choose(0), seen, previous_hook)
        hook_during = sys.excepthook
        c52.flush_deletions()
        assert QApplication.activeModalWidget() is None
    assert len(seen) == 1 and visible_recovery_dialogs() == []
    assert hook_during == app.guard._excepthook and sys.excepthook is hook_before
