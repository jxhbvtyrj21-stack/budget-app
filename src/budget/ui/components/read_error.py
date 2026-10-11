"""Стан екрана «Помилка» під час читання даних (ui-information-architecture.md, 12).

``ReadBoundary`` — єдина межа читання в інтерфейсі й єдине місце, де UI перехоплює
будь-який виняток. Звичайну помилку читання (``read_failure``) записує в журнал і
показує замість вмісту екрана; усе інше, зокрема пошкодження бази, — той самий об'єкт
винятку далі (``raise``), до ``RuntimeCorruptionGuard``. Виняток не зберігається.

Межа діє лише для оновлення вже побудованого екрана. Читання під час побудови
сторінок (створення вікна, ``replace_services``, відновлення) нею не загортаються.
"""

import logging
from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QVBoxLayout, QWidget

from budget.errors import DataReadError
from budget.services.read_errors import read_failure
from budget.ui.components.basic import button
from budget.ui.components.forms import Notice

log = logging.getLogger(__name__)

READ_ERROR_TITLE = "Помилка"
READ_ERROR_TEXT = DataReadError.default_message
SERVICE_BUTTON = "Сервіс"


class ReadErrorState(QFrame):
    """«Помилка» · «Не вдалося прочитати дані.» · кнопка «Сервіс»."""

    service_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.notice = Notice(READ_ERROR_TITLE, READ_ERROR_TEXT, error=True)
        layout.addWidget(self.notice)
        self.service_button = button(SERVICE_BUTTON)
        self.service_button.clicked.connect(self.service_requested.emit)
        layout.addWidget(self.service_button, 0, Qt.AlignmentFlag.AlignLeft)


class ReadBoundary:
    """Вміст екрана в окремому контейнері й стан «Помилка» над ним."""

    def __init__(self, page_body: QVBoxLayout) -> None:
        self.error_state = ReadErrorState()
        self.error_state.hide()
        self.content = QWidget()
        self.content_body = QVBoxLayout(self.content)
        self.content_body.setContentsMargins(0, 0, 0, 0)
        self.content_body.setSpacing(page_body.spacing())
        page_body.addWidget(self.error_state)
        page_body.addWidget(self.content, 1)

    @property
    def failed(self) -> bool:
        return not self.error_state.isHidden()

    def run(self, load: Callable[[], None], *, guarded: bool = True) -> None:
        """Читання вже побудованого екрана. ``guarded=False`` — без межі (як до E2)."""
        if not guarded:
            load()
            self._show_content()
            return
        try:
            load()
        except Exception as error:
            failure = read_failure(error)
            if failure is None:
                raise
            log.error("Ordinary read failure on screen refresh", exc_info=failure)
            self._show_error()
            return
        self._show_content()

    def _show_content(self) -> None:
        self.error_state.hide()
        self.content.show()

    def _show_error(self) -> None:
        self.content.hide()
        self.error_state.show()
