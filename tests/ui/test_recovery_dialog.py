"""Діалог «Дані пошкоджено» і шлях запуску з пошкодженою базою (DS-6; IA 10.1)."""

import errno
import os
from datetime import UTC, datetime, timedelta

import pytest
from PySide6.QtWidgets import QMessageBox

import budget.app as app_module
from budget.app import open_application_database
from budget.domain.calendar import FixedClock
from budget.domain.money import Money
from budget.errors import DatabaseCorruptedError, StorageError
from budget.platform.identity import load_product_identity
from budget.platform.paths import DataPaths
from budget.services.backup import (
    BACKUPS_UNREADABLE_MESSAGE,
    BackupKind,
    BackupService,
    RecoveryService,
    RestoreError,
    find_backups,
    restore_candidates,
)
from budget.services.facade import AppServices
from budget.services.setup import SetupDraft
from budget.ui.dialogs.recovery_dialog import (
    RecoveryDialog,
    candidate_text,
    confirmation_text,
)

START = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)


def plain(text: str) -> str:
    return text.replace(" ", " ").replace(" ", " ")


@pytest.fixture
def clock():
    return FixedClock(START)


@pytest.fixture
def paths(tmp_path):
    return DataPaths(tmp_path / "Мої дані" / "Budget")


@pytest.fixture
def candidates(paths, clock):
    """Справна база з копіями «на вимогу» й щоденною; з'єднання закрите."""
    connection = open_application_database(paths, clock)
    AppServices.create(connection, clock).setup.complete(
        SetupDraft(general_remainder=Money(250_000))
    )
    clock.set(START + timedelta(hours=2))
    BackupService(connection, paths.backups, clock).create_backup(BackupKind.ON_DEMAND)
    connection.close()
    return restore_candidates(paths.backups)


def make_dialog(qtbot, candidates, restore=None):
    calls = []

    def default(candidate):
        calls.append(candidate)
        return "connection"

    dialog = RecoveryDialog(
        "budget.db.corrupted-20261006-120000", lambda: candidates, restore or default
    )
    qtbot.addWidget(dialog)
    return dialog, calls


def answer(monkeypatch, confirmed: bool):
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    role = (
        QMessageBox.ButtonRole.DestructiveRole if confirmed else QMessageBox.ButtonRole.RejectRole
    )
    monkeypatch.setattr(
        QMessageBox,
        "clickedButton",
        lambda self: next(b for b in self.buttons() if self.buttonRole(b) == role),
    )


def test_texts_show_date_kind_and_size(candidates):
    newest = candidates[0]
    assert newest.backup.kind is BackupKind.ON_DEMAND
    text = plain(candidate_text(newest))
    assert text.startswith("6 жовтня 2026, 14:00 · На вимогу · ")
    assert text.endswith(("кБ", "МБ"))
    assert plain(confirmation_text(newest)) == (
        "Поточні дані буде замінено даними копії від 6 жовтня 2026, 14:00."
    )


def test_dialog_lists_valid_backups_newest_first(qtbot, candidates):
    dialog, _ = make_dialog(qtbot, candidates)
    assert dialog.windowTitle() == "Дані пошкоджено"
    assert dialog.list.count() == len(candidates) > 1
    texts = [dialog.list.item(i).text() for i in range(dialog.list.count())]
    assert texts == [candidate_text(c) for c in candidates]
    assert dialog.selected() is candidates[0]
    assert dialog.restore_button.isEnabled()
    assert dialog.close_button.text() == "Закрити застосунок"
    assert dialog.restore_button.text() == "Відновити з вибраної копії"


def test_dialog_without_backups(qtbot):
    dialog, _ = make_dialog(qtbot, [])
    assert dialog.list.isHidden() and not dialog.empty.isHidden()
    assert not dialog.restore_button.isEnabled()


def test_declined_confirmation_does_nothing(qtbot, candidates, monkeypatch):
    dialog, calls = make_dialog(qtbot, candidates)
    answer(monkeypatch, confirmed=False)
    dialog.restore_selected()
    assert calls == [] and dialog.result() == 0


def test_confirmed_restore_uses_selected_backup(qtbot, candidates, monkeypatch):
    dialog, calls = make_dialog(qtbot, candidates)
    dialog.list.setCurrentRow(1)
    answer(monkeypatch, confirmed=True)
    dialog.restore_selected()
    assert calls == [candidates[1]]
    assert dialog.restored == "connection" and dialog.result() == 1


