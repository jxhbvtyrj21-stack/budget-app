"""Каркас сторінки: заголовок і вміст до CONTENT_MAX_WIDTH, по центру (design-system.md, 4)."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QScrollArea, QVBoxLayout, QWidget

from budget.ui.components.basic import text_label
from budget.ui.theme.tokens import CONTENT_MAX_WIDTH, SPACING


class Page(QScrollArea):
    def __init__(self, title: str) -> None:
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        content.setObjectName("ContentArea")
        content.setMaximumWidth(CONTENT_MAX_WIDTH)
        self.body = QVBoxLayout(content)
        self.body.setContentsMargins(SPACING[6], SPACING[6], SPACING[6], SPACING[6])
        self.body.setSpacing(SPACING[6])
        self.header = QHBoxLayout()
        self.title_label = text_label(title, "title")
        self.header.addWidget(self.title_label, 1)
        self.body.addLayout(self.header)
        viewport = QWidget()
        viewport.setObjectName("PageViewport")
        viewport.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        centered = QHBoxLayout(viewport)
        centered.setContentsMargins(0, 0, 0, 0)
        centered.addStretch(1)
        centered.addWidget(content, 100)
        centered.addStretch(1)
        self.setWidget(viewport)


def clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            # Від'єднати одразу: до відкладеного знищення віджет не повинен лишатися видимим.
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()
        elif item.layout() is not None:
            clear_layout(item.layout())
