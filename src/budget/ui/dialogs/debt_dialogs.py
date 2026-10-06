"""Форми боргів (ui-information-architecture.md, 7, 9; ADR 0018, ADR 0022).

- «Отримання позикових коштів»: назва, опис, сума; під час зміни в поточному
  місяці — лише сума (назву й опис змінюють у картці боргу).
- «Погашення»: одне джерело з доступним залишком, сума, опис; показує залишок боргу.
  Джерело обирає лише користувач; форма не підбирає іншого. Для погашення, яке не
  можна змінити фінансово (минулий місяць, Q168, Q189), редагується лише опис.
- «Назва й опис боргу»: метадані, доступні завжди.
"""

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QLineEdit, QVBoxLayout

from budget.domain.money import Money
from budget.errors import BudgetError, ValidationError
from budget.services.balances import DebtView
from budget.services.debt import DebtService, RepaymentView
from budget.services.expense import SourceOption
from budget.ui.components.basic import button, text_label
from budget.ui.components.forms import LabeledField, Notice, money_input
from budget.ui.formatting import format_money, parse_money_input
from budget.ui.messages import insufficient_funds_text, overpayment_text, user_text
from budget.ui.theme.tokens import SPACING

LOAN_HINT = "Сума буде зарахована до загального нерозподіленого залишку. Це не дохід."
REPAYMENT_HINT = "Погашення зменшує залишок обраного джерела й залишок боргу. Це не витрата."


