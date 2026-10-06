"""Місяць (ui-information-architecture.md, розділ 5): доходи, витрати, поповнення й борги.

Поточний місяць — створення й дії рядків у межах правил ADR 0010–0014; минулі
місяці — лише перегляд, без кнопок створення, редагування чи видалення.
"""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QMenu, QMessageBox, QPushButton, QVBoxLayout

from budget.domain.calendar import CalendarMonth
from budget.errors import BudgetError
from budget.services.balances import DebtView, IncomeView
from budget.services.debt import RepaymentView
from budget.services.expense import ExpenseView
from budget.services.facade import AppServices
from budget.services.replenishment import ReplenishmentView
from budget.ui.components.basic import Panel, button, text_label
from budget.ui.components.forms import ListRow, Notice
from budget.ui.dialogs.debt_dialogs import LoanReceiptDialog, RepaymentDialog
from budget.ui.dialogs.expense_dialog import ExpenseDialog
from budget.ui.dialogs.replenishment_dialog import ReplenishmentDialog
from budget.ui.formatting import format_money, format_month
from budget.ui.messages import user_text
from budget.ui.screens.page import Page, clear_layout


def income_secondary(view: IncomeView) -> str:
    return f"Дохід · залишок {format_money(view.balance)}"


def expense_secondary(view: ExpenseView) -> str:
    return f"Витрата · з: {view.source_name}"


