"""Борги (ui-information-architecture.md, розділ 7; ADR 0018, ADR 0022).

Перелік згруповано: «Активні» й «Погашені». Картка боргу — вкладене подання того
самого екрана (рядок шляху, «Назад», ``Esc`` і ``Alt+←``): залишок, сума боргу,
погашено, опис, статус, історія. «Погасити» — лише для активного боргу; назву й
опис можна змінити завжди. Борг ніде не показується як джерело коштів.
"""

from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QStackedWidget, QVBoxLayout

from budget.domain.models import DebtOrigin, DebtStatus
from budget.services.balances import DebtView
from budget.services.facade import AppServices
from budget.ui.components.basic import Panel, amount_label, button, text_label
from budget.ui.components.forms import ListRow
from budget.ui.components.read_error import ReadBoundary
from budget.ui.components.status import debt_badge
from budget.ui.dialogs.debt_dialogs import DebtMetadataDialog, LoanReceiptDialog, RepaymentDialog
from budget.ui.formatting import format_money, format_month
from budget.ui.screens.page import Page, clear_layout
from budget.ui.theme.tokens import SPACING

INITIAL_DEBT_ROW = "Початковий борг (первинне налаштування)"
LOAN_RECEIPT_ROW = "Отримання позикових коштів"


def repayment_secondary(source_name: str) -> str:
    return f"Погашення · з: {source_name}"


