"""Складання застосунку: ідентичність, шляхи, база, тема, головне вікно."""

import argparse
import logging
import sqlite3
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from budget.domain.calendar import Clock, SystemClock
from budget.errors import BudgetError, DatabaseCorruptedError, StartupError
from budget.platform.identity import ProductIdentity, load_product_identity
from budget.platform.paths import DataPaths, data_paths
from budget.platform.resources import assets_dir
from budget.services.backup import (
    BackupService,
    RecoveryService,
    RestoreCandidate,
    RestoreError,
)
from budget.services.startup import prepare_database
from budget.storage.integrity import corruption_code, quick_check

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


class ApplicationSession:
    """Єдиний власник з'єднання з робочою базою на час роботи застосунку.

    Не singleton і не глобальний стан: створюється в ``_run_gui``. Сервіси й
    інтерфейс отримують той самий об'єкт з'єднання через ``AppServices`` і ніколи
    не відкривають і не закривають його самі. SQL і бізнес-логіки тут немає.
    """

    def __init__(self, paths: DataPaths, clock: Clock) -> None:
        self._paths = paths
        self._clock = clock
        self._connection: sqlite3.Connection | None = None

    @property
    def is_open(self) -> bool:
        return self._connection is not None

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("Немає відкритої бази в сесії")
        return self._connection

    def open(self) -> sqlite3.Connection:
        """Відкриває базу звичайним шляхом запуску (``open_application_database``)."""
        self._require_closed()
        self._connection = open_application_database(self._paths, self._clock)
        return self._connection

    def adopt(self, connection: sqlite3.Connection) -> None:
        """Бере у власність уже відкриту базу (після відновлення під час запуску)."""
        self._require_closed()
        self._connection = connection

    def close(self) -> None:
        """Закриває поточне з'єднання; повторний виклик нічого не робить."""
        connection, self._connection = self._connection, None
        if connection is not None:
            connection.close()

    def _require_closed(self) -> None:
        if self._connection is not None:
            raise RuntimeError("Сесія вже має відкриту базу; спершу закрийте її")


class RuntimeCorruptionGuard:
    """Розпізнає пошкодження бази під час роботи застосунку (Block C3).

    Спільна точка входу ``handle_runtime_corruption`` викликається з ``sys.excepthook``
    (неперехоплений виняток зі слоту Qt) і з ``_run_gui`` до запуску циклу подій.
    Пошкодження визначає ``storage.integrity.corruption_code`` (лише код SQLite).
    Усе інше обробник не чіпає: передає попередньому ``sys.excepthook``. Перше
    пошкодження записується в журнал один раз і передається ``on_corruption`` через
    ``QTimer.singleShot(0)`` — поза слотом, де воно виникло. Наступні не запускають
    другого відновлення. Стан належить екземпляру, створеному в ``_run_gui``.
    """

    def __init__(self, on_corruption: Callable[[BaseException], None]) -> None:
        self._on_corruption = on_corruption
        self._previous_hook = None
        self._detected = False

    @property
    def detected(self) -> bool:
        """RUNTIME_CORRUPTION_DETECTED: відновлення вже очікується."""
        return self._detected

    def handle_runtime_corruption(self, error: BaseException, *, schedule: bool) -> bool:
        """``True``, якщо ``error`` — пошкодження бази; ``schedule`` — цикл подій працює."""
        code = corruption_code(error)
        if code is None:
            return False
        if self._detected:
            log.info("Repeated database corruption error while recovery is pending")
            return True
        self._detected = True
        log.error(
            "Database corruption detected at runtime: sqlite code %s, %s",
            code,
            type(error).__name__,
        )
        if schedule:
            from PySide6.QtCore import QTimer

            QTimer.singleShot(0, lambda: self._on_corruption(error))
        return True

    def __enter__(self) -> "RuntimeCorruptionGuard":
        self._previous_hook = sys.excepthook
        sys.excepthook = self._excepthook
        return self

    def __exit__(self, *exc_info) -> None:
        if sys.excepthook == self._excepthook:
            sys.excepthook = self._previous_hook
        self._previous_hook = None

    def _excepthook(self, exc_type, error, traceback) -> None:
        if not self.handle_runtime_corruption(error, schedule=True):
            self._previous_hook(exc_type, error, traceback)


def _suspend_ui_for_recovery(error: BaseException) -> None:
    """Поки відновлення не виконано (C5–C6), звичайна робота з пошкодженою базою зупинена:
    усі вікна застосунку стають неактивними."""
    from PySide6.QtWidgets import QApplication

    for widget in QApplication.topLevelWidgets():
        widget.setEnabled(False)


@dataclass(frozen=True, slots=True)
class RuntimeRecoveryChoice:
    """Результат діалогу відновлення під час роботи: вибрана справна копія або скасування."""

    candidate: RestoreCandidate | None

    @property
    def cancelled(self) -> bool:
        return self.candidate is None


