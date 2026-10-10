"""Сервіс (ui-information-architecture.md, розділ 8; design-system.md, 6.5).

«Резервні копії»: таблиця справних копій від найновішої (дата й час створення,
вид, розмір) і «Створити резервну копію» — копія на вимогу (DS-5). Перелік, перевірку
й створення копій виконує ``BackupService``; екран лише показує результат. Невдача —
повідомлення «Помилка» з причиною (IA 12); пошкодження бази, знайдене перевіркою
перед копією, показується так само, без запуску відновлення.

«Відновити з копії» (S3): вибраний у таблиці кандидат із ``BackupService.candidates()``
після підтвердження «Поточні дані буде замінено даними копії від …» передається
сигналом ``restore_requested``. Саме відновлення (сесія, файли, новий граф сервісів)
виконує шар застосунку; екран не отримує ні з'єднання, ні сесії, ні шляхів.

«Про програму»: назва продукту (з ``product.toml`` через головне вікно) і версія з
метаданих встановленого пакета.
"""

import logging
from importlib.metadata import version

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
)

from budget.errors import BudgetError
from budget.services.backup import BackupKind, BackupService, RestoreCandidate
from budget.ui.components.basic import Panel, button, text_label
from budget.ui.components.forms import Notice
from budget.ui.dialogs.recovery_dialog import confirmation_text
from budget.ui.formatting import format_backup_kind, format_moment, format_size
from budget.ui.messages import user_text
from budget.ui.screens.page import Page

log = logging.getLogger(__name__)

BACKUP_COLUMNS = ("Дата й час створення", "Вид", "Розмір")
EMPTY_BACKUPS = "Резервних копій ще немає"
CREATE_BACKUP = "Створити резервну копію"
RESTORE_BACKUP = "Відновити з копії"
BACKUP_FAILED = "Резервну копію не створено."
PACKAGE = "budget"  # назва дистрибутива в pyproject.toml


def application_version() -> str:
    """Версія застосунку з метаданих пакета (``pyproject.toml``), без копії в коді."""
    return version(PACKAGE)


class ServicePage(Page):
    restore_requested = Signal(object)  # RestoreCandidate, підтверджений користувачем

    def __init__(self, backups: BackupService, product_name: str) -> None:
        super().__init__("Сервіс")
        self._backups = backups
        self.candidates: list[RestoreCandidate] = []
        self._list_unreadable = False  # «Помилка» зараз — про перелік копій

        panel = Panel()
        panel.body.addWidget(text_label("Резервні копії", "heading"))
        self.table = QTableWidget(0, len(BACKUP_COLUMNS))
        self.table.setHorizontalHeaderLabels(BACKUP_COLUMNS)
        self.table.setAccessibleName("Резервні копії")
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setMinimumHeight(240)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, len(BACKUP_COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        panel.body.addWidget(self.table)
        self.empty = text_label(EMPTY_BACKUPS, "body", muted=True)
        panel.body.addWidget(self.empty)
        self.failure = Notice("Помилка", error=True)
        self.failure.hide()
        panel.body.addWidget(self.failure)
        actions = QHBoxLayout()
        actions.addStretch(1)
        self.restore_button = button(RESTORE_BACKUP)
        self.restore_button.clicked.connect(self.request_restore)
        self.create_button = button(CREATE_BACKUP, "primary")
        self.create_button.clicked.connect(self.create_backup)
        actions.addWidget(self.restore_button)
        actions.addWidget(self.create_button)
        panel.body.addLayout(actions)
        self.body.addWidget(panel)

        about = Panel()
        about.body.addWidget(text_label("Про програму", "heading"))
        self.product_label = text_label(product_name, "body-strong")
        self.version_label = text_label(f"Версія {application_version()}", "body", muted=True)
        about.body.addWidget(self.product_label)
        about.body.addWidget(self.version_label)
        self.body.addWidget(about)
        self.body.addStretch(1)
        self.refresh()

    def refresh(self) -> None:
        """Перелік справних копій. Перелік не прочитано (``BudgetError``) — «Помилка» з
        причиною замість «Резервних копій ще немає»; відновлювати нічого. Коли перелік
        знову прочитано, ця «Помилка» зникає; інші повідомлення не зачіпаються."""
        try:
            self.candidates = self._backups.candidates()
        except BudgetError as error:
            log.exception("Backup list could not be read")
            self.failure.set_text(user_text(error))
            self.failure.show()
            self.candidates = []
            unavailable = True
        else:
            unavailable = False
            if self._list_unreadable:
                self.failure.hide()
        self._list_unreadable = unavailable
        self.table.setRowCount(len(self.candidates))
        for row, candidate in enumerate(self.candidates):
            backup = candidate.backup
            cells = (
                format_moment(backup.created),
                format_backup_kind(backup.kind),
                format_size(candidate.size),
            )
            for column, text in enumerate(cells):
                self.table.setItem(row, column, QTableWidgetItem(text))
        has_rows = bool(self.candidates)
        self.table.setVisible(has_rows)
        self.empty.setVisible(not unavailable and not has_rows)
        if has_rows:
            self.table.selectRow(0)  # найновіша справна копія
        self.restore_button.setEnabled(has_rows)

    def selected(self) -> RestoreCandidate | None:
        rows = self.table.selectionModel().selectedRows()
        return self.candidates[rows[0].row()] if rows else None

    def request_restore(self) -> None:
        """Підтвердження й запит на відновлення вибраної копії; без вибору — нічого."""
        candidate = self.selected()
        if candidate is None or not self.confirm(candidate):
            return
        self.restore_requested.emit(candidate)

    def confirm(self, candidate: RestoreCandidate) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(RESTORE_BACKUP)
        box.setText(confirmation_text(candidate))
        cancel = box.addButton("Скасувати", QMessageBox.ButtonRole.RejectRole)
        confirm = box.addButton("Відновити", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is confirm

    def show_failure(self, text: str) -> None:
        """«Помилка» з причиною (IA 12) — для невдалого відновлення з копії."""
        self._list_unreadable = False
        self.failure.set_text(text)
        self.failure.show()

    def create_backup(self) -> None:
        """Копія на вимогу (DS-5) на з'єднанні сесії; потім оновлений перелік."""
        try:
            self._backups.create_backup(BackupKind.ON_DEMAND)
        except BudgetError as error:
            log.exception("On-demand backup failed")
            self._list_unreadable = False
            self.failure.set_text(f"{BACKUP_FAILED} {user_text(error)}")
            self.failure.show()
            return
        self.failure.hide()
        self.refresh()
