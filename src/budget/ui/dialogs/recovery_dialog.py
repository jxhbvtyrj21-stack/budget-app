"""Діалог «Дані пошкоджено» (DS-6; ui-information-architecture.md, 10.1, 8).

Пояснює, що застосунок нічого не записав і зберіг пошкоджений файл (під час запуску
чи під час роботи — тоді ще й що подальші зміни зупинено). Показує
справні резервні копії від найновішої (дата й час, вид, розмір). «Відновити з
вибраної копії» — лише після підтвердження «Поточні дані буде замінено даними
копії від …». Невдача — повідомлення «Помилка» з причиною; діалог лишається
відкритим, щоб можна було вибрати іншу копію або закрити застосунок.

Запуск без робочої бази, але з ознаками попередньої (R1): власне пояснення й окрема
явна дія «Почати з порожніми даними» — лише після підтвердження, ніколи автоматично.
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
from budget.services.backup import RestoreCandidate
from budget.ui.components.basic import button, text_label
from budget.ui.components.forms import Notice
from budget.ui.formatting import format_backup_kind, format_moment, format_size
from budget.ui.messages import user_text
from budget.ui.theme.tokens import SPACING


def candidate_text(candidate: RestoreCandidate) -> str:
    backup = candidate.backup
    return (
        f"{format_moment(backup.created)} · {format_backup_kind(backup.kind)} · "
        f"{format_size(candidate.size)}"
    )


def confirmation_text(candidate: RestoreCandidate) -> str:
    return f"Поточні дані буде замінено даними копії від {format_moment(candidate.backup.created)}."


STARTUP_EXPLANATION = (
    "Під час запуску виявлено пошкодження даних. Застосунок нічого не записав у "
    "пошкоджений файл і зберіг його як «{name}» у теці даних."
)
MISSING_DATABASE_EXPLANATION = (
    "Основну базу даних не знайдено.\n\n"
    "Застосунок виявив резервні копії або файли, що свідчать про попередній стан бази. "
    "Щоб не втратити дані через автоматичне створення нової бази, запуск призупинено.\n\n"
    "Виберіть резервну копію для відновлення або, якщо ви свідомо хочете почати без "
    "попередніх даних, оберіть відповідну дію."
)
START_EMPTY = "Почати з порожніми даними"
START_EMPTY_CONFIRMATION = (
    "Основну базу даних не знайдено, але застосунок виявив ознаки попередньої бази або "
    "резервні копії.\n\n"
    "Якщо почати з порожніми даними, нова база буде створена без відновлення попередніх "
    "даних.\n\n"
    "Резервні копії та файли карантину не будуть видалені.\n\n"
    "Продовжити?"
)
RUNTIME_EXPLANATION = (
    "Під час роботи виявлено пошкодження даних. Подальші зміни зупинено, щоб нічого "
    "не записати в пошкоджені дані. Пошкоджений файл збережено як «{name}» у теці даних. "
    "Оберіть резервну копію для відновлення."
)


class RecoveryDialog(QDialog):
    """Вибір копії для відновлення.

    ``restore`` викликається з кандидатом із показаного переліку після підтвердження;
    його результат зберігається в ``restored``. ``quarantined_name`` — назва збереженого
    пошкодженого файлу; ``runtime`` — пошкодження виявлено під час роботи, а не запуску.
    ``quarantined_name=None`` — робочої бази немає взагалі (R1). ``start_empty`` — дія
    «Почати з порожніми даними» (лише тоді є кнопка): після підтвердження й успіху
    ``started_empty`` істинне; невдача — «Помилка», діалог лишається відкритим.
    """

    def __init__(
        self,
        quarantined_name: str | None,
        load: Callable[[], list[RestoreCandidate]],
        restore: Callable[[RestoreCandidate], object],
        parent=None,
        *,
        runtime: bool = False,
        start_empty: Callable[[], object] | None = None,
    ) -> None:
        super().__init__(parent)
        self._load = load
        self._restore = restore
        self._start_empty = start_empty
        self.restored: object | None = None
        self.started_empty = False
        self.setWindowTitle("Дані пошкоджено")
        # Фіксована ширина: висота тексту з переносами рахується від неї.
        self.setFixedWidth(560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACING[5], SPACING[5], SPACING[5], SPACING[5])
        layout.setSpacing(SPACING[4])
        layout.addWidget(text_label("Дані пошкоджено", "heading"))
        if quarantined_name is None:
            text = MISSING_DATABASE_EXPLANATION
        else:
            text = (RUNTIME_EXPLANATION if runtime else STARTUP_EXPLANATION).format(
                name=quarantined_name
            )
        explanation = text_label(text, "body")
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
        self.start_empty_button = None
        if start_empty is not None:
            self.start_empty_button = button(START_EMPTY)
            self.start_empty_button.clicked.connect(self.start_empty_selected)
            actions.addWidget(self.start_empty_button)
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

    def confirm_start_empty(self) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(START_EMPTY)
        box.setText(START_EMPTY_CONFIRMATION)
        cancel = box.addButton("Скасувати", QMessageBox.ButtonRole.RejectRole)
        confirm = box.addButton(START_EMPTY, QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is confirm

    def start_empty_selected(self) -> None:
        """Лише явна дія з підтвердженням; невдача — «Помилка», нової бази немає."""
        if self._start_empty is None or not self.confirm_start_empty():
            return
        self.failure.hide()
        try:
            self._start_empty()
        except BudgetError as error:
            self.failure.set_text(user_text(error))
            self.failure.show()
            return
        self.started_empty = True
        self.accept()

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
