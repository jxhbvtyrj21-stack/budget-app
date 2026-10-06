"""Діалог «Дані пошкоджено» (DS-6; ui-information-architecture.md, 10.1, 8).

Пояснює, що застосунок нічого не записав і зберіг пошкоджений файл; показує
справні резервні копії від найновішої (дата й час, вид, розмір). «Відновити з
вибраної копії» — лише після підтвердження «Поточні дані буде замінено даними
копії від …». Невдача — повідомлення «Помилка» з причиною; діалог лишається
відкритим, щоб можна було вибрати іншу копію або закрити застосунок.
"""

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QVBoxLayout,
)

from budget.errors import BudgetError
from budget.services.backup import BackupKind, RestoreCandidate
from budget.ui.components.basic import button, text_label
from budget.ui.components.forms import Notice
from budget.ui.formatting import format_moment, format_size
from budget.ui.messages import user_text
from budget.ui.theme.tokens import SPACING

KIND_LABELS = {
    BackupKind.DAILY: "Щоденна",
    BackupKind.WEEKLY: "Щотижнева",
    BackupKind.MONTHLY: "Щомісячна",
    BackupKind.BEFORE_MIGRATION: "Перед міграцією",
    BackupKind.ON_DEMAND: "На вимогу",
    BackupKind.BEFORE_RESTORE: "Перед відновленням",
}


def candidate_text(candidate: RestoreCandidate) -> str:
    backup = candidate.backup
    return (
        f"{format_moment(backup.created)} · {KIND_LABELS[backup.kind]} · "
        f"{format_size(candidate.size)}"
    )


def confirmation_text(candidate: RestoreCandidate) -> str:
    return f"Поточні дані буде замінено даними копії від {format_moment(candidate.backup.created)}."


class RecoveryDialog(QDialog):
    """Вибір копії для відновлення. ``restore`` повертає відкриту відновлену базу."""

    def __init__(
        self,
        quarantined_name: str,
        load: Callable[[], list[RestoreCandidate]],
        restore: Callable[[RestoreCandidate], object],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._load = load
        self._restore = restore
        self.restored: object | None = None
        self.setWindowTitle("Дані пошкоджено")
        # Фіксована ширина: висота тексту з переносами рахується від неї.
        self.setFixedWidth(560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACING[5], SPACING[5], SPACING[5], SPACING[5])
        layout.setSpacing(SPACING[4])
        layout.addWidget(text_label("Дані пошкоджено", "heading"))
        explanation = text_label(
            "Під час запуску виявлено пошкодження даних. Застосунок нічого не записав у "
            f"пошкоджений файл і зберіг його як «{quarantined_name}» у теці даних.",
            "body",
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        layout.addWidget(text_label("Резервні копії", "caption", muted=True))
        self.list = QListWidget()
        self.list.setAccessibleName("Резервні копії")
        self.list.currentItemChanged.connect(lambda *_: self._update_actions())
        self.list.itemDoubleClicked.connect(lambda *_: self.restore_selected())
        self.list.setMinimumHeight(180)
        layout.addWidget(self.list, 1)
        self.empty = text_label("Справних резервних копій немає.", "body", muted=True)
        layout.addWidget(self.empty)
        self.failure = Notice("Помилка", error=True)
        self.failure.hide()
        layout.addWidget(self.failure)
        actions = QHBoxLayout()
        actions.addStretch(1)
        self.close_button = button("Закрити застосунок")
        self.close_button.clicked.connect(self.reject)
        self.restore_button = button("Відновити з вибраної копії", "primary")
        self.restore_button.clicked.connect(self.restore_selected)
        actions.addWidget(self.close_button)
        actions.addWidget(self.restore_button)
        layout.addLayout(actions)
        self.reload()

    def reload(self) -> None:
        self.list.clear()
        for candidate in self._load():
            item = QListWidgetItem(candidate_text(candidate))
            item.setData(Qt.ItemDataRole.UserRole, candidate)
            self.list.addItem(item)
        has_candidates = self.list.count() > 0
        self.list.setVisible(has_candidates)
        self.empty.setVisible(not has_candidates)
        if has_candidates:
            self.list.setCurrentRow(0)  # найновіша справна копія (DS-6)
        self._update_actions()

    def selected(self) -> RestoreCandidate | None:
        item = self.list.currentItem()
        return None if item is None else item.data(Qt.ItemDataRole.UserRole)

    def _update_actions(self) -> None:
        self.restore_button.setEnabled(self.selected() is not None)

    def confirm(self, candidate: RestoreCandidate) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Відновлення з копії")
        box.setText(confirmation_text(candidate))
        cancel = box.addButton("Скасувати", QMessageBox.ButtonRole.RejectRole)
        confirm = box.addButton("Відновити", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is confirm

    def restore_selected(self) -> None:
        candidate = self.selected()
        if candidate is None or not self.confirm(candidate):
            return
        self.failure.hide()
        try:
            self.restored = self._restore(candidate)
        except BudgetError as error:
            self.failure.set_text(user_text(error))
            self.failure.show()
            self.reload()
            return
        self.accept()
