"""Майстер первинного налаштування (ui-information-architecture.md, розділ 11).

Кроки: вітання, наявні кошти, накопичення, борги, перевірка. Чернетка зберігається
під час переходу між кроками й під час закриття вікна (Q176, Q178).

Помилка збереження (IA 12): кожна дія спершу зберігає змінену копію чернетки
(candidate) і лише після успіху робить її поточною й оновлює вікно. Невдача —
«Помилка», а показаний крок, чернетка, поля й перелік лишаються узгодженими й
незмінними, тож дію можна повторити.
"""

import logging
from dataclasses import replace

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from budget.domain.money import Money
from budget.errors import BudgetError, DataWriteError, ValidationError
from budget.services.setup import (
    InitialAccumulation,
    InitialDebt,
    InitialSetupService,
    SetupDraft,
    SetupStep,
)
from budget.ui.components.basic import Panel, button, text_label
from budget.ui.components.forms import LabeledField, ListRow, money_input
from budget.ui.formatting import format_money, parse_money_input
from budget.ui.screens.page import Page, clear_layout
from budget.ui.theme.tokens import SPACING

STEP_TITLES = {
    SetupStep.WELCOME: "Вітання",
    SetupStep.GENERAL_REMAINDER: "Наявні кошти",
    SetupStep.ACCUMULATIONS: "Накопичення",
    SetupStep.DEBTS: "Борги",
    SetupStep.REVIEW: "Перевірка",
}
CAN_SKIP = "Можна пропустити."  # кроки 3–4 без записів (IA 12)
SAVE_FAILED_TITLE = "Помилка"
SAVE_FAILED = (
    "Не вдалося зберегти зміни майстра. Введені значення залишилися в майстрі, тож дію "
    "можна повторити."
)
CLOSE_SAVE_FAILED = (
    "Не вдалося зберегти незавершене налаштування, тому вікно не закрито. Введені значення "
    "залишилися в майстрі — спробуйте закрити ще раз."
)

log = logging.getLogger(__name__)


