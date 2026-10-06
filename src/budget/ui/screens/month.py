"""Місяць (ui-information-architecture.md, розділ 5): доходи й звичайні витрати.

Поточний місяць — створення й дії рядків у межах правил ADR 0010–0014; минулі
місяці — лише перегляд, без кнопок створення, редагування чи видалення.
"""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QMenu, QMessageBox, QPushButton, QVBoxLayout

from budget.domain.calendar import CalendarMonth
from budget.errors import BudgetError
from budget.services.balances import IncomeView
from budget.services.expense import ExpenseView
from budget.services.facade import AppServices
from budget.ui.components.basic import Panel, button, text_label
from budget.ui.components.forms import ListRow, Notice
from budget.ui.dialogs.expense_dialog import ExpenseDialog
from budget.ui.formatting import format_money, format_month
from budget.ui.messages import user_text
from budget.ui.screens.page import Page, clear_layout


def income_secondary(view: IncomeView) -> str:
    return f"Дохід · залишок {format_money(view.balance)}"


def expense_secondary(view: ExpenseView) -> str:
    return f"Витрата · з: {view.source_name}"


class MonthPage(Page):
    new_income_requested = Signal()
    changed = Signal()

    def __init__(self, services: AppServices) -> None:
        super().__init__("")
        self._services = services
        self.month = services.months.current_month()

        self.previous_button = button("‹")
        self.previous_button.clicked.connect(lambda: self.show_month(self.month.previous()))
        self.next_button = button("›")
        self.next_button.clicked.connect(lambda: self.show_month(self.month.next()))
        self.header.insertWidget(0, self.previous_button)
        self.header.insertWidget(1, self.next_button)
        self.current_tag = text_label("Поточний місяць", "caption")
        self.current_tag.setProperty("tone", "primary")
        self.header.addWidget(self.current_tag)
        self.new_expense_button = button("Нова витрата", "primary")
        self.new_expense_button.clicked.connect(self.open_new_expense)
        self.new_income_button = button("Новий дохід")
        self.new_income_button.clicked.connect(self.new_income_requested.emit)
        self.header.addWidget(self.new_income_button)
        self.header.addWidget(self.new_expense_button)

        self.read_only_banner = Notice("Минулий місяць — лише перегляд")
        self.read_only_banner.setObjectName("InfoBanner")
        self.body.addWidget(self.read_only_banner)
        self.income_rows = self._section("Доходи")
        self.expense_rows = self._section("Витрати")
        self.body.addStretch(1)
        self.refresh()

    def _section(self, title: str) -> QVBoxLayout:
        panel = Panel()
        panel.body.addWidget(text_label(title, "heading"))
        rows = QVBoxLayout()
        rows.setSpacing(0)
        panel.body.addLayout(rows)
        self.body.addWidget(panel)
        return rows

    # Навігація -------------------------------------------------------------------------

    def first_month(self) -> CalendarMonth:
        completed = self._services.setup.state().completed_month
        return completed or self._services.months.current_month()

    def is_current(self) -> bool:
        return self._services.months.is_current(self.month)

    def show_month(self, month: CalendarMonth) -> None:
        current = self._services.months.current_month()
        self.month = max(self.first_month(), min(current, month))
        self.refresh()

    # Відображення ----------------------------------------------------------------------

    def refresh(self) -> None:
        current = self._services.months.current_month()
        if self.month > current:
            self.month = current
        editable = self.is_current()
        self.title_label.setText(format_month(self.month))
        self.current_tag.setVisible(editable)
        self.read_only_banner.setVisible(not editable)
        self.new_expense_button.setVisible(editable)
        self.new_income_button.setVisible(editable)
        self.previous_button.setEnabled(self.month > self.first_month())
        self.next_button.setEnabled(self.month < current)

        clear_layout(self.income_rows)
        incomes = self._services.incomes.list_for_month(self.month)
        if not incomes:
            self.income_rows.addWidget(
                text_label("У цьому місяці немає доходів.", "body", muted=True)
            )
        for view in incomes:
            self.income_rows.addWidget(
                ListRow(
                    view.income.name,
                    income_secondary(view),
                    view.income.amount,
                    archived=view.income.archived,
                )
            )

        clear_layout(self.expense_rows)
        expenses = self._services.expenses.list_for_month(self.month)
        if not expenses:
            self.expense_rows.addWidget(
                text_label("У цьому місяці немає витрат.", "body", muted=True)
            )
        for view in expenses:
            actions = self._row_actions(view) if editable else None
            self.expense_rows.addWidget(
                ListRow(
                    view.expense.name, expense_secondary(view), view.expense.amount, actions=actions
                )
            )

    def _row_actions(self, view: ExpenseView) -> QPushButton:
        more = button("⋯", "text")
        more.setAccessibleName(f"Дії для «{view.expense.name}»")
        menu = QMenu(more)
        menu.addAction("Редагувати", lambda: self.open_edit_expense(view))
        if self._services.expenses.financial_lock_reason(view.expense) is None:
            menu.addAction("Видалити", lambda: self.delete_expense(view))
        more.setMenu(menu)
        return more

    # Дії -------------------------------------------------------------------------------

    def open_new_expense(self) -> None:
        if ExpenseDialog(self._services.expenses, parent=self).exec():
            self._after_change()

    def open_edit_expense(self, view: ExpenseView) -> None:
        if ExpenseDialog(self._services.expenses, editing=view, parent=self).exec():
            self._after_change()

    def delete_expense(self, view: ExpenseView) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("Видалення витрати")
        box.setText(
            f"Видалити витрату «{view.expense.name}» на суму {format_money(view.expense.amount)}?"
            f" Залишок джерела «{view.source_name}» збільшиться на "
            f"{format_money(view.expense.amount)}."
        )
        cancel = box.addButton("Скасувати", QMessageBox.ButtonRole.RejectRole)
        confirm = box.addButton("Видалити витрату", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(cancel)
        box.exec()
        if box.clickedButton() is not confirm:
            return
        try:
            self._services.expenses.delete(view.expense.id)
        except BudgetError as error:
            QMessageBox.warning(self, "Видалення витрати", user_text(error))
            return
        self._after_change()

    def _after_change(self) -> None:
        self.refresh()
        self.changed.emit()
