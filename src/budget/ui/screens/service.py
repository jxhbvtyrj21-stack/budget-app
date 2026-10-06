"""Сервіс (ui-information-architecture.md, розділ 8; design-system.md, 6.5).

«Резервні копії»: таблиця справних копій від найновішої (дата й час створення,
вид, розмір). Перелік і перевірку копій виконує ``BackupService``; екран лише
показує результат.
"""

from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableWidget, QTableWidgetItem

from budget.services.backup import BackupService, RestoreCandidate
from budget.ui.components.basic import Panel, text_label
from budget.ui.formatting import format_backup_kind, format_moment, format_size
from budget.ui.screens.page import Page

BACKUP_COLUMNS = ("Дата й час створення", "Вид", "Розмір")
EMPTY_BACKUPS = "Резервних копій ще немає"


class ServicePage(Page):
    def __init__(self, backups: BackupService) -> None:
        super().__init__("Сервіс")
        self._backups = backups
        self.candidates: list[RestoreCandidate] = []

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
        self.body.addWidget(panel)
        self.body.addStretch(1)
        self.refresh()

    def refresh(self) -> None:
        self.candidates = self._backups.candidates()
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
        self.empty.setVisible(not has_rows)
