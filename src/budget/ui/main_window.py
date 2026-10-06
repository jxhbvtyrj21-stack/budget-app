"""Головне вікно: бічна навігація й маршрути екранів (ADR 0016).

До завершення первинного налаштування показується лише майстер, без навігації до
фінансових екранів (ADR 0013, Q190). Після завершення вікно перебудовується.
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import QHBoxLayout, QMainWindow, QStackedWidget, QWidget

from budget.services.facade import AppServices
from budget.ui.components.sidebar import Sidebar
from budget.ui.dialogs.expense_dialog import ExpenseDialog
from budget.ui.dialogs.income_dialog import IncomeDialog
from budget.ui.dialogs.long_gap_dialog import LongGapDialog
from budget.ui.dialogs.replenishment_dialog import ReplenishmentDialog
from budget.ui.screens.accumulations import AccumulationsPage
from budget.ui.screens.month import MonthPage
from budget.ui.screens.overview import OverviewPage
from budget.ui.screens.placeholders import PlaceholderPage
from budget.ui.screens.setup_wizard import SetupWizardPage
from budget.ui.theme.tokens import WINDOW_DEFAULT_SIZE, WINDOW_MIN_SIZE

MAIN_ROUTES = [
    ("overview", "Огляд"),
    ("month", "Місяць"),
    ("accumulations", "Накопичення"),
    ("debts", "Борги"),
]
FOOTER_ROUTES = [("service", "Сервіс")]
SETUP_ROUTE = "setup"


class MainWindow(QMainWindow):
    def __init__(self, title: str, services: AppServices) -> None:
        super().__init__()
        self._services = services
        self.setWindowTitle(title)
        self.setMinimumSize(*WINDOW_MIN_SIZE)
        self.resize(*WINDOW_DEFAULT_SIZE)
        self.sidebar: Sidebar | None = None
        self.wizard: SetupWizardPage | None = None
        self._routes: dict[str, QWidget] = {}
        self._shortcuts: list[QShortcut] = []
        if services.setup.is_completed():
            self._build_normal()
        else:
            self._build_setup()

    # Побудова --------------------------------------------------------------------------

    def _reset_central(self) -> QHBoxLayout:
        for shortcut in self._shortcuts:
            shortcut.setParent(None)
        self._shortcuts.clear()
        self._routes.clear()
        self.sidebar = None
        self.wizard = None
        self.pages = QStackedWidget()
        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.setCentralWidget(central)
        return layout

    def _build_setup(self) -> None:
        layout = self._reset_central()
        self.wizard = SetupWizardPage(self._services.setup)
        self.wizard.completed.connect(self._on_setup_completed)
        self._add_route(SETUP_ROUTE, self.wizard)
        layout.addWidget(self.pages, 1)
        self.navigate(SETUP_ROUTE)

    def _build_normal(self) -> None:
        layout = self._reset_central()
        self.sidebar = Sidebar(MAIN_ROUTES, FOOTER_ROUTES)
        self.sidebar.route_selected.connect(self.navigate)
        layout.addWidget(self.sidebar)
        self.overview = OverviewPage(self._services)
        self.overview.new_income_requested.connect(self.open_income_dialog)
        self.overview.long_gap_requested.connect(self.open_long_gap_dialog)
        self.month = MonthPage(self._services)
        self.month.new_income_requested.connect(self.open_income_dialog)
        self.month.changed.connect(self.refresh)
        self.overview.new_expense_requested.connect(self.open_expense_dialog)
        self.overview.new_replenishment_requested.connect(self.open_replenishment_dialog)
        self.accumulations = AccumulationsPage(self._services)
        self.accumulations.changed.connect(self.refresh)
        self._add_route("overview", self.overview)
        self._add_route("month", self.month)
        self._add_route("accumulations", self.accumulations)
        for route, title in MAIN_ROUTES[3:] + FOOTER_ROUTES:
            self._add_route(route, PlaceholderPage(title))
        for index, (route, _) in enumerate(MAIN_ROUTES, start=1):
            shortcut = QShortcut(QKeySequence(f"Ctrl+{index}"), self)
            shortcut.activated.connect(lambda r=route: self.navigate(r))
            self._shortcuts.append(shortcut)
        layout.addWidget(self.pages, 1)
        # Початковий фокус — область вмісту, а не пункт навігації.
        self.pages.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.pages.setFocus()
        self.navigate("overview")

    def _add_route(self, route: str, page: QWidget) -> None:
        self._routes[route] = page
        self.pages.addWidget(page)

    # Навігація й дії -------------------------------------------------------------------

    def routes(self) -> list[str]:
        return list(self._routes)

    def current_route(self) -> str:
        current = self.pages.currentWidget()
        return next(route for route, page in self._routes.items() if page is current)

    def navigate(self, route: str) -> None:
        self.pages.setCurrentWidget(self._routes[route])
        if self.sidebar is not None:
            self.sidebar.set_active(route)

    def refresh(self) -> None:
        if self.sidebar is not None:
            self.overview.refresh()
            self.month.refresh()
            self.accumulations.refresh()

    def open_income_dialog(self) -> None:
        if IncomeDialog(self._services.incomes, self).exec():
            self.refresh()

    def open_expense_dialog(self) -> None:
        if ExpenseDialog(self._services.expenses, parent=self).exec():
            self.refresh()

    def open_replenishment_dialog(self) -> None:
        if ReplenishmentDialog(self._services.replenishments, parent=self).exec():
            self.refresh()

    def open_long_gap_dialog(self) -> None:
        pending = self._services.transitions.pending_long_gap()
        if pending is None:
            return
        dialog = LongGapDialog(pending.total, self)
        if dialog.exec() and dialog.choice is not None:
            self._services.transitions.resolve_long_gap(dialog.choice)
        self.refresh()

    def _on_setup_completed(self) -> None:
        self._build_normal()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.wizard is not None:
            self.wizard.save_on_close()
        super().closeEvent(event)