class SetupWizardPage(Page):
    completed = Signal()

    def __init__(self, setup: InitialSetupService) -> None:
        super().__init__("Первинне налаштування")
        self._setup = setup
        self.draft = setup.draft()
        self.step_label = text_label("", "subheading", muted=True)
        self.body.addWidget(self.step_label)

        self.steps = QStackedWidget()
        self.steps.addWidget(self._welcome())
        self.steps.addWidget(self._remainder_step())
        self.steps.addWidget(self._accumulations_step())
        self.steps.addWidget(self._debts_step())
        self.steps.addWidget(self._review_step())
        self.body.addWidget(self.steps)

        footer = QHBoxLayout()
        self.reset_button = button("Почати заново", "danger-text")
        self.reset_button.clicked.connect(self._confirm_reset)
        footer.addWidget(self.reset_button)
        footer.addStretch(1)
        self.back_button = button("Назад")
        self.back_button.clicked.connect(lambda: self.go_to(self.draft.step - 1))
        self.next_button = button("Далі", "primary")
        self.next_button.clicked.connect(self._next)
        self.finish_button = button("Завершити налаштування", "primary")
        self.finish_button.clicked.connect(self.finish)
        for widget in (self.back_button, self.next_button, self.finish_button):
            footer.addWidget(widget)
        self.body.addLayout(footer)
        self.body.addStretch(1)
        self._show(self.draft.step)

    # Кроки -----------------------------------------------------------------------------

    def _welcome(self) -> QWidget:
        panel = Panel()
        panel.body.addWidget(text_label("Налаштуймо стартовий стан бюджету.", "heading"))
        panel.body.addWidget(
            text_label(
                "Введіть кошти, накопичення й борги, які вже є до початку роботи. "
                "Це стартовий стан, а не доходи чи витрати. Налаштування можна закрити "
                "й продовжити пізніше.",
                "body",
                muted=True,
            )
        )
        return panel

    def _remainder_step(self) -> QWidget:
        panel = Panel()
        self.remainder_input = money_input()
        if self.draft.general_remainder.is_positive:
            self.remainder_input.setText(format_money(self.draft.general_remainder))
        self.remainder_field = LabeledField(
            "Початковий загальний нерозподілений залишок (необов'язково)", self.remainder_input
        )
        panel.body.addWidget(self.remainder_field)
        panel.body.addWidget(
            text_label(
                "Кошти, які вже є до початку роботи. Не створює доходу.", "secondary", muted=True
            )
        )
        return panel

    def _accumulations_step(self) -> QWidget:
        panel = Panel()
        panel.body.addWidget(
            text_label("Усі накопичення отримають статус «Активне».", "secondary", muted=True)
        )
        self.accumulation_list = QVBoxLayout()
        panel.body.addLayout(self.accumulation_list)
        self.acc_name = LabeledField("Назва (обов'язково)", QLineEdit())
        self.acc_description = LabeledField("Опис", QLineEdit())
        self.acc_balance = LabeledField("Початковий баланс", money_input())
        self.acc_target = LabeledField("Цільова сума (необов'язково)", money_input(""))
        row = QHBoxLayout()
        row.setSpacing(SPACING[4])
        for field in (self.acc_name, self.acc_description, self.acc_balance, self.acc_target):
            row.addWidget(field)
        panel.body.addLayout(row)
        self.add_accumulation_button = button("Додати накопичення")
        self.add_accumulation_button.clicked.connect(self.add_accumulation)
        panel.body.addWidget(self.add_accumulation_button)
        return panel

    def _debts_step(self) -> QWidget:
        panel = Panel()
        panel.body.addWidget(
            text_label(
                "Це наявний борг. Він не додається до наявних коштів і не є отриманням "
                "позикових коштів.",
                "secondary",
                muted=True,
            )
        )
        self.debt_list = QVBoxLayout()
        panel.body.addLayout(self.debt_list)
        self.debt_name = LabeledField("Назва (обов'язково)", QLineEdit())
        self.debt_description = LabeledField("Опис", QLineEdit())
        self.debt_balance = LabeledField("Поточний залишок боргу", money_input())
        row = QHBoxLayout()
        row.setSpacing(SPACING[4])
        for field in (self.debt_name, self.debt_description, self.debt_balance):
            row.addWidget(field)
        panel.body.addLayout(row)
        self.add_debt_button = button("Додати борг")
        self.add_debt_button.clicked.connect(self.add_debt)
        panel.body.addWidget(self.add_debt_button)
        return panel

    def _review_step(self) -> QWidget:
        panel = Panel()
        self.review = QVBoxLayout()
        panel.body.addLayout(self.review)
        return panel

    # Дії -------------------------------------------------------------------------------

    def go_to(self, step: int) -> None:
        self._move(self.draft, step)

    def _next(self) -> None:
        candidate = self.draft
        if self.draft.step is SetupStep.GENERAL_REMAINDER:
            amount = self._read_remainder()
            if amount is None:
                return
            candidate = replace(candidate, general_remainder=amount)
        self._move(candidate, self.draft.step + 1)

    def _move(self, candidate: SetupDraft, step: int) -> None:
        """Крок змінюється лише після збереження чернетки з цим кроком."""
        step = SetupStep(max(SetupStep.WELCOME, min(SetupStep.REVIEW, step)))
        if self._save(replace(candidate, step=step)):
            self._show(step)

    def _read_remainder(self) -> Money | None:
        try:
            amount = parse_money_input(self.remainder_input.text()) or Money.zero()
        except ValidationError as error:
            self.remainder_field.set_error(error.user_message)
            return None
        self.remainder_field.set_error(None)
        return amount

    def add_accumulation(self) -> None:
        fields = (self.acc_name, self.acc_description, self.acc_balance, self.acc_target)
        for field in fields:
            field.set_error(None)
        try:
            balance = parse_money_input(self.acc_balance.field.text()) or Money.zero()
        except ValidationError as error:
            self.acc_balance.set_error(error.user_message)
            return
        try:
            target = parse_money_input(self.acc_target.field.text())
        except ValidationError as error:
            self.acc_target.set_error(error.user_message)
            return
        try:
            item = InitialAccumulation(
                self.acc_name.field.text(), self.acc_description.field.text(), balance, target
            )
        except ValidationError as error:
            self.acc_name.set_error(error.user_message)
            return
        if not self._save(replace(self.draft, accumulations=(*self.draft.accumulations, item))):
            return  # поля лишаються заповненими
        for field in fields:
            field.field.clear()
        self._render_lists()

    def add_debt(self) -> None:
        fields = (self.debt_name, self.debt_description, self.debt_balance)
        for field in fields:
            field.set_error(None)
        try:
            balance = parse_money_input(self.debt_balance.field.text()) or Money.zero()
            item = InitialDebt(
                self.debt_name.field.text(), self.debt_description.field.text(), balance
            )
        except ValidationError as error:
            target = (
                self.debt_name if not self.debt_name.field.text().strip() else self.debt_balance
            )
            target.set_error(error.user_message)
            return
        if not self._save(replace(self.draft, debts=(*self.draft.debts, item))):
            return  # поля лишаються заповненими
        for field in fields:
            field.field.clear()
        self._render_lists()

    def remove_accumulation(self, index: int) -> None:
        items = list(self.draft.accumulations)
        del items[index]
        if self._save(replace(self.draft, accumulations=tuple(items))):
            self._render_lists()

    def remove_debt(self, index: int) -> None:
        items = list(self.draft.debts)
        del items[index]
        if self._save(replace(self.draft, debts=tuple(items))):
            self._render_lists()

    def finish(self) -> None:
        try:
            self._setup.complete(self.draft)
        except DataWriteError:
            log.warning("Initial setup was not completed", exc_info=True)
            QMessageBox.warning(self, SAVE_FAILED_TITLE, SAVE_FAILED)
            return
        except BudgetError as error:
            QMessageBox.warning(self, "Помилка", error.user_message)
            return
        self.completed.emit()

    def reset(self) -> None:
        """Чернетка стає порожньою лише після збереження скинутого стану."""
        try:
            self._setup.reset()
        except BudgetError as error:
            self._report(error, SAVE_FAILED)
            return
        self.draft = SetupDraft()
        self.remainder_input.clear()
        self._show(self.draft.step)

    def _confirm_reset(self) -> None:
        answer = QMessageBox.question(
            self,
            "Почати заново",
            "Усі введені в майстрі дані буде видалено.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Reset,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Reset:
            self.reset()

    def save_on_close(self) -> bool:
        """``False`` — не збережено (показано «Помилка»): вікно закривати не можна."""
        candidate = self.draft
        amount = self._read_remainder()
        if amount is not None:
            candidate = replace(candidate, general_remainder=amount)
        return self._save(candidate, CLOSE_SAVE_FAILED)

    # Відображення ----------------------------------------------------------------------

    def _save(self, candidate: SetupDraft, failure_text: str = SAVE_FAILED) -> bool:
        """Зберігає ``candidate`` і лише після успіху робить його поточною чернеткою.
        Пошкодження бази тут не перехоплюється — воно йде до guard."""
        try:
            self._setup.save_draft(candidate)
        except BudgetError as error:
            self._report(error, failure_text)
            return False
        self.draft = candidate
        return True

    def _report(self, error: BudgetError, failure_text: str) -> None:
        if isinstance(error, DataWriteError):
            log.warning("Setup draft was not saved", exc_info=True)
            QMessageBox.warning(self, SAVE_FAILED_TITLE, failure_text)
        else:
            QMessageBox.warning(self, SAVE_FAILED_TITLE, error.user_message)

    def _show(self, step: SetupStep) -> None:
        self.steps.setCurrentIndex(step - 1)
        self.step_label.setText(f"Крок {int(step)} з 5 · {STEP_TITLES[step]}")
        self.back_button.setVisible(step > SetupStep.WELCOME)
        self.next_button.setVisible(step < SetupStep.REVIEW)
        self.finish_button.setVisible(step is SetupStep.REVIEW)
        self._render_lists()

    def _render_lists(self) -> None:
        clear_layout(self.accumulation_list)
        if not self.draft.accumulations:
            self.accumulation_list.addWidget(text_label(CAN_SKIP, "body", muted=True))
        for index, item in enumerate(self.draft.accumulations):
            target = f" · ціль {format_money(item.target)}" if item.target is not None else ""
            self.accumulation_list.addWidget(
                self._removable(
                    ListRow(item.name, f"Початковий баланс{target}", item.initial_balance),
                    lambda i=index: self.remove_accumulation(i),
                )
            )
        clear_layout(self.debt_list)
        if not self.draft.debts:
            self.debt_list.addWidget(text_label(CAN_SKIP, "body", muted=True))
        for index, item in enumerate(self.draft.debts):
            self.debt_list.addWidget(
                self._removable(
                    ListRow(item.name, "Наявний борг", item.balance),
                    lambda i=index: self.remove_debt(i),
                )
            )
        clear_layout(self.review)
        self.review.addWidget(text_label("Загальний нерозподілений залишок", "subheading"))
        self.review.addWidget(text_label(format_money(self.draft.general_remainder), "amount-lg"))
        self.review.addWidget(text_label(f"Накопичення: {len(self.draft.accumulations)}", "body"))
        for item in self.draft.accumulations:
            self.review.addWidget(ListRow(item.name, "Початковий баланс", item.initial_balance))
        self.review.addWidget(text_label(f"Борги: {len(self.draft.debts)}", "body"))
        for item in self.draft.debts:
            self.review.addWidget(ListRow(item.name, "Наявний борг", item.balance))

    @staticmethod
    def _removable(row: QWidget, on_remove) -> QWidget:
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(row, 1)
        remove = button("Прибрати", "danger-text")
        remove.clicked.connect(on_remove)
        layout.addWidget(remove)
        return holder
