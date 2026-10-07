"""Огляд (ui-information-architecture.md, розділ 4) — у межах реалізованих сервісів."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QGridLayout, QVBoxLayout

from budget.domain.money import Money
from budget.services.facade import AppServices
from budget.ui.components.basic import Panel, amount_label, button, text_label
from budget.ui.dialogs.base_minimum_dialog import BaseMinimumDialog
from budget.ui.formatting import format_money, format_month
from budget.ui.messages import comparison_text
from budget.ui.screens.page import Page, clear_layout
from budget.ui.theme.tokens import SPACING

# Порожній стан (IA 12): після налаштування немає жодного фінансового запису. Кнопка
# «Новий дохід» уже є в заголовку екрана, тому окремо не дублюється.
START_WITH_INCOME = "Почніть із нового доходу."


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
    month_requested = Signal()
    changed = Signal()

    def __init__(self, services: AppServices) -> None:
        super().__init__("Огляд")
        self._services = services
        self.new_income_button = button("Новий дохід")
        self.new_income_button.clicked.connect(self.new_income_requested.emit)
        self.header.addWidget(self.new_income_button)
        self.new_replenishment_button = button("Поповнити накопичення")
        self.new_replenishment_button.clicked.connect(self.new_replenishment_requested.emit)
        self.header.addWidget(self.new_replenishment_button)
        new_expense = button("Нова витрата", "primary")
        new_expense.clicked.connect(self.new_expense_requested.emit)
        self.header.addWidget(new_expense)
        self.summary = Panel()
        self.body.addWidget(self.summary)
        self.empty_state = text_label(START_WITH_INCOME, "body", muted=True)
        self.body.addWidget(self.empty_state)
        self.composition = QGridLayout()
        self.composition.setSpacing(SPACING[5])
        self.body.addLayout(self.composition)
        self.month_label = text_label("", "heading")
        self.body.addWidget(self.month_label)
        # Поточний місяць — компактно: підсумки, базовий мінімум і перехід до Місяця.
        self.current_month = QVBoxLayout()
        self.current_month.setSpacing(SPACING[2])
        self.body.addLayout(self.current_month)
        # Зобов'язання — окрема секція після розділювача, поза загальною доступною
        # сумою (ADR 0018, п. 8; ui-information-architecture.md, 4.1).
        self.obligations = Section()
        self.body.addWidget(self.obligations)
        self.body.addStretch(1)
        self.refresh()

    def refresh(self) -> None:
        funds = self._services.balances.available_funds()
        self.empty_state.setVisible(not self._services.analysis.has_financial_records())
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
        archived = self._services.accumulations.list_archived()
        archived_note = (
            f"з них в архіві: {format_money(sum((v.balance for v in archived), Money.zero()))}"
            if archived
            else ""
        )
        panels = [
            ("Доходи поточного місяця", funds.active_incomes, f"Активних доходів: {len(active)}"),
            ("Загальний нерозподілений залишок", funds.general_remainder, ""),
            ("Накопичення", funds.accumulations, archived_note),
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
        self._fill_current_month(current)
        self._fill_obligations()

    def _fill_current_month(self, month) -> None:
        """Доходи й фактичні витрати місяця, базовий мінімум і нейтральне порівняння."""
        layout = self.current_month
        clear_layout(layout)
        analysis = self._services.analysis.analyse(month)
        figures = QGridLayout()
        figures.setHorizontalSpacing(SPACING[6])
        self.month_amounts = {}
        for column, (title, amount) in enumerate(
            (("Доходи місяця", analysis.incomes), ("Фактичні витрати", analysis.actual_expenses))
        ):
            figures.addWidget(text_label(title, "caption", muted=True), 0, column)
            self.month_amounts[title] = amount_label(amount)
            figures.addWidget(self.month_amounts[title], 1, column)
        figures.addWidget(text_label("Базовий мінімум", "caption", muted=True), 0, 2)
        if analysis.base_minimum is None:
            self.base_minimum_label = text_label("не задано", "body", muted=True)
        else:
            self.base_minimum_label = amount_label(analysis.base_minimum)
        figures.addWidget(self.base_minimum_label, 1, 2)
        title = "Задати" if analysis.base_minimum is None else "Змінити"
        self.base_minimum_button = button(title, "text")
        self.base_minimum_button.clicked.connect(lambda: self.open_base_minimum(month))
        figures.addWidget(self.base_minimum_button, 2, 2, Qt.AlignmentFlag.AlignLeft)
        layout.addLayout(figures)
        self.comparison_label = text_label(
            comparison_text(analysis.comparison) if analysis.comparison else "",
            "secondary",
            muted=True,
        )
        self.comparison_label.setVisible(analysis.comparison is not None)
        layout.addWidget(self.comparison_label)
        layout.addWidget(
            text_label(
                f"Поповнення накопичень {format_money(analysis.replenishments)} · погашення "
                f"боргів {format_money(analysis.debt_repayments)} · отримані позикові кошти "
                f"{format_money(analysis.loan_receipts)}",
                "secondary",
                muted=True,
            )
        )
        open_month = button("Відкрити місяць", "text")
        open_month.clicked.connect(self.month_requested.emit)
        layout.addWidget(open_month, 0, Qt.AlignmentFlag.AlignLeft)

    def open_base_minimum(self, month) -> None:
        if BaseMinimumDialog(self._services.base_minimums, month, parent=self).exec():
            self.changed.emit()

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
