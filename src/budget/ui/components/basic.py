"""Базові елементи: текстові ролі, кнопки, панель, сума."""

from PySide6.QtWidgets import QFrame, QLabel, QPushButton, QVBoxLayout, QWidget

from budget.domain.money import Money
from budget.ui.formatting import format_money
from budget.ui.theme.tokens import SPACING, TYPE_SCALE


def text_label(text: str, role: str = "body", *, muted: bool = False) -> QLabel:
    """Підпис із роллю шкали типографіки (``TYPE_SCALE``)."""
    if role not in TYPE_SCALE:
        raise KeyError(role)
    label = QLabel(text)
    label.setProperty("textRole", role)
    if muted:
        label.setProperty("tone", "muted")
    label.setWordWrap(True)
    return label


def amount_label(amount: Money, role: str = "amount") -> QLabel:
    """Сума: лише ``format_money``, колір — основний текст (design-system.md, 2.4)."""
    label = text_label(format_money(amount), role)
    label.setWordWrap(False)
    return label


def button(text: str, variant: str = "secondary") -> QPushButton:
    if variant not in ("primary", "secondary"):
        raise ValueError(variant)
    widget = QPushButton(text)
    widget.setProperty("variant", variant)
    return widget


class Panel(QFrame):
    """Панель: поверхня з тонкою межею, без тіні."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Panel")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(SPACING[5], SPACING[5], SPACING[5], SPACING[5])
        self.body.setSpacing(SPACING[3])
