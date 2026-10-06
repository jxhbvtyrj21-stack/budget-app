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
from budget.services.backup import BackupService, RecoveryService, RestoreError
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


def restore_and_open(paths: DataPaths, clock: Clock, backup_path: Path) -> sqlite3.Connection:
    """Відновлює базу з копії й відкриває її звичайним шляхом запуску (DS-6).

    Після заміни файлів база проходить ``quick_check``, обов'язкову копію перед
    міграцією й міграції, автоматичні копії та повну перевірку цілісності. Лише
    тоді з'єднання повертається застосунку. Будь-яка невдача — ``RestoreError``,
    а файли бази повертаються до стану перед спробою.
    """
    recovery = RecoveryService(paths.database, paths.backups, clock)
    outcome = recovery.restore(backup_path)
    try:
        connection = open_application_database(paths, clock)
    except BudgetError as exc:
        log.exception("Restored database failed to open")
        recovery.rollback(outcome)
        raise RestoreError(detail=f"Відновлена база не відкрилася: {exc.detail}") from exc
    try:
        recovery.check_restored(connection)
    except BudgetError:
        connection.close()
        recovery.rollback(outcome)
        raise
    recovery.finish(outcome)
    return connection


def apply_theme() -> None:
    """Шрифти й таблиця стилів застосунку — один раз на процес."""
    from PySide6.QtWidgets import QApplication

    from budget.ui.theme.stylesheet import build_stylesheet
    from budget.ui.theme.typography import register_fonts

    application = QApplication.instance()
    if application.property("budgetThemeApplied"):
        return
    families = register_fonts(assets_dir() / "fonts")
    application.setStyleSheet(build_stylesheet(families))
    application.setProperty("budgetThemeApplied", True)


def build_main_window(identity: ProductIdentity, connection: sqlite3.Connection, clock: Clock):
    """Створює головне вікно з темою; фінансові екрани — лише після налаштування.

    Перед побудовою виконується перехід між місяцями для доходів (ADR 0009).
    """

    from budget.services.facade import AppServices
    from budget.ui.main_window import MainWindow

    apply_theme()
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
        connection = _recover_corrupted_database(identity, paths, clock, exc)
        if connection is None:
            return EXIT_DATA_CORRUPTED
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


def _recover_corrupted_database(
    identity: ProductIdentity, paths: DataPaths, clock: Clock, error: DatabaseCorruptedError
) -> sqlite3.Connection | None:
    """Пошкоджена база: нічого не записуємо, зберігаємо файли під новою назвою (DS-6),
    потім діалог «Дані пошкоджено» з відновленням із вибраної справної копії (IA 10.1).

    Повертає відкриту відновлену базу або ``None``, якщо користувач закрив застосунок.
    """
    from budget.ui.dialogs.recovery_dialog import RecoveryDialog

    recovery = RecoveryService(paths.database, paths.backups, clock)
    try:
        quarantined = recovery.quarantine_corrupted()
    except BudgetError:
        log.exception("Quarantine failed")
        _show_message(identity.name, error.user_message)
        return None
    apply_theme()
    dialog = RecoveryDialog(
        quarantined.name,
        load=recovery.candidates,
        restore=lambda candidate: restore_and_open(paths, clock, candidate.backup.path),
    )
    if not dialog.exec():
        return None
    log.info("Database restored from %s", dialog.selected().backup.path.name)
    return dialog.restored


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
