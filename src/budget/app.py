"""Складання застосунку: ідентичність, шляхи, база, тема, головне вікно."""

import argparse
import logging
import sqlite3
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from budget.domain.calendar import Clock, SystemClock
from budget.errors import BudgetError, DatabaseCorruptedError, StartupError, StorageError
from budget.platform.identity import ProductIdentity, load_product_identity
from budget.platform.paths import DataPaths, data_paths
from budget.platform.resources import assets_dir
from budget.services.backup import (
    ROLLBACK_FAILED_MESSAGE,
    BackupService,
    RecoveryService,
    RestoreCandidate,
    RestoreError,
)
from budget.services.startup import prepare_database
from budget.storage.database import close_without_checkpoint
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
    except DatabaseCorruptedError:
        close_without_checkpoint(connection)
        raise
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

    def close_after_corruption(self) -> None:
        """Закриває з'єднання з пошкодженою базою без checkpoint WAL
        (``close_without_checkpoint``); повторний виклик нічого не робить."""
        connection, self._connection = self._connection, None
        if connection is not None:
            close_without_checkpoint(connection)

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

    def rearm(self) -> None:
        """Лише після повністю успішного відновлення: наступне пошкодження знову
        запускає одне відновлення. Той самий екземпляр і той самий ``sys.excepthook``."""
        self._detected = False

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


def _resume_ui_after_recovery() -> None:
    """Відновлення завершено: вікна застосунку знову активні."""
    from PySide6.QtWidgets import QApplication

    for widget in QApplication.topLevelWidgets():
        widget.setEnabled(True)


QUARANTINE_FAILED_MESSAGE = (
    "Під час роботи виявлено пошкодження даних, але пошкоджений файл не вдалося безпечно "
    "зберегти. Застосунок нічого не записав у пошкоджені дані й буде закритий."
)
APPLICATION_WILL_CLOSE = "Застосунок буде закритий."
RESTORE_ABORTED_MESSAGE = (
    "Під час відновлення з копії сталася непередбачена помилка. Стан файлів даних не "
    "перевірено, тому застосунок буде закритий. Пошкоджений файл лишається збереженим "
    "у теці даних."
)
RESTORE_INCOMPLETE_MESSAGE = (
    "Дані відновлено з вибраної копії, але продовжити з ними роботу не вдалося. "
    "Застосунок буде закритий; відновлені дані лишаються в теці даних."
)


