"""Накопичення (ui-information-architecture.md, розділ 6).

Перелік має два подання — «Робочий перелік» (групи «Активні», «Досягнуті»,
«Закриті») і «Архів». Картка накопичення — вкладене подання того самого екрана:
рядок шляху, «Назад», ``Esc`` і ``Alt+←`` повертають до переліку. Дії показуються
лише ті, що дозволені правилами ADR 0007, ADR 0013 і ADR 0022; закриття за
ненульового залишку вимкнене з поясненням. Поповнення — наступний етап.
"""

from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from budget.domain.models import AccumulationStatus
from budget.errors import BudgetError
from budget.services.accumulation import ARCHIVED_NOTICE, AccumulationView, status_label
from budget.services.facade import AppServices
from budget.ui.components.basic import Panel, amount_label, button, text_label
from budget.ui.components.forms import ListRow, Notice
from budget.ui.components.status import ProgressView, archive_marker, status_badge
from budget.ui.dialogs.accumulation_dialog import AccumulationDialog
from budget.ui.formatting import format_money, format_month
from budget.ui.messages import close_blocked_text, user_text
from budget.ui.screens.page import Page, clear_layout
from budget.ui.theme.tokens import SPACING

GROUPS = (
    (AccumulationStatus.ACTIVE, "Активні"),
    (AccumulationStatus.REACHED, "Досягнуті"),
    (AccumulationStatus.CLOSED, "Закриті"),
)
ARCHIVE_EXPLANATION = (
    "Залишки архівованих накопичень входять до загальної доступної суми, "
    "але не можуть бути джерелом нових операцій."
)
TRANSITION_TEXT = {
    AccumulationStatus.ACTIVE: "Зробити активним",
    AccumulationStatus.REACHED: "Позначити досягнутим",
    AccumulationStatus.CLOSED: "Закрити",
}
INITIAL_BALANCE_ROW = "Початковий баланс (первинне налаштування)"


def badges(view: AccumulationView) -> QWidget:
    """Позначка статусу й, для архівованого, окрема позначка «В архіві»."""
    container = QWidget()
    row = QHBoxLayout(container)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(SPACING[2])
    row.setAlignment(Qt.AlignmentFlag.AlignVCenter)
    row.addWidget(status_badge(view.accumulation.status))
    if view.accumulation.archived:
        row.addWidget(archive_marker())
    container.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    return container


class AccumulationRow(QFrame):
    """Рядок переліку: назва, опис, позначки, прогрес, залишок; відкриває картку."""

    def __init__(self, view: AccumulationView, on_open: Callable[[AccumulationView], None]):
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
        texts.addWidget(text_label(view.accumulation.name, "body-strong"))
        if view.accumulation.description:
            texts.addWidget(text_label(view.accumulation.description, "secondary", muted=True))
        if view.progress is not None:
            texts.addWidget(ProgressView(view.progress))
        layout.addLayout(texts, 1)
        layout.addWidget(badges(view))
        layout.addWidget(amount_label(view.balance))
        self.open_button = button("Відкрити", "text")
        self.open_button.setAccessibleName(f"Відкрити «{view.accumulation.name}»")
        self.open_button.clicked.connect(lambda: self._on_open(self.view))
        layout.addWidget(self.open_button)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._on_open(self.view)
        super().mouseReleaseEvent(event)


class AccumulationListPage(Page):
    open_requested = Signal(int)
    changed = Signal()

    def __init__(self, services: AppServices) -> None:
        super().__init__("Накопичення")
        self._services = services
        self.new_button = button("Нове накопичення", "primary")
        self.new_button.clicked.connect(self.open_new)
        self.header.addWidget(self.new_button)

        switch = QHBoxLayout()
        switch.setSpacing(0)
        self.working_button = button("Робочий перелік")
        self.archive_button = button("Архів")
        self._views = QButtonGroup(self)
        self._views.setExclusive(True)
        for item in (self.working_button, self.archive_button):
            item.setCheckable(True)
            item.setProperty("segment", True)
            self._views.addButton(item)
            switch.addWidget(item)
            item.clicked.connect(self.refresh)
        switch.addStretch(1)
        self.working_button.setChecked(True)
        self.body.addLayout(switch)

        self.sections = QVBoxLayout()
        self.sections.setSpacing(SPACING[6])
        self.body.addLayout(self.sections)
        self.body.addStretch(1)
        self.refresh()

    def showing_archive(self) -> bool:
        return self.archive_button.isChecked()

    def show_archive(self, archive: bool) -> None:
        (self.archive_button if archive else self.working_button).setChecked(True)
        self.refresh()

    def refresh(self) -> None:
        clear_layout(self.sections)
        self.group_panels: dict[AccumulationStatus, Panel] = {}
        self.rows: list[AccumulationRow] = []
        if self.showing_archive():
            self._fill_archive()
        else:
            self._fill_working()

    def _fill_working(self) -> None:
        views = self._services.accumulations.list_working()
        if not views:
            self._empty("Накопичень ще немає.")
            return
        for status, title in GROUPS:
            members = [v for v in views if v.accumulation.status is status]
            if members:
                self.group_panels[status] = self._panel(title, members)

    def _fill_archive(self) -> None:
        self.sections.addWidget(text_label(ARCHIVE_EXPLANATION, "secondary", muted=True))
        views = self._services.accumulations.list_archived()
        if not views:
            self._empty("В архіві нічого немає.")
            return
        self._panel("Архів", views)

    def _panel(self, title: str, views: list[AccumulationView]) -> Panel:
        panel = Panel()
        panel.body.addWidget(text_label(title, "heading"))
        rows = QVBoxLayout()
        rows.setSpacing(0)
        for view in views:
            row = AccumulationRow(view, lambda v: self.open_requested.emit(v.accumulation.id))
            self.rows.append(row)
            rows.addWidget(row)
        panel.body.addLayout(rows)
        self.sections.addWidget(panel)
        return panel

    def _empty(self, text: str) -> None:
        panel = Panel()
        panel.body.addWidget(text_label(text, "body", muted=True))
        self.sections.addWidget(panel)

    def open_new(self) -> None:
        if AccumulationDialog(self._services.accumulations, parent=self).exec():
            self.show_archive(False)
            self.changed.emit()