def replenishment_secondary(view: ReplenishmentView) -> str:
    """Поповнення · у: отримувач · з: джерела (design-system.md, 9)."""
    sources = ", ".join(dict.fromkeys(view.source_names))
    return f"Поповнення · у: {view.recipient_name} · з: {sources}"


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
        self.new_replenishment_button = button("Поповнити накопичення")
        self.new_replenishment_button.clicked.connect(self.open_new_replenishment)
        # Дії поточного місяця — окремим рядком під заголовком, щоб не стискати назву.
        self.actions_row = QHBoxLayout()
        self.actions_row.addStretch(1)
        for action in (
            self.new_income_button,
            self.new_replenishment_button,
            self.new_expense_button,
        ):
            self.actions_row.addWidget(action)
        self.body.insertLayout(1, self.actions_row)

        self.read_only_banner = Notice("Минулий місяць — лише перегляд")
        self.read_only_banner.setObjectName("InfoBanner")
        self.body.addWidget(self.read_only_banner)
        self.income_rows = self._section("Доходи")
        self.expense_rows = self._section("Витрати")
        self.replenishment_rows = self._section("Поповнення накопичень")
        self.debt_rows = self._section("Борги")
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
        self.new_replenishment_button.setVisible(editable)
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

        clear_layout(self.replenishment_rows)
        replenishments = self._services.replenishments.list_for_month(self.month)
        if not replenishments:
            self.replenishment_rows.addWidget(
                text_label("У цьому місяці немає поповнень накопичень.", "body", muted=True)
            )
        for view in replenishments:
            actions = self._replenishment_actions(view) if editable else None
            self.replenishment_rows.addWidget(
                ListRow(
                    view.replenishment.name,
                    replenishment_secondary(view),
                    view.total,
                    actions=actions,
                )
            )

        clear_layout(self.debt_rows)
        debts = self._services.debts
        receipts = debts.list_for_month(self.month)
        repayments = debts.repayments_for_month(self.month)
        if not receipts and not repayments:
            self.debt_rows.addWidget(
                text_label("У цьому місяці немає операцій боргів.", "body", muted=True)
            )
        for view in receipts:
            actions = self._loan_actions(view) if editable else None
            self.debt_rows.addWidget(
                ListRow(
                    view.debt.name,
                    "Отримання позикових коштів · до нерозподіленого залишку",
                    view.debt.amount,
                    actions=actions,
                )
            )
        for view in repayments:
            actions = self._repayment_actions(view) if editable else None
            self.debt_rows.addWidget(
                ListRow(
                    view.repayment.description or "Погашення",
                    f"Погашення · борг: {view.debt_name} · з: {view.source_name}",
                    view.repayment.amount,
                    actions=actions,
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

    def _replenishment_actions(self, view: ReplenishmentView) -> QPushButton:
        service = self._services.replenishments
        more = button("⋯", "text")
        more.setAccessibleName(f"Дії для «{view.replenishment.name}»")
        menu = QMenu(more)
        menu.addAction("Редагувати", lambda: self.open_edit_replenishment(view))
        locked = service.financial_lock_reason(view.replenishment) is not None
        if not locked and not service.locked_sources(view.replenishment):
            menu.addAction("Видалити", lambda: self.delete_replenishment(view))
        more.setMenu(menu)
        return more

    def open_new_replenishment(self) -> None:
        if ReplenishmentDialog(self._services.replenishments, parent=self).exec():
            self._after_change()

    def open_edit_replenishment(self, view: ReplenishmentView) -> None:
        dialog = ReplenishmentDialog(self._services.replenishments, editing=view, parent=self)
        if dialog.exec():
            self._after_change()

    def delete_replenishment(self, view: ReplenishmentView) -> None:
        total = format_money(view.total)
        sources = ", ".join(dict.fromkeys(view.source_names))
        box = QMessageBox(self)
        box.setWindowTitle("Видалення поповнення")
        box.setText(
            f"Видалити поповнення «{view.replenishment.name}» на суму {total}? "
            f"Кошти повернуться джерелам ({sources}), а залишок накопичення "
            f"«{view.recipient_name}» зменшиться на {total}."
        )
        cancel = box.addButton("Скасувати", QMessageBox.ButtonRole.RejectRole)
        confirm = box.addButton("Видалити поповнення", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(cancel)
        box.exec()
        if box.clickedButton() is not confirm:
            return
        try:
            self._services.replenishments.delete(view.replenishment.id)
        except BudgetError as error:
            QMessageBox.warning(self, "Видалення поповнення", user_text(error))
            return
        self._after_change()

    def _loan_actions(self, view: DebtView) -> QPushButton | None:
        """Отримання: змінити суму; видалити — лише без погашень (ADR 0018, п. 5)."""
        service = self._services.debts
        if service.loan_lock_reason(view.debt) is not None:
            return None
        more = button("⋯", "text")
        more.setAccessibleName(f"Дії для «{view.debt.name}»")
        menu = QMenu(more)
        menu.addAction("Змінити суму", lambda: self.open_edit_loan(view))
        if not view.repaid.is_positive:
            menu.addAction("Видалити", lambda: self.delete_loan(view))
        more.setMenu(menu)
        return more

    def _repayment_actions(self, view: RepaymentView) -> QPushButton:
        service = self._services.debts
        more = button("⋯", "text")
        more.setAccessibleName(f"Дії для погашення боргу «{view.debt_name}»")
        menu = QMenu(more)
        menu.addAction("Редагувати", lambda: self.open_edit_repayment(view))
        if service.repayment_delete_block_reason(view.repayment) is None:
            menu.addAction("Видалити", lambda: self.delete_repayment(view))
        more.setMenu(menu)
        return more

    def open_edit_loan(self, view: DebtView) -> None:
        if LoanReceiptDialog(self._services.debts, editing=view, parent=self).exec():
            self._after_change()

    def open_edit_repayment(self, view: RepaymentView) -> None:
        if RepaymentDialog(self._services.debts, editing=view, parent=self).exec():
            self._after_change()

    def delete_loan(self, view: DebtView) -> None:
        amount = format_money(view.debt.amount)
        if not self._confirm(
            "Видалення отримання",
            f"Видалити отримання позикових коштів «{view.debt.name}» на суму {amount} разом "
            f"із боргом? Загальний нерозподілений залишок зменшиться на {amount}.",
            "Видалити отримання",
        ):
            return
        self._run_delete(
            "Видалення отримання", lambda: self._services.debts.delete_loan(view.debt.id)
        )

    def delete_repayment(self, view: RepaymentView) -> None:
        amount = format_money(view.repayment.amount)
        if not self._confirm(
            "Видалення погашення",
            f"Видалити погашення боргу «{view.debt_name}» на суму {amount}? Залишок джерела "
            f"«{view.source_name}» і залишок боргу збільшаться на {amount}.",
            "Видалити погашення",
        ):
            return
        self._run_delete(
            "Видалення погашення",
            lambda: self._services.debts.delete_repayment(view.repayment.id),
        )

    def _confirm(self, title: str, text: str, confirm_text: str) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setText(text)
        cancel = box.addButton("Скасувати", QMessageBox.ButtonRole.RejectRole)
        confirm = box.addButton(confirm_text, QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is confirm

    def _run_delete(self, title: str, action) -> None:
        try:
            action()
        except BudgetError as error:
            QMessageBox.warning(self, title, user_text(error))
            return
        self._after_change()

    def _after_change(self) -> None:
        self.refresh()
        self.changed.emit()