class RuntimeRecoveryFlow:
    """Точка входу відновлення під час роботи (``on_corruption`` для C3; Blocks C5.1–C5.2).

    Порядок: вікна неактивні → ``session.close_after_corruption()`` (жодного з'єднання
    з пошкодженою базою; без checkpoint WAL у пошкоджений файл) → карантин
    ``.db``/``-wal``/``-shm`` → діалог «Дані пошкоджено» з назвою збереженого файлу
    й перевіреними копіями. Скасування — ``exit_application``
    з ``EXIT_DATA_CORRUPTED``: бази вже немає, працювати далі нема з чим. Невдалий
    карантин — повідомлення й той самий вихід; діалог не відкривається.

    Вибрана й підтверджена копія відновлюється всередині діалогу через ``restore``
    (``restore_and_open``: повністю відкрита й перевірена база або ``RestoreError``,
    яку діалог показує). Лише після успіху сесія бере у власність саме повернуте
    з'єднання (``adopt``), і воно передається ``on_restored``. Інших з'єднань тут немає.

    Невдачі (Block C5.3/C6):

    * ``RestoreError`` з успішним відкатом — діалог лишається відкритим із причиною,
      можна вибрати іншу копію; вікна неактивні, ``guard`` не готовий, сесія закрита.
    * Невдалий відкат (``ROLLBACK_FAILED_MESSAGE``) або непередбачений виняток під час
      відновлення — стан файлів не гарантований: діалог закривається, повідомлення,
      вихід з ``EXIT_DATA_CORRUPTED``.
    * Невдача після успішного ``restore_and_open`` (``adopt``, ``on_restored``) —
      повернуте з'єднання закривається, вікна лишаються неактивними, ``guard`` не
      готовий, повідомлення й той самий вихід.
    """

    def __init__(
        self,
        session: ApplicationSession,
        recovery: RecoveryService,
        restore: Callable[[Path], sqlite3.Connection],
        on_restored: Callable[[sqlite3.Connection], None],
        exit_application: Callable[[int], None],
        show_error: Callable[[str], None],
    ) -> None:
        self._session = session
        self._recovery = recovery
        self._restore = restore
        self._on_restored = on_restored
        self._exit_application = exit_application
        self._show_error = show_error
        self.quarantine_error: StorageError | None = None
        self.failure: BaseException | None = None  # невдача, після якої лише вихід
        self._failure_message = ""

    def __call__(self, error: BaseException) -> None:
        from budget.ui.dialogs.recovery_dialog import RecoveryDialog

        _suspend_ui_for_recovery(error)
        self._session.close_after_corruption()
        try:
            quarantined = self._recovery.quarantine_corrupted()
        except StorageError as failure:
            self.quarantine_error = failure
            log.exception("Runtime quarantine of the corrupted database failed")
            self._show_error(QUARANTINE_FAILED_MESSAGE)
            self._exit_application(EXIT_DATA_CORRUPTED)
            return
        log.info("Corrupted database kept as %s", quarantined.name)
        # Без батька: вимкнені вікна застосунку не вимикають сам діалог.
        dialog = RecoveryDialog(
            quarantined.name,
            load=self._recovery.candidates,
            restore=self._attempt_restore,
            runtime=True,
        )
        accepted = bool(dialog.exec())
        connection = dialog.restored if accepted else None
        candidate = dialog.selected()
        dialog.deleteLater()
        if self.failure is not None:
            self._terminate(self._failure_message)
            return
        if connection is None:
            log.info("Runtime recovery cancelled")
            self._exit_application(EXIT_DATA_CORRUPTED)
            return
        log.info("Database restored at runtime from %s", candidate.backup.path.name)
        stage = "session.adopt"
        try:
            self._session.adopt(connection)
            stage = "resume on the restored database"
            self._on_restored(connection)
        except Exception as failure:
            self.failure = failure
            log.exception("Runtime recovery failed after the restore at stage: %s", stage)
            self._close_restored(connection)
            self._terminate(RESTORE_INCOMPLETE_MESSAGE)

    def _attempt_restore(self, candidate: RestoreCandidate) -> sqlite3.Connection | None:
        """Одна спроба з кандидатом, вибраним у діалозі. ``RestoreError`` з успішним
        відкатом — до діалогу (повторна спроба); інакше ``None`` і ``failure``."""
        try:
            return self._restore(candidate.backup.path)
        except RestoreError as failure:
            if failure.user_message != ROLLBACK_FAILED_MESSAGE:
                log.warning(
                    "Runtime restore from %s failed; another backup can be chosen",
                    candidate.backup.path.name,
                    exc_info=True,
                )
                raise
            self.failure = failure
            self._failure_message = f"{failure.user_message} {APPLICATION_WILL_CLOSE}"
            log.exception("Runtime restore failed and its rollback failed too")
        except Exception as failure:
            self.failure = failure
            self._failure_message = RESTORE_ABORTED_MESSAGE
            log.exception("Runtime restore failed unexpectedly")
        return None

    def _close_restored(self, connection: sqlite3.Connection) -> None:
        if self._session.is_open and self._session.connection is connection:
            self._session.close()
        else:
            connection.close()

    def _terminate(self, message: str) -> None:
        self._show_error(message)
        self._exit_application(EXIT_DATA_CORRUPTED)


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


def resume_after_runtime_restore(
    window,
    connection: sqlite3.Connection,
    clock: Clock,
    backups_dir: Path,
    guard: RuntimeCorruptionGuard,
) -> None:
    """Продовження роботи на відновленій базі (Block C5.2).

    Новий граф сервісів — на з'єднанні, яким уже володіє сесія; вікно замінює граф
    (C2). Переходу між місяцями й діалогу тривалої перерви немає: відкривається саме
    стан копії. Потім вікна знову активні, і лише наприкінці ``guard`` знову готовий
    до наступного пошкодження.
    """
    from budget.services.facade import AppServices

    stage = "AppServices.create"
    try:
        services = AppServices.create(connection, clock, backups_dir)
        stage = "MainWindow.replace_services"
        window.replace_services(services)
    except Exception as failure:
        failure.add_note(f"Runtime recovery stage: {stage}")
        raise
    _resume_ui_after_recovery()
    guard.rearm()
    log.info("Runtime recovery finished; work continues on the restored database")


