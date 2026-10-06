"""Складання застосунку: ідентичність, шляхи, база, тема, головне вікно."""

import argparse
import logging
import sqlite3
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from budget.domain.calendar import Clock, SystemClock
from budget.errors import BudgetError, DatabaseCorruptedError, StartupError
from budget.platform.identity import ProductIdentity, load_product_identity
from budget.platform.paths import DataPaths, data_paths
from budget.platform.resources import assets_dir
from budget.services.backup import BackupService, RecoveryService
from budget.services.startup import prepare_database
from budget.storage.integrity import quick_check

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ALREADY_RUNNING = 1
EXIT_STARTUP_FAILED = 2
EXIT_DATA_CORRUPTED = 3


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.self_check:
        return self_check()
    try:
        identity = load_product_identity()
        paths = data_paths(identity)
    except StartupError as exc:
        _show_fatal(exc)
        return EXIT_STARTUP_FAILED
    from budget.platform.logging_setup import configure_logging

    configure_logging(paths.logs)
    return _run_gui(identity, paths, SystemClock())


def self_check() -> int:
    """Перевірка зібраного застосунку без екрана (AC-6): ідентичність, схема, модулі Qt."""
    try:
        load_product_identity()
        with tempfile.TemporaryDirectory() as directory:
            connection = prepare_database(
                Path(directory) / "budget.db", Path(directory) / "backups", SystemClock()
            )
            healthy = quick_check(connection)
            connection.close()
        import budget.ui.main_window  # noqa: F401  — перевірка наявності Qt у збірці
    except Exception as exc:
        print(f"self-check failed: {exc}", file=sys.stderr)
        return EXIT_STARTUP_FAILED
    return EXIT_OK if healthy else EXIT_STARTUP_FAILED


def open_application_database(paths: DataPaths, clock: Clock) -> sqlite3.Connection:
    """Відкриває й готує базу, потім створює автоматичні копії поточного періоду (DS-5).

    Пошкодження, виявлене повним ``integrity_check`` перед копією, — ``DatabaseCorruptedError``:
    з'єднання закривається без жодного запису, далі — наявний карантин (DS-6).
    """
    connection = prepare_database(paths.database, paths.backups, clock)
    try:
        BackupService(connection, paths.backups, clock).run_automatic()
    except BaseException:
        connection.close()
        raise
    return connection


def build_main_window(identity: ProductIdentity, connection: sqlite3.Connection, clock: Clock):
    """Створює головне вікно з темою; фінансові екрани — лише після налаштування.

    Перед побудовою виконується перехід між місяцями для доходів (ADR 0009).
    """
    from PySide6.QtWidgets import QApplication

    from budget.services.facade import AppServices
    from budget.ui.main_window import MainWindow
    from budget.ui.theme.stylesheet import build_stylesheet
    from budget.ui.theme.typography import register_fonts

    application = QApplication.instance()
    families = register_fonts(assets_dir() / "fonts")
    application.setStyleSheet(build_stylesheet(families))
    services = AppServices.create(connection, clock)
    services.transitions.run_on_startup()
    return MainWindow(identity.name, services)


def _run_gui(identity: ProductIdentity, paths: DataPaths, clock: Clock) -> int:
    from PySide6.QtWidgets import QApplication

    from budget.platform.single_instance import acquire_single_instance
    from budget.platform.windows import set_app_user_model_id

    set_app_user_model_id(identity.app_user_model_id)
    application = QApplication(sys.argv)
    application.setApplicationName(identity.name)
    application.setOrganizationName(identity.publisher)

    lock = acquire_single_instance(paths.lock)
    if lock is None:
        _show_message(identity.name, "Застосунок уже запущено.")
        return EXIT_ALREADY_RUNNING
    try:
        connection = open_application_database(paths, clock)
    except DatabaseCorruptedError as exc:
        log.exception("Database corrupted")
        return _handle_corrupted_database(identity, paths, clock, exc)
    except BudgetError as exc:
        log.exception("Startup failed")
        _show_message(identity.name, exc.user_message)
        return EXIT_STARTUP_FAILED
    window = build_main_window(identity, connection, clock)
    window.show()
    # Спеціальний діалог після тривалої перерви: один, без автоматичного вибору.
    window.open_long_gap_dialog()
    try:
        return application.exec()
    finally:
        connection.close()
        lock.unlock()


def _handle_corrupted_database(
    identity: ProductIdentity, paths: DataPaths, clock: Clock, error: DatabaseCorruptedError
) -> int:
    """Пошкоджена база: нічого не записуємо, зберігаємо файл під новою назвою (DS-6).

    Вибір резервної копії для відновлення в інтерфейсі — наступний етап.
    """
    try:
        quarantined = RecoveryService(paths.database, clock).quarantine_corrupted()
    except BudgetError:
        log.exception("Quarantine failed")
        _show_message(identity.name, error.user_message)
        return EXIT_DATA_CORRUPTED
    _show_message(
        identity.name,
        f"{error.user_message}\nПошкоджений файл збережено як «{quarantined.name}» у теці даних.",
    )
    return EXIT_DATA_CORRUPTED


def _show_fatal(error: BudgetError) -> None:
    log.error("Fatal startup error: %s", error.detail or error.user_message)
    print(error.user_message, file=sys.stderr)
    _show_message(None, error.user_message)


def _show_message(title: str | None, text: str) -> None:
    from PySide6.QtWidgets import QApplication, QMessageBox

    if QApplication.instance() is None:
        QApplication(sys.argv)
    QMessageBox.critical(None, title or "", text)


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--self-check", action="store_true")
    args, _ = parser.parse_known_args(list(argv))  # аргументи Qt проходять далі
    return args