class AccumulationDetailPage(Page):
    """Картка накопичення: залишок, ціль і прогрес, опис, позначки, дозволені дії."""

    back_requested = Signal()
    changed = Signal()

    def __init__(self, services: AppServices) -> None:
        super().__init__("")
        self._services = services
        self.view: AccumulationView | None = None

        crumbs = QHBoxLayout()
        self.back_button = button("‹ Назад", "text")
        self.back_button.clicked.connect(self.back_requested.emit)
        self.breadcrumb = text_label("", "caption", muted=True)
        crumbs.addWidget(self.back_button)
        crumbs.addWidget(self.breadcrumb, 1)
        self.body.insertLayout(0, crumbs)
        # Позначки — одразу після назви, а не на іншому краї заголовка.
        self.header.setStretch(0, 0)
        self.badge_row = QHBoxLayout()
        self.header.addLayout(self.badge_row)
        self.header.addStretch(1)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        summary = Panel()
        summary.body.addWidget(text_label("Залишок", "caption", muted=True))
        self.balance = text_label("", "display-amount")
        summary.body.addWidget(self.balance)
        self.target_area = QVBoxLayout()
        summary.body.addLayout(self.target_area)
        self.description = text_label("", "body")
        summary.body.addWidget(self.description)
        self.body.addWidget(summary)

        self.archived_notice = Notice("Обмеження", ARCHIVED_NOTICE)
        self.body.addWidget(self.archived_notice)

        actions = QHBoxLayout()
        actions.setSpacing(SPACING[3])
        self.edit_button = button("Змінити назву, опис і цільову суму")
        self.edit_button.clicked.connect(self.open_edit)
        self.status_button = button("Змінити статус")
        self.status_menu = QMenu(self.status_button)
        self.status_button.setMenu(self.status_menu)
        self.archive_button = button("Архівувати")
        self.archive_button.clicked.connect(self.archive)
        self.unarchive_button = button("Розархівувати")
        self.unarchive_button.clicked.connect(self.unarchive)
        for widget in (
            self.status_button,
            self.archive_button,
            self.unarchive_button,
            self.edit_button,
        ):
            actions.addWidget(widget)
        actions.addStretch(1)
        self.body.addLayout(actions)
        self.close_hint = text_label("", "secondary", muted=True)
        self.body.addWidget(self.close_hint)

        history = Panel()
        history.body.addWidget(text_label("Історія операцій", "heading"))
        self.history_rows = QVBoxLayout()
        self.history_rows.setSpacing(0)
        history.body.addLayout(self.history_rows)
        self.body.addWidget(history)
        self.body.addStretch(1)

    def keyPressEvent(self, event) -> None:
        """``Esc`` і ``Alt+←`` повертають до переліку (ui-information-architecture.md, 3)."""
        alt_left = (
            event.key() == Qt.Key.Key_Left and event.modifiers() & Qt.KeyboardModifier.AltModifier
        )
        if event.key() == Qt.Key.Key_Escape or alt_left:
            self.back_requested.emit()
            return
        super().keyPressEvent(event)

    # Відображення ----------------------------------------------------------------------

    def show_accumulation(self, accumulation_id: int) -> None:
        self.view = self._services.accumulations.get(accumulation_id)
        self.refresh()
        # Фокус — на картці, щоб Esc і Alt+← повертали до переліку.
        self.setFocus()

    def refresh(self) -> None:
        if self.view is None:
            return
        service = self._services.accumulations
        view = self.view = service.get(self.view.accumulation.id)
        accumulation = view.accumulation
        self.title_label.setText(accumulation.name)
        self.breadcrumb.setText(f"Накопичення / {accumulation.name}")
        clear_layout(self.badge_row)
        self.badge_row.addWidget(badges(view))
        self.balance.setText(format_money(view.balance))
        clear_layout(self.target_area)
        if view.progress is not None:
            self.target_area.addWidget(ProgressView(view.progress))
        else:
            self.target_area.addWidget(
                text_label("Цільову суму не задано.", "secondary", muted=True)
            )
        self.description.setText(accumulation.description or "")
        self.description.setVisible(bool(accumulation.description))

        archived = accumulation.archived
        self.archived_notice.setVisible(archived)
        transitions = service.status_transitions(view)
        self.status_button.setVisible(bool(transitions))
        self.status_menu.clear()
        blocker = service.close_blocker(view)
        for status in transitions:
            if status is AccumulationStatus.CLOSED and blocker is not None:
                action = self.status_menu.addAction("Закрити — лише при залишку 0")
                action.setEnabled(False)
            else:
                self.status_menu.addAction(
                    TRANSITION_TEXT[status], lambda s=status: self.change_status(s)
                )
        closable = AccumulationStatus.CLOSED in transitions
        self.close_hint.setText(close_blocked_text(view.balance) if blocker else "")
        self.close_hint.setVisible(closable and blocker is not None)
        is_closed = accumulation.status is AccumulationStatus.CLOSED
        self.archive_button.setVisible(is_closed and not archived)
        self.unarchive_button.setVisible(archived)
        self._fill_history(view)

    def _fill_history(self, view: AccumulationView) -> None:
        clear_layout(self.history_rows)
        initial = view.accumulation.initial_balance
        if initial.is_positive:
            self.history_rows.addWidget(ListRow(INITIAL_BALANCE_ROW, "Без місяця", initial))
        expenses = self._services.accumulations.expense_history(view.accumulation.id)
        month = None
        for item in expenses:
            if item.expense.month != month:
                month = item.expense.month
                heading = text_label(format_month(month), "subheading")
                heading.setContentsMargins(0, SPACING[3], 0, SPACING[1])
                self.history_rows.addWidget(heading)
            self.history_rows.addWidget(
                ListRow(item.expense.name, "Витрата з накопичення", item.expense.amount)
            )
        if not expenses and not initial.is_positive:
            self.history_rows.addWidget(text_label("Операцій ще немає.", "body", muted=True))

    # Дії -------------------------------------------------------------------------------

    def _run(self, title: str, action: Callable[[], object]) -> None:
        try:
            action()
        except BudgetError as error:
            QMessageBox.warning(self, title, user_text(error))
        self.refresh()
        self.changed.emit()

    def change_status(self, status: AccumulationStatus) -> None:
        accumulation_id = self.view.accumulation.id
        self._run(
            f"Статус «{status_label(status)}»",
            lambda: self._services.accumulations.change_status(accumulation_id, status),
        )

    def archive(self) -> None:
        accumulation_id = self.view.accumulation.id
        self._run("Архівування", lambda: self._services.accumulations.archive(accumulation_id))

    def unarchive(self) -> None:
        accumulation_id = self.view.accumulation.id
        self._run("Розархівування", lambda: self._services.accumulations.unarchive(accumulation_id))

    def open_edit(self) -> None:
        dialog = AccumulationDialog(self._services.accumulations, editing=self.view, parent=self)
        if dialog.exec():
            self.refresh()
            self.changed.emit()


class AccumulationsPage(QStackedWidget):
    """Маршрут «Накопичення»: перелік і вкладена картка без окремого пункту навігації."""

    changed = Signal()

    def __init__(self, services: AppServices) -> None:
        super().__init__()
        self.list_page = AccumulationListPage(services)
        self.detail_page = AccumulationDetailPage(services)
        self.addWidget(self.list_page)
        self.addWidget(self.detail_page)
        self.list_page.open_requested.connect(self.open_detail)
        self.list_page.changed.connect(self.changed.emit)
        self.detail_page.back_requested.connect(self.show_list)
        self.detail_page.changed.connect(self.changed.emit)

    def open_detail(self, accumulation_id: int) -> None:
        self.detail_page.show_accumulation(accumulation_id)
        self.setCurrentWidget(self.detail_page)

    def show_list(self) -> None:
        self.list_page.refresh()
        self.setCurrentWidget(self.list_page)

    def showing_detail(self) -> bool:
        return self.currentWidget() is self.detail_page

    def refresh(self) -> None:
        self.list_page.refresh()
        if self.showing_detail():
            self.detail_page.refresh()