def test_failed_restore_shows_error_and_stays_open(qtbot, candidates, monkeypatch):
    loads = []

    def fail(candidate):
        raise RestoreError("Не вдалося відновити дані з вибраної копії.")

    dialog = RecoveryDialog("x", lambda: loads.append(1) or candidates, fail)
    qtbot.addWidget(dialog)
    answer(monkeypatch, confirmed=True)
    dialog.restore_selected()
    assert not dialog.failure.isHidden()
    assert dialog.failure.title.text() == "Помилка"
    assert dialog.failure.body.text() == "Не вдалося відновити дані з вибраної копії."
    assert dialog.restored is None and dialog.result() == 0
    assert len(loads) == 2  # перелік оновлено після невдачі


# Шлях запуску --------------------------------------------------------------------------------


def corrupt(paths) -> None:
    paths.database.write_bytes(b"corrupted" * 1000)
    for side in ("-wal", "-shm"):
        paths.database.with_name(paths.database.name + side).unlink(missing_ok=True)


def run_recovery(paths, clock):
    with pytest.raises(DatabaseCorruptedError) as caught:
        open_application_database(paths, clock)
    return app_module._recover_corrupted_database(
        load_product_identity(), paths, clock, caught.value
    )


def test_startup_corruption_leads_to_restored_database(
    qtbot, paths, clock, candidates, monkeypatch
):
    corrupt(paths)
    answer(monkeypatch, confirmed=True)
    monkeypatch.setattr(
        RecoveryDialog, "exec", lambda self: self.restore_selected() or self.result()
    )
    clock.set(START + timedelta(days=1))
    connection = run_recovery(paths, clock)
    assert connection is not None
    assert connection.execute("SELECT balance FROM general_remainder").fetchone() == (250_000,)
    connection.close()
    kept = [p for p in paths.root.iterdir() if ".corrupted-" in p.name]
    assert [p.read_bytes()[:9] for p in kept] == [b"corrupted"]
    # Пошкоджений стан не видано за копію BEFORE_RESTORE.
    assert BackupKind.BEFORE_RESTORE not in {b.kind for b in find_backups(paths.backups)}


def test_closing_dialog_exits_without_restoring(qtbot, paths, clock, candidates, monkeypatch):
    corrupt(paths)
    monkeypatch.setattr(RecoveryDialog, "exec", lambda self: 0)
    assert run_recovery(paths, clock) is None
    assert not paths.database.exists()  # пошкоджена база не лишилася робочою
    assert [p for p in paths.root.iterdir() if ".corrupted-" in p.name]


def test_failed_quarantine_shows_message(qtbot, paths, clock, candidates, monkeypatch):
    corrupt(paths)
    messages = []
    monkeypatch.setattr(app_module, "_show_message", lambda title, text: messages.append(text))

    def locked(self):
        raise StorageError(detail="файл зайнятий")

    monkeypatch.setattr(app_module.RecoveryService, "quarantine_corrupted", locked)
    assert run_recovery(paths, clock) is None
    assert messages == [DatabaseCorruptedError.default_message]


# Перелік копій не прочитано (F9) -----------------------------------------------------------


def unreadable_backups(monkeypatch, backups_dir):
    original = os.scandir

    def scandir(path="."):
        if os.fspath(path) == os.fspath(backups_dir):
            raise PermissionError(errno.EACCES, "доступ заборонено", os.fspath(path))
        return original(path)

    monkeypatch.setattr(os, "scandir", scandir)


@pytest.mark.parametrize("mode", ["missing", "startup", "runtime"])
def test_unreadable_backup_list_is_an_error_not_no_backups(
    qtbot, tmp_path, clock, monkeypatch, mode
):
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()
    (backups_dir / "budget-20261005-120000-daily.db").write_bytes(b"copy")
    unreadable_backups(monkeypatch, backups_dir)
    recovery = RecoveryService(tmp_path / "budget.db", backups_dir, clock)
    restore_calls = []
    if mode == "missing":
        dialog = RecoveryDialog(
            None, recovery.candidates, restore_calls.append, start_empty=lambda: None
        )
    else:
        dialog = RecoveryDialog(
            "budget.db.corrupted-x",
            recovery.candidates,
            restore_calls.append,
            runtime=mode == "runtime",
        )  # створюється без винятку
    qtbot.addWidget(dialog)
    assert not dialog.failure.isHidden()
    assert dialog.failure.body.text() == BACKUPS_UNREADABLE_MESSAGE
    assert dialog.empty.isHidden()  # не «Справних резервних копій немає.»
    assert dialog.list.isHidden() and dialog.list.count() == 0
    assert not dialog.restore_button.isEnabled() and dialog.selected() is None
    dialog.restore_selected()  # відновлювати нічого
    assert restore_calls == [] and dialog.result() == 0
    assert dialog.close_button.isEnabled()
    if mode == "missing":
        assert dialog.start_empty_button.isEnabled()  # інші дії — без змін
