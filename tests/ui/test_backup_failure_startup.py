"""Недоступна тека копій не зупиняє запуск застосунку (H3).

Справжній ``run_application``: блокування, сесія, автоматичні копії (які не
вдаються), головне вікно, цикл подій і звичайне завершення. Помилка копії — не
пошкодження бази: діалогу відновлення й карантину немає.
"""

import logging
import sqlite3
from datetime import UTC, datetime, timedelta

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

import budget.app as app_module
from budget.app import EXIT_OK, open_application_database, run_application
from budget.domain.calendar import FixedClock
from budget.domain.money import Money
from budget.errors import StorageError
from budget.platform.identity import load_product_identity
from budget.platform.paths import DataPaths
from budget.services.facade import AppServices
from budget.services.setup import SetupDraft
from budget.storage.recovery import database_files
from budget.ui.dialogs.recovery_dialog import RecoveryDialog
from budget.ui.main_window import MainWindow

START = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)


def test_unavailable_backups_do_not_stop_the_application(qapp, tmp_path, monkeypatch, caplog):
    paths = DataPaths(tmp_path / "Users" / "Олена Коваль" / "AppData" / "Local" / "Budget")
    clock = FixedClock(START)
    connection = open_application_database(paths, clock)
    AppServices.create(connection, clock).setup.complete(
        SetupDraft(general_remainder=Money(100_000))
    )
    connection.close()
    for path in paths.backups.iterdir():
        path.unlink()
    paths.backups.rmdir()
    paths.backups.write_bytes(b"not a directory")  # тека копій недоступна
    clock.set(START + timedelta(days=40))
    messages, seen = [], {"dialogs": 0, "windows": []}
    monkeypatch.setattr(app_module, "_show_message", lambda title, text: messages.append(text))
    finished = []

    def poll():
        if finished:
            return
        widgets = QApplication.topLevelWidgets()
        if any(isinstance(w, RecoveryDialog) and w.isVisible() for w in widgets):
            seen["dialogs"] += 1
        windows = [w for w in widgets if isinstance(w, MainWindow) and w.isVisible()]
        if windows:
            seen["windows"] = windows
            qapp.exit(EXIT_OK)
        else:
            QTimer.singleShot(10, poll)

    QTimer.singleShot(0, poll)
    try:
        with caplog.at_level(logging.INFO):
            code = run_application(load_product_identity(), paths, clock, qapp)
    finally:
        finished.append(True)
        for window in seen["windows"]:
            window.close()
            window.deleteLater()
    assert code == EXIT_OK
    assert len(seen["windows"]) == 1 and seen["dialogs"] == 0 and messages == []
    # Усі три автоматичні копії не створені — кожна записана в журнал як помилка копії.
    failures = [r for r in caplog.records if r.getMessage().startswith("Автоматична копія")]
    assert len(failures) == 3
    assert all(type(r.exc_info[1]) is StorageError for r in failures)
    assert "Database corruption" not in caplog.text
    # Без карантину; звичайне завершення перенесло WAL (звичайне закриття) — база ціла.
    assert not [p for p in paths.root.iterdir() if ".corrupted-" in p.name]
    assert not database_files(paths.database)[1].exists()
    check = sqlite3.connect(paths.database)
    assert check.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    check.close()