class RuntimeRecoveryFlow:
    """Точка входу відновлення під час роботи (``on_corruption`` для C3, Block C4).

    Зупиняє звичайну роботу (вікна неактивні), показує наявний діалог «Дані
    пошкоджено» з перевіреними копіями й передає ``on_choice`` вибрану копію або
    скасування. Нічого не відновлює, не закриває й не відкриває базу: це C5.
    Скасування не повертає до звичайної роботи — вікна лишаються неактивними.
    """

    def __init__(
        self,
        load_candidates: Callable[[], list[RestoreCandidate]],
        on_choice: Callable[[RuntimeRecoveryChoice], None],
    ) -> None:
        self._load_candidates = load_candidates
        self._on_choice = on_choice

    def __call__(self, error: BaseException) -> None:
        from budget.ui.dialogs.recovery_dialog import RecoveryDialog

        _suspend_ui_for_recovery(error)
        # Без батька: вимкнені вікна застосунку не вимикають сам діалог.
        dialog = RecoveryDialog(None, load=self._load_candidates, restore=lambda c: c)
        accepted = bool(dialog.exec())
        choice = RuntimeRecoveryChoice(dialog.restored if accepted else None)
        dialog.deleteLater()
        if choice.cancelled:
            log.info("Runtime recovery cancelled")
        else:
            log.info("Runtime recovery backup selected: %s", choice.candidate.backup.path.name)
        self._on_choice(choice)


def _await_runtime_restore(choice: RuntimeRecoveryChoice) -> None:
    """Відновлення під час роботи ще не реалізоване (C5): вікна лишаються неактивними."""


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
        _roll_back(recovery, outcome, exc)
        raise RestoreError(detail=f"Відновлена база не відкрилася: {exc.detail}") from exc
    try:
        recovery.check_restored(connection)
    except BudgetError as exc:
        log.exception("Restored database failed the integrity check")
        connection.close()
        _roll_back(recovery, outcome, exc)
        raise
    recovery.finish(outcome)
    return connection


def _roll_back(recovery: RecoveryService, outcome, cause: BudgetError) -> None:
    """Відкат невдалого відновлення. Якщо й він не вдався, жодна з двох помилок не
    приховується: обидві в журналі й у подробицях, а користувач бачить, що попередній
    стан не повернуто."""
    try:
        recovery.rollback(outcome)
    except RestoreError as failure:
        log.exception("Rollback after failed restore failed")
        raise RestoreError(
            failure.user_message, detail=f"{cause.detail}; відкат: {failure.detail}"
        ) from cause


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


def build_main_window(
    identity: ProductIdentity,
    connection: sqlite3.Connection,
    clock: Clock,
    backups_dir: Path | None = None,
    *,
    month_transition: bool = True,
):
    """Створює головне вікно з темою; фінансові екрани — лише після налаштування.

    Під час звичайного запуску перед побудовою виконується перехід між місяцями
    для доходів (ADR 0009). Після відновлення з копії (``month_transition=False``)
    переходу немає: відкривається саме стан, що міститься в копії.
    """

    from budget.services.facade import AppServices
    from budget.ui.main_window import MainWindow

    apply_theme()
    services = AppServices.create(connection, clock, backups_dir)
    if month_transition:
        services.transitions.run_on_startup()
    return MainWindow(identity.name, services)


def show_main_window(
    identity: ProductIdentity,
    connection: sqlite3.Connection,
    clock: Clock,
    backups_dir: Path,
    *,
    restored: bool,
):
    """Показує головне вікно. Після відновлення — без переходу між місяцями й без
    автоматичного діалогу тривалої перерви: жодної фінансової зміни лише тому, що
    з часу копії настав інший календарний момент."""
    window = build_main_window(
        identity, connection, clock, backups_dir, month_transition=not restored
    )
    window.show()
    if not restored:
        # Спеціальний діалог після тривалої перерви: один, без автоматичного вибору.
        window.open_long_gap_dialog()
    return window


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
    session = ApplicationSession(paths, clock)
    exit_code, restored = start_session(identity, session, paths, clock)
    if exit_code is not None:
        return exit_code
    recovery = RecoveryService(paths.database, paths.backups, clock)
    guard = RuntimeCorruptionGuard(RuntimeRecoveryFlow(recovery.candidates, _await_runtime_restore))
    try:
        # Посилання тримає вікно живим до кінця циклу подій.
        window = show_window_or_report(identity, session, clock, paths, restored, guard)
        if window is None:
            return EXIT_DATA_CORRUPTED
        with guard:
            return application.exec()
    finally:
        session.close()
        lock.unlock()


def show_window_or_report(
    identity: ProductIdentity,
    session: ApplicationSession,
    clock: Clock,
    paths: DataPaths,
    restored: bool,
    guard: RuntimeCorruptionGuard,
):
    """Показує головне вікно. Пошкодження бази до запуску циклу подій — ``None``:
    ``guard`` фіксує його (контрольований вихід; відновлення — C5). Інші помилки
    SQLite не перехоплюються."""
    try:
        return show_main_window(
            identity, session.connection, clock, paths.backups, restored=restored
        )
    except sqlite3.Error as exc:
        if not guard.handle_runtime_corruption(exc, schedule=False):
            raise
        return None


def start_session(
    identity: ProductIdentity, session: ApplicationSession, paths: DataPaths, clock: Clock
) -> tuple[int | None, bool]:
    """Відкриває базу сесії під час запуску.

    Повертає ``(код виходу, відновлено)``: код виходу — якщо продовжити неможливо,
    інакше ``None``; ``відновлено`` — базу відновлено з копії (DS-6). Відкритою
    лишається лише база, якою володіє ``session``.
    """
    try:
        session.open()
    except DatabaseCorruptedError as exc:
        log.exception("Database corrupted")
        connection = _recover_corrupted_database(identity, paths, clock, exc)
        if connection is None:
            return EXIT_DATA_CORRUPTED, False
        session.adopt(connection)
        return None, True
    except BudgetError as exc:
        log.exception("Startup failed")
        _show_message(identity.name, exc.user_message)
        return EXIT_STARTUP_FAILED, False
    return None, False


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
