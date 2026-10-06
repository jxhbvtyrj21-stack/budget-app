"""Заглушки екранів: заголовок і порожня панель. Вміст реалізується на наступних етапах."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QScrollArea, QVBoxLayout, QWidget

from budget.ui.components.basic import Panel, text_label
from budget.ui.theme.tokens import CONTENT_MAX_WIDTH, SPACING

PLACEHOLDER_TEXT = "Розділ буде реалізовано на наступних етапах."


class PlaceholderPage(QScrollArea):
    def __init__(self, title: str) -> None:
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        content.setObjectName("ContentArea")
        content.setMaximumWidth(CONTENT_MAX_WIDTH)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(SPACING[6], SPACING[6], SPACING[6], SPACING[6])
        layout.setSpacing(SPACING[7])
        self.title_label = text_label(title, "title")
        layout.addWidget(self.title_label)
        panel = Panel()
        panel.body.addWidget(text_label(PLACEHOLDER_TEXT, "body", muted=True))
        layout.addWidget(panel)
        layout.addStretch(1)
        # Вміст до CONTENT_MAX_WIDTH, по центру області вмісту (design-system.md, 4).
        viewport = QWidget()
        viewport.setObjectName("PageViewport")
        viewport.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        centered = QHBoxLayout(viewport)
        centered.setContentsMargins(0, 0, 0, 0)
        centered.addStretch(1)
        centered.addWidget(content, 100)
        centered.addStretch(1)
        self.setWidget(viewport)
