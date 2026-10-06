"""Огляд (ui-information-architecture.md, розділ 4) — у межах реалізованих сервісів."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QGridLayout, QVBoxLayout

from budget.services.facade import AppServices
from budget.ui.components.basic import Panel, amount_label, button, text_label
from budget.ui.formatting import format_month
from budget.ui.screens.page import Page, clear_layout
from budget.ui.theme.tokens import SPACING


class Section(QFrame):
    """Секція з верхнім розділювачем, без панелі."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("DividedSection")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(0, SPACING[5], 0, 0)
        self.body.setSpacing(SPACING[2])


class OverviewPage(Page):
    new_income_requested = Signal()
    new_expense_requested = Signal()
    new_replenishment_requested = Signal()
    long_gap_requested = Signal()
    debts_requested = Signal()

    def __init__(self, services: AppServices) -> None:
        super().__init__("Огляд")
        self._services = services
        new_income = button("Новий дохід")
        new_income.clicked.connect(self.new_income_requested.emit)
        self.header.addWidget(new_income)
        self.new_replenishment_button = button("Поповнити накопичення")
        self.new_replenishment_button.clicked.connect(self.new_replenishment_requested.emit)
        self.header.addWidget(self.new_replenishment_button)
        new_expense = button("Нова витрата", "primary")
        new_expense.clicked.connect(self.new_expense_requested.emit)
        self.header.addWidget(new_expense)
        self.summary = Panel()
        self.body.addWidget(self.summary)
        self.composition = QGridLayout()
        self.composition.setSpacing(SPACING[5])
        self.body.addLayout(self.composition)
        self.month_label = text_label("", "heading")
        self.body.addWidget(self.month_label)
        # Зобов'язання — окрема секція після розділювача, поза загальною доступною
        # сумою (ADR 0018, п. 8; ui-information-architecture.md, 4.1).
        self.obligations = Section()
        self.body.addWidget(self.obligations)
        self.body.addStretch(1)
        self.refresh()

    def refresh(self) -> None:
        funds = self._services.balances.available_funds()
        clear_layout(self.summary.body)
        self.summary.body.addWidget(text_label("Загальна доступна сума", "subheading", muted=True))
        self.total_label = amount_label(funds.total, "display-amount")
        self.summary.body.addWidget(self.total_label)
        self.summary.body.addWidget(
            text_label(
                "Усі власні кошти в застосунку. Витрачати можна лише в межах залишку "
                "обраного джерела.",
                "secondary",
                muted=True,
            )
        )
        clear_layout(self.composition)
        current = self._services.months.current_month()
        incomes = self._services.incomes.list_for_month(current)
        active = [v for v in incomes if not v.income.archived and v.balance.is_positive]
        panels = [
            ("Доходи поточного місяця", funds.active_incomes, f"Активних доходів: {len(active)}"),
            ("Загальний нерозподілений залишок", funds.general_remainder, ""),
            ("Накопичення", funds.accumulations, ""),
        ]
        pending = self._services.transitions.pending_long_gap()
        if pending is not None:
            panels.append(("Залишки минулих місяців, що очікують рішення", pending.total, ""))
        self.panel_amounts = {}
        for column, (title, amount, note) in enumerate(panels):
            panel = Panel()
            panel.body.addWidget(text_label(title, "subheading", muted=True))
            label = amount_label(amount, "amount-lg")
            self.panel_amounts[title] = label
            panel.body.addWidget(label)
            if note:
                panel.body.addWidget(text_label(note, "secondary", muted=True))
            if pending is not None and column == len(panels) - 1:
                resolve = button("Вирішити")
                resolve.clicked.connect(self.long_gap_requested.emit)
                panel.body.addWidget(resolve)
            self.composition.addWidget(panel, 0, column)
        self.month_label.setText(f"Поточний місяць — {format_month(current)}")
        self._fill_obligations()

    def _fill_obligations(self) -> None:
        clear_layout(self.obligations.body)
        self.obligations.body.addWidget(text_label("Зобов'язання", "heading"))
        active = self._services.debts.list_active()
        if active:
            self.obligations.body.addWidget(text_label("Активні борги", "subheading", muted=True))
            self.debts_label = amount_label(self._services.debts.active_total(), "amount-lg")
            self.obligations.body.addWidget(self.debts_label)
            self.obligations.body.addWidget(
                text_label(f"Активних боргів: {len(active)}", "secondary", muted=True)
            )
        else:
            self.debts_label = None
            self.obligations.body.addWidget(
                text_label("Активних боргів немає.", "body", muted=True)
            )
        self.obligations.body.addWidget(
            text_label(
                "Борги не входять до загальної доступної суми й не зменшують її.",
                "secondary",
                muted=True,
            )
        )
        all_debts = button("Усі борги", "text")
        all_debts.clicked.connect(self.debts_requested.emit)
        self.obligations.body.addWidget(all_debts, 0, Qt.AlignmentFlag.AlignLeft)
