"""Форма поповнення накопичення (ui-information-architecture.md, 9; ADR 0007, ADR 0012).

Отримувач, рядки «джерело + сума» з кнопкою «Додати джерело», доступний залишок
кожного джерела й загальна сума. Джерела й суми задає лише користувач: форма нічого
не розподіляє й не підбирає. Одне джерело можна обрати лише в одному рядку. Доступні
суми надходять із сервісу; недостатній залишок — повідомлення «Обмеження» без
збереження. Для поповнення архівованого накопичення чи минулого місяця фінансові поля
заблоковані (Q189, Q191); частину з архівованого доходу не можна змінити (Q174).
"""

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from budget.domain.models import ReplenishmentPart, SourceRef
from budget.domain.money import Money
from budget.errors import BudgetError, ValidationError
from budget.services.expense import SourceOption
from budget.services.replenishment import (
    RecipientOption,
    ReplenishmentService,
    ReplenishmentView,
)
from budget.ui.components.basic import button, text_label
from budget.ui.components.forms import LabeledField, Notice, money_input
from budget.ui.formatting import format_money, parse_money_input
from budget.ui.messages import insufficient_funds_text, user_text
from budget.ui.theme.tokens import SPACING

DIALOG_WIDTH = 720
AMOUNT_WIDTH = 140
AVAILABLE_WIDTH = 150


class PartRow(QWidget):
    """Рядок «джерело + сума» з доступним залишком обраного джерела."""

    changed = Signal()
    remove_requested = Signal(object)

    def __init__(self, *, locked: bool = False) -> None:
        super().__init__()
        self.locked = locked
        self.options: list[SourceOption] = []
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACING[3])
        self.source_combo = QComboBox()
        self.source_combo.setAccessibleName("Джерело")
        self.amount = money_input()
        self.amount.setAccessibleName("Сума з джерела")
        self.amount.setFixedWidth(AMOUNT_WIDTH)
        self.available = text_label("", "secondary", muted=True)
        self.available.setWordWrap(False)
        self.available.setFixedWidth(AVAILABLE_WIDTH)
        self.remove_button = button("Прибрати", "text")
        self.remove_button.clicked.connect(lambda: self.remove_requested.emit(self))
        layout.addWidget(self.source_combo, 1)
        layout.addWidget(self.amount)
        layout.addWidget(self.available)
        layout.addWidget(self.remove_button)
        self.source_combo.currentIndexChanged.connect(lambda _index: self.changed.emit())
        self.amount.textChanged.connect(lambda _text: self.changed.emit())

    def set_options(self, options: list[SourceOption], selected: SourceRef | None) -> None:
        """Перелік джерел рядка без джерел, обраних в інших рядках."""
        self.source_combo.blockSignals(True)
        self.options = options
        self.source_combo.clear()
        for option in options:
            self.source_combo.addItem(option.name)
        index = next((i for i, o in enumerate(options) if o.source == selected), 0)
        self.source_combo.setCurrentIndex(index if options else -1)
        self.source_combo.blockSignals(False)

    def selected(self) -> SourceOption | None:
        index = self.source_combo.currentIndex()
        return self.options[index] if 0 <= index < len(self.options) else None

    def amount_value(self) -> Money | None:
        """Сума рядка; ``None`` — порожньо або некоректно (ValidationError приховано)."""
        try:
            return parse_money_input(self.amount.text())
        except ValidationError:
            return None

    def set_editable(self, editable: bool) -> None:
        self.source_combo.setEnabled(editable)
        self.amount.setEnabled(editable)
        self.remove_button.setVisible(editable)