class _DebtDialog(QDialog):
    """Спільний каркас форми: заголовок, поля, повідомлення, кнопки."""

    def __init__(self, title: str, save_text: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(480)
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(SPACING[5], SPACING[5], SPACING[5], SPACING[5])
        self.layout_.setSpacing(SPACING[4])
        self.layout_.addWidget(text_label(title, "heading"))
        self.fields = QVBoxLayout()
        self.fields.setSpacing(SPACING[4])
        self.layout_.addLayout(self.fields)
        self.limit = Notice("Обмеження")
        self.limit.hide()
        self.layout_.addWidget(self.limit)
        self.failure = Notice("Не вдалося зберегти", error=True)
        self.failure.hide()
        self.layout_.addWidget(self.failure)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = button("Скасувати")
        cancel.clicked.connect(self.reject)
        self.save_button = button(save_text, "primary")
        self.save_button.clicked.connect(self.save)
        buttons.addWidget(cancel)
        buttons.addWidget(self.save_button)
        self.layout_.addLayout(buttons)

    def save(self) -> None:  # pragma: no cover — перевизначається
        raise NotImplementedError

    def fit_height(self) -> None:
        """Висота під перенесений текст повідомлень; після оновлення макета.

        Контекст — сам діалог: після його знищення відкладений виклик скасовується.
        """
        QTimer.singleShot(0, self, self._apply_height)

    def _apply_height(self) -> None:
        needed = self.layout_.totalHeightForWidth(self.width())
        if needed > self.height():
            self.resize(self.width(), needed)

    def show_failure(self, error: BudgetError) -> None:
        self.failure.set_text(user_text(error))
        self.failure.show()


class LoanReceiptDialog(_DebtDialog):
    def __init__(self, service: DebtService, editing: DebtView | None = None, parent=None):
        title = "Зміна суми отримання" if editing else "Отримання позикових коштів"
        super().__init__(title, "Зберегти" if editing else "Отримати", parent)
        self._service = service
        self._editing = editing
        self.name = LabeledField("Назва (обов'язково)", QLineEdit())
        self.description = LabeledField("Опис", QLineEdit())
        self.amount = LabeledField("Сума", money_input())
        if editing is None:
            self.fields.addWidget(self.name)
            self.fields.addWidget(self.description)
        else:
            self.fields.addWidget(text_label(f"Борг «{editing.debt.name}»", "body-strong"))
            self.fields.addWidget(
                text_label(
                    f"Погашено: {format_money(editing.repaid)}. Назву й опис змінюють у "
                    "картці боргу.",
                    "secondary",
                    muted=True,
                )
            )
            self.amount.field.setText(format_money(editing.debt.amount))
        self.fields.addWidget(self.amount)
        self.fields.addWidget(text_label(LOAN_HINT, "secondary", muted=True))

    def save(self) -> None:
        for field in (self.name, self.amount):
            field.set_error(None)
        self.failure.hide()
        try:
            amount = parse_money_input(self.amount.field.text())
            if amount is None:
                raise ValidationError("Вкажіть суму.")
        except ValidationError as error:
            self.amount.set_error(error.user_message)
            return
        try:
            if self._editing is None:
                name = self.name.field.text()
                if not name.strip():
                    self.name.set_error("Вкажіть назву.")
                    return
                self._service.receive_loan(name, self.description.field.text(), amount)
            else:
                self._service.update_loan_amount(self._editing.debt.id, amount)
        except ValidationError as error:
            self.amount.set_error(error.user_message)
            return
        except BudgetError as error:
            self.show_failure(error)
            return
        self.accept()


class RepaymentDialog(_DebtDialog):
    def __init__(
        self,
        service: DebtService,
        debt_id: int | None = None,
        editing: RepaymentView | None = None,
        parent=None,
    ):
        title = "Зміна погашення" if editing else "Погашення боргу"
        super().__init__(title, "Зберегти" if editing else "Погасити", parent)
        self._service = service
        self._editing = editing
        current = editing.repayment if editing else None
        self._debt = service.get(current.debt_id if current else debt_id)
        self._lock_reason = service.repayment_lock_reason(current) if current else None
        self._options = service.source_options(editing=current)
        if current is not None and all(o.source != current.source for o in self._options):
            # Заблоковане джерело (архівований дохід чи накопичення) — лише показ.
            self._options.append(SourceOption(current.source, editing.source_name, current.amount))

        # Під час зміни сума цього погашення рахується доступною для боргу.
        released = current.amount if current else Money.zero()
        self._debt_available = self._debt.remaining + released
        self.fields.addWidget(text_label(f"Борг «{self._debt.debt.name}»", "body-strong"))
        self.debt_remaining = text_label(
            f"Залишок боргу: {format_money(self._debt.remaining)}", "secondary", muted=True
        )
        self.fields.addWidget(self.debt_remaining)
        self.source_combo = QComboBox()
        for option in self._options:
            self.source_combo.addItem(option.name)
        self.source = LabeledField("Джерело", self.source_combo)
        self.available = text_label("", "secondary", muted=True)
        self.amount = LabeledField("Сума", money_input())
        self.description = LabeledField("Опис", QLineEdit())
        for widget in (self.source, self.available, self.amount, self.description):
            self.fields.addWidget(widget)
        self.fields.addWidget(text_label(REPAYMENT_HINT, "secondary", muted=True))
        if current is not None:
            index = next(i for i, o in enumerate(self._options) if o.source == current.source)
            self.source_combo.setCurrentIndex(index)
            self.amount.field.setText(format_money(current.amount))
            self.description.field.setText(current.description or "")
        if self._lock_reason is not None:
            self.source_combo.setEnabled(False)
            self.amount.field.setEnabled(False)
        self.amount.field.textChanged.connect(self.validate)
        self.source_combo.currentIndexChanged.connect(self.validate)
        self.validate()

    def selected(self) -> SourceOption | None:
        index = self.source_combo.currentIndex()
        return self._options[index] if 0 <= index < len(self._options) else None

    def validate(self) -> bool:
        """Перевіряє суму проти залишку обраного джерела й залишку боргу без збереження."""
        option = self.selected()
        self.available.setText(
            f"Доступно в джерелі: {format_money(option.available)}" if option else ""
        )
        if self._lock_reason is not None:
            self.limit.set_text(self._lock_reason)
            self.limit.show()
            self.save_button.setEnabled(True)
            return True
        try:
            amount = parse_money_input(self.amount.field.text())
        except ValidationError:
            amount = None
        messages = []
        ok = option is not None and amount is not None and amount.is_positive
        if ok and amount > option.available:
            messages.append(insufficient_funds_text(option.name, option.available, amount))
        if ok and amount > self._debt_available:
            messages.append(overpayment_text(self._debt.debt.name, self._debt_available, amount))
        self.limit.set_text("\n".join(messages))
        self.limit.setVisible(bool(messages))
        self.save_button.setEnabled(ok and not messages)
        self.fit_height()
        return ok and not messages

    def save(self) -> None:
        for field in (self.amount, self.source):
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
        description = self.description.field.text()
        try:
            if self._editing is None:
                self._service.repay(self._debt.debt.id, amount, option.source, description)
            else:
                self._service.update_repayment(
                    self._editing.repayment.id, amount, option.source, description
                )
        except BudgetError as error:
            self.show_failure(error)
            return
        self.accept()


class DebtMetadataDialog(_DebtDialog):
    def __init__(self, service: DebtService, view: DebtView, parent=None):
        super().__init__("Назва й опис боргу", "Зберегти", parent)
        self._service = service
        self._view = view
        self.name = LabeledField("Назва (обов'язково)", QLineEdit(view.debt.name))
        self.description = LabeledField("Опис", QLineEdit(view.debt.description or ""))
        self.fields.addWidget(self.name)
        self.fields.addWidget(self.description)
        self.fields.addWidget(
            text_label(
                "Назва й опис — метадані: суми, залишок і історія погашень не змінюються.",
                "secondary",
                muted=True,
            )
        )

    def save(self) -> None:
        self.name.set_error(None)
        self.failure.hide()
        name = self.name.field.text()
        if not name.strip():
            self.name.set_error("Вкажіть назву.")
            return
        try:
            self._service.update_metadata(self._view.debt.id, name, self.description.field.text())
        except BudgetError as error:
            self.show_failure(error)
            return
        self.accept()
