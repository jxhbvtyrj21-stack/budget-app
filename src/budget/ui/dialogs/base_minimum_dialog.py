"""Форма «Базовий мінімум місяця» (ui-information-architecture.md, 9; ADR 0002, ADR 0022).

Одна сума ≥ 0 для поточного місяця. Це орієнтир, а не ліміт чи план: збереження не
змінює залишків і не створює фінансового запису. Видалення немає.
"""

from PySide6.QtWidgets import QDialog, QHBoxLayout, QVBoxLayout

from budget.domain.calendar import CalendarMonth
from budget.errors import BudgetError, ValidationError
from budget.services.base_minimum import BaseMinimumService
from budget.ui.components.basic import button, text_label
from budget.ui.components.forms import LabeledField, Notice, money_input
from budget.ui.formatting import format_money, format_month, parse_money_input
from budget.ui.messages import user_text
from budget.ui.theme.tokens import SPACING

HINT = (
    "Базовий мінімум — орієнтир для порівняння з фактичними витратами, а не ліміт. "
    "На наступний місяць він не переноситься."
)


class BaseMinimumDialog(QDialog):
    def __init__(self, service: BaseMinimumService, month: CalendarMonth, parent=None):
        super().__init__(parent)
        self._service = service
        self._month = month
        current = service.get(month)
        title = "Змінити базовий мінімум" if current is not None else "Задати базовий мінімум"
        self.setWindowTitle(title)
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACING[5], SPACING[5], SPACING[5], SPACING[5])
        layout.setSpacing(SPACING[4])
        layout.addWidget(text_label(title, "heading"))
        layout.addWidget(text_label(format_month(month), "secondary", muted=True))
        self.amount = LabeledField("Сума", money_input())
        if current is not None:
            self.amount.field.setText(format_money(current))
        layout.addWidget(self.amount)
        layout.addWidget(text_label(HINT, "secondary", muted=True))
        self.failure = Notice("Не вдалося зберегти", error=True)
        self.failure.hide()
        layout.addWidget(self.failure)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = button("Скасувати")
        cancel.clicked.connect(self.reject)
        self.save_button = button("Зберегти", "primary")
        self.save_button.clicked.connect(self.save)
        buttons.addWidget(cancel)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)

    def save(self) -> None:
        self.amount.set_error(None)
        self.failure.hide()
        try:
            amount = parse_money_input(self.amount.field.text())
            if amount is None:
                raise ValidationError("Вкажіть суму.")
            self._service.set(self._month, amount)
        except ValidationError as error:
            self.amount.set_error(error.user_message)
            return
        except BudgetError as error:
            self.failure.set_text(user_text(error))
            self.failure.show()
            return
        self.accept()
