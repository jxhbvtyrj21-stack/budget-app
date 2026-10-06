"""Бічна навігація (ui-information-architecture.md, розділ 3)."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QButtonGroup, QPushButton, QVBoxLayout, QWidget

from budget.ui.theme.tokens import SIDEBAR_WIDTH, SPACING


class Sidebar(QWidget):
    route_selected = Signal(str)

    def __init__(self, main_routes: list[tuple[str, str]], footer_routes: list[tuple[str, str]]):
        super().__init__()
        self.setObjectName("Sidebar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(SIDEBAR_WIDTH)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, SPACING[5], 0, SPACING[5])
        layout.setSpacing(SPACING[1])
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: dict[str, QPushButton] = {}
        for route, title in main_routes:
            layout.addWidget(self._add(route, title))
        layout.addStretch(1)
        for route, title in footer_routes:
            layout.addWidget(self._add(route, title))

    def _add(self, route: str, title: str) -> QPushButton:
        item = QPushButton(title)
        item.setCheckable(True)
        # Фокус лише з клавіатури: клацання мишею не показує рамку фокуса.
        item.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        item.setProperty("route", route)
        item.clicked.connect(lambda: self.route_selected.emit(route))
        self._group.addButton(item)
        self._buttons[route] = item
        return item

    def set_active(self, route: str) -> None:
        self._buttons[route].setChecked(True)

    def active_route(self) -> str | None:
        checked = self._group.checkedButton()
        return checked.property("route") if checked else None
