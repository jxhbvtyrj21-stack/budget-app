"""Форма нового доходу: сума, назва (обов'язково), опис. Місяць — поточний (ADR 0009)."""

from PySide6.QtWidgets import QDialog, QHBoxLayout, QLineEdit, QVBoxLayout

from budget.errors import BudgetError, ValidationError
from budget.services.income import IncomeService
from budget.ui.components.basic import button, text_label
from budget.ui.components.forms import LabeledField, Notice, money_input
from budget.ui.formatting import parse_money_input
from budget.ui.theme.tokens import SPACING


class IncomeDialog(QDialog):
    def __init__(self, incomes: IncomeService, parent=None) -> None:
        super().__init__(parent)
        self._incomes = incomes
        self.setWindowTitle("Новий дохід")
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACING[5], SPACING[5], SPACING[5], SPACING[5])
        layout.setSpacing(SPACING[4])
        layout.addWidget(text_label("Новий дохід", "heading"))
        layout.addWidget(
            text_label(
                "Дохід належатиме поточному місяцю. Після збереження його не можна "
                "змінити — помилку виправляють новим доходом.",
                "secondary",
                muted=True,
            )
        )
        self.amount = LabeledField("Сума", money_input())
        self.name = LabeledField("Назва (обов'язково)", QLineEdit())
        self.description = LabeledField("Опис", QLineEdit())
        for field in (self.amount, self.name, self.description):
            layout.addWidget(field)
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
        self.name.set_error(None)
        self.failure.hide()
        try:
            amount = parse_money_input(self.amount.field.text())
            if amount is None:
                raise ValidationError("Вкажіть суму.")
        except ValidationError as error:
            self.amount.set_error(error.user_message)
            return
        try:
            self._incomes.create(self.name.field.text(), self.description.field.text(), amount)
        except ValidationError as error:
            target = self.name if not self.name.field.text().strip() else self.amount
            target.set_error(error.user_message)
            return
        except BudgetError as error:
            self.failure.set_text(error.user_message)
            self.failure.show()
            return
        self.accept()
