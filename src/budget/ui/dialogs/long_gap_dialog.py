"""Спеціальний діалог після тривалої перерви (ADR 0009, ui-information-architecture.md 10.2).

Один діалог, загальна сума без розбивки, дві рівноцінні дії; закриття без вибору
нічого не змінює й не обирає варіант автоматично.
"""

from PySide6.QtWidgets import QDialog, QHBoxLayout, QVBoxLayout

from budget.domain.money import Money
from budget.services.month import LongGapChoice
from budget.ui.components.basic import button, text_label
from budget.ui.formatting import format_money
from budget.ui.theme.tokens import SPACING


class LongGapDialog(QDialog):
    def __init__(self, total: Money, parent=None) -> None:
        super().__init__(parent)
        self.choice: LongGapChoice | None = None
        self.setWindowTitle("Залишки доходів за минулі місяці")
        self.setMinimumWidth(600)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACING[5], SPACING[5], SPACING[5], SPACING[5])
        layout.setSpacing(SPACING[4])
        layout.addWidget(text_label("Залишки доходів за минулі місяці", "heading"))
        layout.addWidget(
            text_label(
                "Під час перерви в роботі залишилися невикористані залишки доходів на "
                f"загальну суму {format_money(total)}. Оберіть, що з ними зробити.",
                "body",
            )
        )
        options = QHBoxLayout()
        options.setSpacing(SPACING[5])
        for choice, title, explanation in (
            (
                LongGapChoice.TRANSFER,
                "Перенести",
                "Сума буде додана до загального нерозподіленого залишку.",
            ),
            (
                LongGapChoice.REMOVE,
                "Вилучити",
                "Сума буде прибрана з доступних коштів. Загальний нерозподілений залишок "
                "і накопичення не зміняться.",
            ),
        ):
            column = QVBoxLayout()
            action = button(title)
            action.clicked.connect(lambda _=False, c=choice: self._choose(c))
            column.addWidget(action)
            column.addWidget(text_label(explanation, "secondary", muted=True))
            options.addLayout(column, 1)
        layout.addLayout(options)

    def _choose(self, choice: LongGapChoice) -> None:
        self.choice = choice
        self.accept()
