"""Місяць (ui-information-architecture.md, розділ 5) — поки лише доходи поточного місяця."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QVBoxLayout

from budget.services.balances import IncomeView
from budget.services.facade import AppServices
from budget.ui.components.basic import Panel, button, text_label
from budget.ui.components.forms import ListRow
from budget.ui.formatting import format_money, format_month
from budget.ui.screens.page import Page, clear_layout


def income_secondary(view: IncomeView) -> str:
    return f"Дохід · залишок {format_money(view.balance)}"


class MonthPage(Page):
    new_income_requested = Signal()

    def __init__(self, services: AppServices) -> None:
        super().__init__("")
        self._services = services
        self.current_tag = text_label("Поточний місяць", "caption")
        self.current_tag.setProperty("tone", "primary")
        self.header.addWidget(self.current_tag)
        new_income = button("Новий дохід", "primary")
        new_income.clicked.connect(self.new_income_requested.emit)
        self.header.addWidget(new_income)
        panel = Panel()
        panel.body.addWidget(text_label("Доходи", "heading"))
        self.rows = QVBoxLayout()
        self.rows.setSpacing(0)
        panel.body.addLayout(self.rows)
        self.body.addWidget(panel)
        self.body.addStretch(1)
        self.refresh()

    def refresh(self) -> None:
        month = self._services.months.current_month()
        self.title_label.setText(format_month(month))
        clear_layout(self.rows)
        views = self._services.incomes.list_for_month(month)
        if not views:
            self.rows.addWidget(text_label("У цьому місяці ще немає доходів.", "body", muted=True))
        for view in views:
            self.rows.addWidget(
                ListRow(
                    view.income.name,
                    income_secondary(view),
                    view.income.amount,
                    archived=view.income.archived,
                )
            )