class ReplenishmentDialog(QDialog):
    def __init__(
        self,
        service: ReplenishmentService,
        editing: ReplenishmentView | None = None,
        recipient_id: int | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self._service = service
        self._editing = editing
        title = "Зміна поповнення" if editing else "Поповнення накопичення"
        self.setWindowTitle(title)
        self.setFixedWidth(DIALOG_WIDTH)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACING[5], SPACING[5], SPACING[5], SPACING[5])
        layout.setSpacing(SPACING[4])
        layout.addWidget(text_label(title, "heading"))

        self.recipient_combo = QComboBox()
        self.recipient = LabeledField("Накопичення-отримувач", self.recipient_combo)
        layout.addWidget(self.recipient)

        layout.addWidget(text_label("Джерела", "caption", muted=True))
        self.rows_layout = QVBoxLayout()
        self.rows_layout.setSpacing(SPACING[2])
        layout.addLayout(self.rows_layout)
        self.add_button = button("Додати джерело", "text")
        self.add_button.clicked.connect(self.add_row)
        layout.addWidget(self.add_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.total = text_label("", "body-strong")
        layout.addWidget(self.total)

        self.name = LabeledField("Назва (обов'язково)", QLineEdit())
        self.description = LabeledField("Опис", QLineEdit())
        layout.addWidget(self.name)
        layout.addWidget(self.description)

        self.limit = Notice("Обмеження")
        self.limit.hide()
        layout.addWidget(self.limit)
        self.failure = Notice("Не вдалося зберегти", error=True)
        self.failure.hide()
        layout.addWidget(self.failure)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = button("Скасувати")
        cancel.clicked.connect(self.reject)
        self.save_button = button("Зберегти" if editing else "Поповнити", "primary")
        self.save_button.clicked.connect(self.save)
        buttons.addWidget(cancel)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)

        self.rows: list[PartRow] = []
        current = editing.replenishment if editing else None
        self._lock_reason = service.financial_lock_reason(current) if current else None
        self._locked_sources = service.locked_sources(current) if current else set()
        self._options = service.source_options(editing=current)
        self._recipients = service.recipient_options()
        self._fill_recipients(current, recipient_id)
        if editing is not None:
            self.name.field.setText(current.name)
            self.description.field.setText(current.description or "")
            for item, source_name in zip(current.parts, editing.source_names, strict=True):
                locked = self._lock_reason is not None or item.source in self._locked_sources
                row = self._append_row(locked=locked)
                row.initial_source = item.source
                if locked:
                    # Заблокована частина: лише показ її джерела й суми.
                    row.set_options([SourceOption(item.source, source_name, item.amount)], None)
                row.amount.setText(format_money(item.amount))
        else:
            self.add_row()
        if recipient_id is not None and editing is None:
            self.recipient_combo.setEnabled(False)
        if self._lock_reason is not None:
            self.recipient_combo.setEnabled(False)
        self._refresh_rows()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._fit_height()

    # Отримувач і рядки -----------------------------------------------------------------

    def _fill_recipients(self, current, recipient_id: int | None) -> None:
        if current is not None and all(
            r.accumulation_id != current.accumulation_id for r in self._recipients
        ):
            # Поточний отримувач в архіві — лише показ, фінансові поля заблоковані.
            self._recipients.append(
                RecipientOption(current.accumulation_id, self._editing.recipient_name, Money(0))
            )
        for option in self._recipients:
            self.recipient_combo.addItem(option.name)
        wanted = current.accumulation_id if current is not None else recipient_id
        index = next((i for i, r in enumerate(self._recipients) if r.accumulation_id == wanted), 0)
        self.recipient_combo.setCurrentIndex(index if self._recipients else -1)

    def selected_recipient(self) -> int | None:
        index = self.recipient_combo.currentIndex()
        if 0 <= index < len(self._recipients):
            return self._recipients[index].accumulation_id
        return None

    def _append_row(self, *, locked: bool = False) -> PartRow:
        row = PartRow(locked=locked)
        row.initial_source = None
        row.changed.connect(self._refresh_rows)
        row.remove_requested.connect(self.remove_row)
        row.set_editable(not locked)
        self.rows.append(row)
        self.rows_layout.addWidget(row)
        return row

    def add_row(self) -> None:
        """Новий рядок з першим ще не обраним джерелом; суму вводить користувач."""
        free = self._free_sources(None)
        if not free:
            return
        row = self._append_row()
        row.initial_source = free[0].source
        self._refresh_rows()
        row.amount.setFocus()

    def remove_row(self, row: PartRow) -> None:
        if row.locked or len(self.rows) <= 1:
            return
        self.rows.remove(row)
        row.hide()
        row.setParent(None)
        row.deleteLater()
        self._refresh_rows()

    def _taken(self, exclude: PartRow | None) -> set[SourceRef]:
        taken = set()
        for row in self.rows:
            if row is exclude:
                continue
            option = row.selected()
            source = option.source if option else row.initial_source
            if source is not None:
                taken.add(source)
        return taken

    def _free_sources(self, row: PartRow | None) -> list[SourceOption]:
        taken = self._taken(row)
        return [o for o in self._options if o.source not in taken]

    def _refresh_rows(self) -> None:
        """Оновлює переліки джерел (без повторів), доступні суми, підсумок і обмеження."""
        for row in self.rows:
            if row.locked:
                continue
            current = row.selected()
            selected = current.source if current else row.initial_source
            row.set_options(self._free_sources(row), selected)
            row.initial_source = row.selected().source if row.selected() else None
            option = row.selected()
            row.available.setText(f"доступно {format_money(option.available)}" if option else "")
            row.remove_button.setVisible(len(self.rows) > 1)
        editable = self._lock_reason is None
        self.add_button.setVisible(editable)
        self.add_button.setEnabled(editable and bool(self._free_sources(None)))
        self.validate()

    # Перевірка й збереження ------------------------------------------------------------

    def parts(self) -> list[ReplenishmentPart] | None:
        """Частини, введені користувачем, без жодних змін; ``None`` — є незаповнені."""
        parts = []
        for row in self.rows:
            option, amount = row.selected(), row.amount_value()
            if option is None or amount is None or not amount.is_positive:
                return None
            parts.append(ReplenishmentPart(option.source, amount))
        return parts

    def total_amount(self) -> Money:
        total = Money.zero()
        for row in self.rows:
            amount = row.amount_value()
            if amount is not None:
                total = total + amount
        return total

    def validate(self) -> bool:
        self.total.setText(f"Разом: {format_money(self.total_amount())}")
        if self._lock_reason is not None:
            self.limit.set_text(self._lock_reason)
            self.limit.show()
            self.save_button.setEnabled(True)
            return True
        messages = []
        if self._locked_sources:
            messages.append(
                "Частину з архівованого доходу не можна зменшити чи прибрати: "
                "ця зміна повернула б йому кошти."
            )
        ok = self.selected_recipient() is not None and self.parts() is not None
        for row in self.rows:
            option, amount = row.selected(), row.amount_value()
            if row.locked or option is None or amount is None:
                continue
            if amount > option.available:
                messages.append(insufficient_funds_text(option.name, option.available, amount))
                ok = False
        self.limit.set_text("\n".join(messages))
        self.limit.setVisible(bool(messages))
        self.save_button.setEnabled(ok)
        self._fit_height()
        return ok

    def _fit_height(self) -> None:
        """Підганяє висоту під перенесений текст повідомлень за сталої ширини.

        Мінімальний розмір макета не враховує перенесених рядків, тож без цього
        повідомлення «Обмеження» обрізалося б. Розрахунок відкладено до оновлення
        макета після зміни тексту.
        """
        # Контекст — сам діалог: після його знищення виклик скасовується.
        QTimer.singleShot(0, self, self._apply_height)

    def _apply_height(self) -> None:
        needed = self.layout().totalHeightForWidth(self.width())
        if needed > self.height():
            self.resize(self.width(), needed)

    def save(self) -> None:
        self.name.set_error(None)
        self.failure.hide()
        parts = self.parts()
        recipient = self.selected_recipient()
        if parts is None or recipient is None:
            self.failure.set_text("Оберіть отримувача й укажіть суму для кожного джерела.")
            self.failure.show()
            return
        name, description = self.name.field.text(), self.description.field.text()
        if not name.strip():
            self.name.set_error("Вкажіть назву.")
            return
        try:
            if self._editing is None:
                self._service.create(name, description, recipient, parts)
            else:
                self._service.update(
                    self._editing.replenishment.id, name, description, recipient, parts
                )
        except BudgetError as error:
            self.failure.set_text(user_text(error))
            self.failure.show()
            return
        self.accept()
