"""Форми «Нове накопичення» й «Метадані накопичення» (ui-information-architecture.md, 9).

Поля: назва (обов'язково), опис, цільова сума (необов'язково, порожнє поле —
без цілі). Початкового балансу немає (Q172); новому накопиченню статус «Активне»
призначає сервіс (Q173).
"""

from PySide6.QtWidgets import QDialog, QHBoxLayout, QLineEdit, QVBoxLayout

from budget.errors import BudgetError, ValidationError
from budget.services.accumulation import AccumulationService, AccumulationView
from budget.ui.components.basic import button, text_label
from budget.ui.components.forms import LabeledField, Notice, money_input
from budget.ui.formatting import format_money, parse_money_input
from budget.ui.messages import user_text
from budget.ui.theme.tokens import SPACING

TARGET_HINT = "Цільова сума — орієнтир, а не ліміт. Статус не зміниться автоматично."


class AccumulationDialog(QDialog):
    def __init__(
        self,
        service: AccumulationService,
        editing: AccumulationView | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self._service = service
        self._editing = editing
        title = "Назва, опис і цільова сума" if editing else "Нове накопичення"
        self.setWindowTitle(title)
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACING[5], SPACING[5], SPACING[5], SPACING[5])
        layout.setSpacing(SPACING[4])
        layout.addWidget(text_label(title, "heading"))

        self.name = LabeledField("Назва (обов'язково)", QLineEdit())
        self.description = LabeledField("Опис", QLineEdit())
        self.target = LabeledField("Цільова сума (необов'язково)", money_input(""))
        for widget in (self.name, self.description, self.target):
            layout.addWidget(widget)
        layout.addWidget(text_label(TARGET_HINT, "secondary", muted=True))
        if editing is None:
            layout.addWidget(
                text_label(
                    "Нове накопичення має залишок 0 і статус «Активне». "
                    "Кошти додаються поповненням.",
                    "secondary",
                    muted=True,
                )
            )

        self.failure = Notice("Не вдалося зберегти", error=True)
        self.failure.hide()
        layout.addWidget(self.failure)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = button("Скасувати")
        cancel.clicked.connect(self.reject)
        self.save_button = button("Зберегти" if editing else "Створити", "primary")
        self.save_button.clicked.connect(self.save)
        buttons.addWidget(cancel)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)

        if editing is not None:
            accumulation = editing.accumulation
            self.name.field.setText(accumulation.name)
            self.description.field.setText(accumulation.description or "")
            if accumulation.target is not None:
                self.target.field.setText(format_money(accumulation.target))

    def save(self) -> None:
        for field in (self.name, self.target):
            field.set_error(None)
        self.failure.hide()
        try:
            target = parse_money_input(self.target.field.text())
        except ValidationError as error:
            self.target.set_error(error.user_message)
            return
        name, description = self.name.field.text(), self.description.field.text()
        if not name.strip():
            self.name.set_error("Вкажіть назву.")
            return
        try:
            if self._editing is None:
                self._service.create(name, description, target)
            else:
                self._service.update_metadata(
                    self._editing.accumulation.id, name, description, target
                )
        except ValidationError as error:
            self.target.set_error(error.user_message)
            return
        except BudgetError as error:
            self.failure.set_text(user_text(error))
            self.failure.show()
            return
        self.accept()
