"""Поля форм, повідомлення й рядки списків (design-system.md, 6.2, 6.4, 6.8)."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout, QWidget

from budget.domain.money import Money
from budget.ui.components.basic import amount_label, text_label
from budget.ui.theme.tokens import SPACING


def money_input(placeholder: str = "0") -> QLineEdit:
    """Поле суми: праворуч, без обмеження на розбір — розбирає лише domain.money."""
    field = QLineEdit()
    field.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    field.setPlaceholderText(placeholder)
    return field


class LabeledField(QWidget):
    """Підпис над полем і текст помилки під ним."""

    def __init__(self, label: str, field: QWidget) -> None:
        super().__init__()
        self.field = field
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACING[1])
        layout.addWidget(text_label(label, "caption", muted=True))
        layout.addWidget(field)
        self.error = QLabel()
        self.error.setProperty("tone", "error")
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)

    def set_error(self, message: str | None) -> None:
        self.error.setText(message or "")
        self.error.setVisible(bool(message))


class Notice(QFrame):
    """Повідомлення: «Обмеження» (бізнес-правило) або «Помилка»."""

    def __init__(self, title: str, text: str = "", *, error: bool = False) -> None:
        super().__init__()
        self.setObjectName("ErrorNotice" if error else "Notice")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACING[4], SPACING[3], SPACING[4], SPACING[3])
        self.title = text_label(title, "body-strong")
        self.body = text_label(text, "body")
        layout.addWidget(self.title)
        layout.addWidget(self.body)

    def set_text(self, text: str) -> None:
        self.body.setText(text)


class ListRow(QFrame):
    """Рядок редакційного списку: назва, другий рядок, сума, позначка архіву."""

    def __init__(
        self,
        title: str,
        secondary: str,
        amount: Money,
        *,
        archived: bool = False,
        actions: QWidget | None = None,
    ):
        super().__init__()
        self.setObjectName("ListRow")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(SPACING[4], SPACING[3], SPACING[4], SPACING[3])
        texts = QVBoxLayout()
        texts.setSpacing(2)
        texts.addWidget(text_label(title, "body-strong"))
        texts.addWidget(text_label(secondary, "secondary", muted=True))
        layout.addLayout(texts, 1)
        if archived:
            layout.addWidget(text_label("В архіві", "caption", muted=True))
        layout.addWidget(amount_label(amount))
        if actions is not None:
            layout.addWidget(actions)