class DebtRow(QFrame):
    """Рядок переліку: назва, опис, позначка, залишок і «з X»; відкриває картку."""

    def __init__(self, view: DebtView, on_open: Callable[[DebtView], None]):
        super().__init__()
        self.setObjectName("ListRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.view = view
        self._on_open = on_open
        layout = QHBoxLayout(self)
        layout.setContentsMargins(SPACING[4], SPACING[3], SPACING[4], SPACING[3])
        layout.setSpacing(SPACING[4])
        texts = QVBoxLayout()
        texts.setSpacing(2)
        texts.addWidget(text_label(view.debt.name, "body-strong"))
        if view.debt.description:
            texts.addWidget(text_label(view.debt.description, "secondary", muted=True))
        layout.addLayout(texts, 1)
        layout.addWidget(debt_badge(view.status))
        amounts = QVBoxLayout()
        amounts.setSpacing(2)
        amounts.addWidget(amount_label(view.remaining), 0, Qt.AlignmentFlag.AlignRight)
        amounts.addWidget(
            text_label(f"з {format_money(view.debt.amount)}", "secondary", muted=True),
            0,
            Qt.AlignmentFlag.AlignRight,
        )
        layout.addLayout(amounts)
        self.open_button = button("Відкрити", "text")
        self.open_button.setAccessibleName(f"Відкрити «{view.debt.name}»")
        self.open_button.clicked.connect(lambda: self._on_open(self.view))
        layout.addWidget(self.open_button)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._on_open(self.view)
        super().mouseReleaseEvent(event)


class DebtListPage(Page):
    open_requested = Signal(int)
    changed = Signal()
    service_requested = Signal()

    def __init__(self, services: AppServices) -> None:
        super().__init__("Борги")
        self._services = services
        self.new_button = button("Отримати позикові кошти", "primary")
        self.new_button.clicked.connect(self.open_new)
        self.header.addWidget(self.new_button)
        # Вміст — в окремому контейнері: під час помилки читання його не видно (IA 12).
        self.reads = ReadBoundary(self.body)
        self.reads.error_state.service_requested.connect(self.service_requested.emit)
        self.reads.content_body.addWidget(
            text_label(
                "Борги не входять до загальної доступної суми й не зменшують її.",
                "secondary",
                muted=True,
            )
        )
        self.sections = QVBoxLayout()
        self.sections.setSpacing(SPACING[6])
        self.reads.content_body.addLayout(self.sections)
        self.reads.content_body.addStretch(1)
        self._load()  # побудова сторінки — без межі читання

    def refresh(self, *, guarded: bool = True) -> None:
        self.reads.run(self._load, guarded=guarded)

    def _load(self) -> None:
        clear_layout(self.sections)
        self.group_panels: dict[DebtStatus, Panel] = {}
        self.rows: list[DebtRow] = []
        service = self._services.debts
        groups = (
            (DebtStatus.ACTIVE, "Активні", service.list_active()),
            (DebtStatus.PAID, "Погашені", service.list_paid()),
        )
        if not any(views for _, _, views in groups):
            panel = Panel()
            panel.body.addWidget(text_label("Боргів немає.", "body", muted=True))
            self.sections.addWidget(panel)
            return
        for status, title, views in groups:
            if not views:
                continue
            panel = Panel()
            panel.body.addWidget(text_label(title, "heading"))
            rows = QVBoxLayout()
            rows.setSpacing(0)
            for view in views:
                row = DebtRow(view, lambda v: self.open_requested.emit(v.debt.id))
                self.rows.append(row)
                rows.addWidget(row)
            panel.body.addLayout(rows)
            self.sections.addWidget(panel)
            self.group_panels[status] = panel

    def open_new(self) -> None:
        if LoanReceiptDialog(self._services.debts, parent=self).exec():
            self.changed.emit()


class DebtDetailPage(Page):
    back_requested = Signal()
    changed = Signal()
    service_requested = Signal()

    def __init__(self, services: AppServices) -> None:
        super().__init__("")
        self._services = services
        self.debt_id: int | None = None
        self.view: DebtView | None = None
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        crumbs = QHBoxLayout()
        self.back_button = button("‹ Назад", "text")
        self.back_button.clicked.connect(self.back_requested.emit)
        self.breadcrumb = text_label("", "caption", muted=True)
        crumbs.addWidget(self.back_button)
        crumbs.addWidget(self.breadcrumb, 1)
        self.body.insertLayout(0, crumbs)
        self.header.setStretch(0, 0)
        self.badge_row = QHBoxLayout()
        self.header.addLayout(self.badge_row)
        self.header.addStretch(1)

        # Вміст — в окремому контейнері: під час помилки читання його не видно (IA 12).
        self.reads = ReadBoundary(self.body)
        self.reads.error_state.service_requested.connect(self.service_requested.emit)
        content = self.reads.content_body
        summary = Panel()
        summary.body.addWidget(text_label("Залишок боргу", "caption", muted=True))
        self.remaining = text_label("", "display-amount")
        summary.body.addWidget(self.remaining)
        self.amounts = text_label("", "secondary", muted=True)
        summary.body.addWidget(self.amounts)
        self.description = text_label("", "body")
        summary.body.addWidget(self.description)
        content.addWidget(summary)

        actions = QHBoxLayout()
        actions.setSpacing(SPACING[3])
        self.repay_button = button("Погасити", "primary")
        self.repay_button.clicked.connect(self.open_repay)
        self.edit_button = button("Змінити назву й опис")
        self.edit_button.clicked.connect(self.open_edit)
        actions.addWidget(self.repay_button)
        actions.addWidget(self.edit_button)
        actions.addStretch(1)
        content.addLayout(actions)

        history = Panel()
        history.body.addWidget(text_label("Історія операцій", "heading"))
        self.history_rows = QVBoxLayout()
        self.history_rows.setSpacing(0)
        history.body.addLayout(self.history_rows)
        content.addWidget(history)
        content.addStretch(1)

    def keyPressEvent(self, event) -> None:
        """``Esc`` і ``Alt+←`` повертають до переліку (ui-information-architecture.md, 3)."""
        alt_left = (
            event.key() == Qt.Key.Key_Left and event.modifiers() & Qt.KeyboardModifier.AltModifier
        )
        if event.key() == Qt.Key.Key_Escape or alt_left:
            self.back_requested.emit()
            return
        super().keyPressEvent(event)

    def show_debt(self, debt_id: int) -> None:
        # Заголовок попередньої картки не лишається, якщо нову прочитати не вдалося.
        self.debt_id = debt_id
        self.view = None
        self.title_label.setText("")
        self.breadcrumb.setText("")
        clear_layout(self.badge_row)
        self.reads.run(self._load)
        self.setFocus()

    def refresh(self, *, guarded: bool = True) -> None:
        if self.debt_id is None:
            return
        self.reads.run(self._load, guarded=guarded)

    def _load(self) -> None:
        view = self.view = self._services.debts.get(self.debt_id)
        debt = view.debt
        self.title_label.setText(debt.name)
        self.breadcrumb.setText(f"Борги / {debt.name}")
        clear_layout(self.badge_row)
        self.badge_row.addWidget(debt_badge(view.status))
        self.remaining.setText(format_money(view.remaining))
        self.amounts.setText(
            f"Сума боргу {format_money(debt.amount)} · погашено {format_money(view.repaid)}"
        )
        self.description.setText(debt.description or "")
        self.description.setVisible(bool(debt.description))
        self.repay_button.setVisible(view.status is DebtStatus.ACTIVE)
        self._fill_history(view)

    def _fill_history(self, view: DebtView) -> None:
        """Отримання й погашення за місяцями; початковий борг — окремим рядком без місяця."""
        clear_layout(self.history_rows)
        debt = view.debt
        entries = [
            (
                r.repayment.month,
                r.repayment.description or "Погашення",
                repayment_secondary(r.source_name),
                r.repayment.amount,
            )
            for r in self._services.debts.history(debt.id)
        ]
        if debt.origin is DebtOrigin.INITIAL:
            self.history_rows.addWidget(ListRow(INITIAL_DEBT_ROW, "Без місяця", debt.amount))
        else:
            entries.append(
                (debt.month, LOAN_RECEIPT_ROW, "Борг · до нерозподіленого залишку", debt.amount)
            )
        # Стабільне сортування: у межах місяця спершу погашення, потім отримання.
        entries.sort(key=lambda entry: entry[0], reverse=True)
        month = None
        for entry_month, title, secondary, amount in entries:
            if entry_month != month:
                month = entry_month
                heading = text_label(format_month(month), "subheading")
                heading.setContentsMargins(0, SPACING[3], 0, SPACING[1])
                self.history_rows.addWidget(heading)
            self.history_rows.addWidget(ListRow(title, secondary, amount))

    def open_repay(self) -> None:
        dialog = RepaymentDialog(self._services.debts, debt_id=self.view.debt.id, parent=self)
        if dialog.exec():
            self.refresh()
            self.changed.emit()

    def open_edit(self) -> None:
        if DebtMetadataDialog(self._services.debts, self.view, parent=self).exec():
            self.refresh()
            self.changed.emit()


class DebtsPage(QStackedWidget):
    """Маршрут «Борги»: перелік і вкладена картка без окремого пункту навігації."""

    changed = Signal()
    service_requested = Signal()

    def __init__(self, services: AppServices) -> None:
        super().__init__()
        self.list_page = DebtListPage(services)
        self.detail_page = DebtDetailPage(services)
        self.addWidget(self.list_page)
        self.addWidget(self.detail_page)
        self.list_page.open_requested.connect(self.open_detail)
        self.list_page.changed.connect(self.changed.emit)
        self.detail_page.back_requested.connect(self.show_list)
        self.detail_page.changed.connect(self.changed.emit)
        self.list_page.service_requested.connect(self.service_requested.emit)
        self.detail_page.service_requested.connect(self.service_requested.emit)

    def open_detail(self, debt_id: int) -> None:
        self.detail_page.show_debt(debt_id)
        self.setCurrentWidget(self.detail_page)

    def show_list(self) -> None:
        self.list_page.refresh()
        self.setCurrentWidget(self.list_page)

    def showing_detail(self) -> bool:
        return self.currentWidget() is self.detail_page

    def refresh(self, *, guarded: bool = True) -> None:
        self.list_page.refresh(guarded=guarded)
        if self.showing_detail():
            self.detail_page.refresh(guarded=guarded)
