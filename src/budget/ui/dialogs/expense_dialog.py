"""Форма звичайної витрати: назва, опис, сума, одне джерело (ui-information-architecture.md, 9).

Джерело обирає лише користувач; форма показує доступний залишок обраного джерела й
не пропонує іншого. Недостатній залишок — повідомлення «Обмеження» без збереження.
"""

from PySide6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QLineEdit, QVBoxLayout

from budget.errors import BudgetError, ValidationError
from budget.services.expense import ExpenseService, ExpenseView, SourceOption
from budget.ui.components.basic import button, text_label
from budget.ui.components.forms import LabeledField, Notice, money_input
from budget.ui.formatting import format_money, parse_money_input
from budget.ui.messages import insufficient_funds_text, user_text
from budget.ui.theme.tokens import SPACING


class ExpenseDialog(QDialog):
    def __init__(self, service: ExpenseService, editing: ExpenseView | None = None, parent=None):
        super().__init__(parent)
        self._service = service
        self._editing = editing
        title = "Зміна витрати" if editing else "Нова витрата"
        self.setWindowTitle(title)
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACING[5], SPACING[5], SPACING[5], SPACING[5])
        layout.setSpacing(SPACING[4])
        layout.addWidget(text_label(title, "heading"))

        self.name = LabeledField("Назва (обов'язково)", QLineEdit())
        self.description = LabeledField("Опис", QLineEdit())
        self.amount = LabeledField("Сума", money_input())
        self.source_combo = QComboBox()
        self.source = LabeledField("Джерело", self.source_combo)
        self.available = text_label("", "secondary", muted=True)
        for widget in (self.name, self.description, self.amount, self.source, self.available):
            layout.addWidget(widget)

        self.limit = Notice("Обмеження")
        self.limit.hide()
        layout.addWidget(self.limit)
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

        self._lock_reason = service.financial_lock_reason(editing.expense) if editing else None
        self._options = self._load_options()
        for option in self._options:
            self.source_combo.addItem(option.name)
        if editing is not None:
            expense = editing.expense
            self.name.field.setText(expense.name)
            self.description.field.setText(expense.description or "")
            self.amount.field.setText(format_money(expense.amount))
            index = next((i for i, o in enumerate(self._options) if o.source == expense.source), -1)
            self.source_combo.setCurrentIndex(index)
        if self._lock_reason is not None:
            # Фінансові поля заблоковані; назву й опис змінювати можна (Q191).
            self.amount.field.setEnabled(False)
            self.source_combo.setEnabled(False)
            self.limit.set_text(self._lock_reason)
            self.limit.show()
        self.amount.field.textChanged.connect(self.validate)
        self.source_combo.currentIndexChanged.connect(self.validate)
        self.validate()

    def _load_options(self) -> list[SourceOption]:
        expense = self._editing.expense if self._editing else None
        options = self._service.source_options(editing=expense)
        if expense is not None and self._lock_reason is not None:
            if not any(o.source == expense.source for o in options):
                options.append(
                    SourceOption(expense.source, self._editing.source_name, expense.amount)
                )
        return options

    def selected(self) -> SourceOption | None:
        index = self.source_combo.currentIndex()
        return self._options[index] if 0 <= index < len(self._options) else None

    def validate(self) -> bool:
        """Перевіряє суму проти доступного залишку обраного джерела без збереження."""
        option = self.selected()
        self.available.setText(
            f"Доступно в джерелі: {format_money(option.available)}" if option else ""
        )
        if self._lock_reason is not None:
            self.save_button.setEnabled(True)
            return True
        self.limit.hide()
        try:
            amount = parse_money_input(self.amount.field.text())
        except ValidationError:
            amount = None
        ok = option is not None and amount is not None and amount.is_positive
        if ok and amount > option.available:
            self.limit.set_text(insufficient_funds_text(option.name, option.available, amount))
            self.limit.show()
            ok = False
        self.save_button.setEnabled(ok)
        return ok

    def save(self) -> None:
        for field in (self.name, self.amount, self.source):
            field.set_error(None)
        self.failure.hide()
        option = self.selected()
        if option is None:
            self.source.set_error("Оберіть джерело.")
            return
        try:
            amount = parse_money_input(self.amount.field.text())
            if amount is None:
                raise ValidationError("Вкажіть суму.")
        except ValidationError as error:
            self.amount.set_error(error.user_message)
            return
        name, description = self.name.field.text(), self.description.field.text()
        try:
            if self._editing is None:
                self._service.create(name, description, amount, option.source)
            else:
                self._service.update(
                    self._editing.expense.id, name, description, amount, option.source
                )
        except ValidationError as error:
            target = self.name if not name.strip() else self.amount
            target.set_error(error.user_message)
            return
        except BudgetError as error:
            self.failure.set_text(user_text(error))
            self.failure.show()
            return
        self.accept()
