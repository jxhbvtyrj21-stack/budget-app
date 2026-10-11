"""Заглушки екранів, які реалізуються на наступних етапах."""

from budget.ui.components.basic import Panel, text_label
from budget.ui.screens.page import Page

PLACEHOLDER_TEXT = "Розділ буде реалізовано на наступних етапах."


class PlaceholderPage(Page):
    def __init__(self, title: str) -> None:
        super().__init__(title)
        panel = Panel()
        panel.body.addWidget(text_label(PLACEHOLDER_TEXT, "body", muted=True))
        self.body.addWidget(panel)
        self.body.addStretch(1)
