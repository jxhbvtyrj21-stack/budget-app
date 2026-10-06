"""Головне вікно: бічна навігація й маршрути екранів (ADR 0016).

До завершення первинного налаштування показується лише майстер, без навігації до
фінансових екранів (ADR 0013, Q190).
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QHBoxLayout, QMainWindow, QStackedWidget, QWidget

from budget.ui.components.sidebar import Sidebar
from budget.ui.screens.placeholders import PlaceholderPage
from budget.ui.theme.tokens import WINDOW_DEFAULT_SIZE, WINDOW_MIN_SIZE

MAIN_ROUTES = [
    ("overview", "Огляд"),
    ("month", "Місяць"),
    ("accumulations", "Накопичення"),
    ("debts", "Борги"),
]
FOOTER_ROUTES = [("service", "Сервіс")]
SETUP_ROUTE = ("setup", "Первинне налаштування")


class MainWindow(QMainWindow):
    def __init__(self, title: str, setup_completed: bool) -> None:
        super().__init__()
        self.setWindowTitle(title)
        self.setMinimumSize(*WINDOW_MIN_SIZE)
        self.resize(*WINDOW_DEFAULT_SIZE)
        self.pages = QStackedWidget()
        self._routes: dict[str, PlaceholderPage] = {}
        self.sidebar: Sidebar | None = None

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        if setup_completed:
            self.sidebar = Sidebar(MAIN_ROUTES, FOOTER_ROUTES)
            self.sidebar.route_selected.connect(self.navigate)
            layout.addWidget(self.sidebar)
            for route, page_title in MAIN_ROUTES + FOOTER_ROUTES:
                self._add_route(route, page_title)
            for index, (route, _) in enumerate(MAIN_ROUTES, start=1):
                shortcut = QShortcut(QKeySequence(f"Ctrl+{index}"), self)
                shortcut.activated.connect(lambda r=route: self.navigate(r))
            self.navigate("overview")
        else:
            self._add_route(*SETUP_ROUTE)
            self.navigate(SETUP_ROUTE[0])
        layout.addWidget(self.pages, 1)
        self.setCentralWidget(central)
        # Початковий фокус — область вмісту, а не пункт навігації.
        self.pages.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.pages.setFocus()

    def _add_route(self, route: str, title: str) -> None:
        page = PlaceholderPage(title)
        self._routes[route] = page
        self.pages.addWidget(page)

    def routes(self) -> list[str]:
        return list(self._routes)

    def current_route(self) -> str:
        current = self.pages.currentWidget()
        return next(route for route, page in self._routes.items() if page is current)

    def navigate(self, route: str) -> None:
        self.pages.setCurrentWidget(self._routes[route])
        if self.sidebar is not None:
            self.sidebar.set_active(route)