def runtime_recovery_guard(
    session: ApplicationSession,
    paths: DataPaths,
    clock: Clock,
    current_window: Callable[[], object],
    *,
    exit_application: Callable[[int], None],
    show_error: Callable[[str], None],
) -> RuntimeCorruptionGuard:
    """Складає відновлення під час роботи: ``guard`` → ``RuntimeRecoveryFlow`` →
    ``restore_and_open`` → ``resume_after_runtime_restore``. ``current_window`` дає
    головне вікно в момент відновлення (воно створюється після ``guard``)."""
    guard: RuntimeCorruptionGuard

    def resume(connection: sqlite3.Connection) -> None:
        resume_after_runtime_restore(current_window(), connection, clock, paths.backups, guard)

    flow = RuntimeRecoveryFlow(
        session,
        RecoveryService(paths.database, paths.backups, clock),
        restore=lambda backup_path: restore_and_open(paths, clock, backup_path),
        on_restored=resume,
        exit_application=exit_application,
        show_error=show_error,
    )
    guard = RuntimeCorruptionGuard(flow)
    return guard


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

    from budget.platform.windows import set_app_user_model_id

    set_app_user_model_id(identity.app_user_model_id)
    application = QApplication(sys.argv)
    application.setApplicationName(identity.name)
    application.setOrganizationName(identity.publisher)
    return run_application(identity, paths, clock, application)


def run_application(identity: ProductIdentity, paths: DataPaths, clock: Clock, application) -> int:
    """Життєвий цикл застосунку в уже створеному ``QApplication``: блокування одного
    екземпляра, сесія бази, головне вікно, цикл подій і завершення."""
    from budget.platform.single_instance import acquire_single_instance

    lock = acquire_single_instance(paths.lock)
    if lock is None:
        _show_message(identity.name, "Застосунок уже запущено.")
        return EXIT_ALREADY_RUNNING
    session = ApplicationSession(paths, clock)
    exit_code, restored = start_session(identity, session, paths, clock)
    if exit_code is not None:
        return exit_code
    window = None
    guard = runtime_recovery_guard(
        session,
        paths,
        clock,
        lambda: window,
        exit_application=application.exit,
        show_error=lambda text: _show_message(identity.name, text),
    )
    try:
        # Посилання тримає вікно живим до кінця циклу подій.
        window = show_window_with_recovery(identity, session, clock, paths, restored, guard)
        if window is None:
            return EXIT_DATA_CORRUPTED
        with guard:
            return application.exec()
    finally:
        # Пошкодження, по якому відновлення не відбулося (напр., до циклу подій), —
        # закриття без checkpoint; після успішного відновлення guard знову не «detected».
        if guard.detected:
            session.close_after_corruption()
        else:
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
    ``guard`` фіксує його (відновлення — ``show_window_with_recovery``). Інші помилки
    SQLite не перехоплюються."""
    try:
        return show_main_window(
            identity, session.connection, clock, paths.backups, restored=restored
        )
    except sqlite3.Error as exc:
        if not guard.handle_runtime_corruption(exc, schedule=False):
            raise
        return None


def show_window_with_recovery(
    identity: ProductIdentity,
    session: ApplicationSession,
    clock: Clock,
    paths: DataPaths,
    restored: bool,
    guard: RuntimeCorruptionGuard,
):
    """Головне вікно до запуску циклу подій.

    Пошкодження, виявлене тут, іде тим самим шляхом, що й пошкодження під час запуску
    (``_recover_corrupted_database``, Block B): закриття без checkpoint, карантин,
    діалог «Дані пошкоджено», відновлення вибраної копії. Сесія бере саме повернуте
    з'єднання, ``guard`` знову готовий, і вікно будується зі стану копії — без
    переходу між місяцями й діалогу тривалої перерви. Цикл подій ще не працює, тож
    жодного ``QTimer`` і відновлення під час роботи тут немає. ``None`` — користувач
    закрив застосунок або карантин не вдався.
    """
    while True:
        window = show_window_or_report(identity, session, clock, paths, restored, guard)
        if window is not None:
            return window
        session.close_after_corruption()
        connection = _recover_corrupted_database(
            identity,
            paths,
            clock,
            DatabaseCorruptedError(detail="Пошкодження виявлено до запуску циклу подій"),
        )
        if connection is None:
            return None
        session.adopt(connection)
        guard.rearm()
        restored = True


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
