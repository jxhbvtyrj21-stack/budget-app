"""Помилка збереження в майстрі (IA 12): «Помилка», а дані майстра не втрачаються.

Справжній ``MainWindow`` → ``SetupWizardPage`` → ``InitialSetupService`` → транзакція →
SQLite. Невдача вноситься в репозиторій сховища (або це справжнє блокування бази),
тож класифікація й відкат працюють як у роботі. Кожна дія спершу зберігає змінену
копію чернетки й лише після успіху робить її поточною та оновлює вікно.
"""

import sqlite3
import sys
from datetime import UTC, datetime

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLabel, QMessageBox, QPushButton

from budget.app import RuntimeCorruptionGuard, open_application_database
from budget.domain.calendar import FixedClock
from budget.domain.money import Money
from budget.platform.paths import DataPaths
from budget.services.facade import AppServices
from budget.services.setup import InitialSetupService, SetupStep
from budget.storage.repositories import AccumulationRepository
from budget.storage.setup_repository import SetupStateRepository
from budget.ui.main_window import MainWindow
from budget.ui.screens.setup_wizard import CLOSE_SAVE_FAILED, SAVE_FAILED, SAVE_FAILED_TITLE


def with_code(cls, code: int, message: str) -> sqlite3.Error:
    error = cls(message)
    error.sqlite_errorcode = code
    return error


def ordinary() -> sqlite3.Error:
    return with_code(sqlite3.OperationalError, sqlite3.SQLITE_FULL, "database or disk is full")


@pytest.fixture
def clock():
    return FixedClock(datetime(2026, 10, 6, 9, 0, tzinfo=UTC))


@pytest.fixture
def paths(tmp_path):
    return DataPaths(tmp_path / "data")


@pytest.fixture
def connection(paths, clock):
    connection = open_application_database(paths, clock)
    yield connection
    connection.close()


@pytest.fixture
def warnings(monkeypatch):
    shown = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        staticmethod(lambda parent, title, text: shown.append((title, text))),
    )
    return shown


class Fault:
    """Метод репозиторію кидає ``error``, поки його задано; інакше — справжній запис."""

    def __init__(self, monkeypatch, owner, name) -> None:
        self.error: BaseException | None = None
        self.calls = 0
        original = getattr(owner, name)

        def method(*args, **kwargs):
            self.calls += 1
            if self.error is not None:
                raise self.error
            return original(*args, **kwargs)

        monkeypatch.setattr(owner, name, method)


@pytest.fixture
def save_fault(monkeypatch):
    return Fault(monkeypatch, SetupStateRepository, "save_draft")


@pytest.fixture
def window(qtbot, monkeypatch, connection, clock):
    widget = MainWindow("Budget", AppServices.create(connection, clock))
    widget.show()
    yield widget
    monkeypatch.undo()  # спершу прибрати невдачі, тоді закрити (закриття зберігає)
    widget.close()
    widget.deleteLater()


def shown_step(wizard) -> int:
    assert wizard.step_label.text().startswith(f"Крок {wizard.steps.currentIndex() + 1} з 5")
    return wizard.steps.currentIndex() + 1


def stored(connection) -> dict:
    return InitialSetupService(connection).state().draft or {}


def texts(layout) -> list[str]:
    found = []
    for index in range(layout.count()):
        widget = layout.itemAt(index).widget()
        if widget is not None:
            found += [label.text() for label in widget.findChildren(QLabel)]
            if isinstance(widget, QLabel):
                found.append(widget.text())
    return found


def remove_buttons(layout) -> list[QPushButton]:
    buttons = []
    for index in range(layout.count()):
        widget = layout.itemAt(index).widget()
        if widget is not None:
            buttons += [b for b in widget.findChildren(QPushButton) if b.text() == "Прибрати"]
    return buttons


def to_step(wizard, step: int) -> None:
    while shown_step(wizard) < step:
        wizard.next_button.click()


KINDS = {
    "accumulation": {
        "step": 3,
        "fill": lambda w: (w.acc_name.field.setText("Подорож"), w.acc_balance.field.setText("300")),
        "fields": lambda w: (w.acc_name.field.text(), w.acc_balance.field.text()),
        "add": lambda w: w.add_accumulation_button,
        "items": lambda w: w.draft.accumulations,
        "list": lambda w: w.accumulation_list,
        "stored": "accumulations",
    },
    "debt": {
        "step": 4,
        "fill": lambda w: (
            w.debt_name.field.setText("Позика"),
            w.debt_balance.field.setText("300"),
        ),
        "fields": lambda w: (w.debt_name.field.text(), w.debt_balance.field.text()),
        "add": lambda w: w.add_debt_button,
        "items": lambda w: w.draft.debts,
        "list": lambda w: w.debt_list,
        "stored": "debts",
    },
}


# E3-1, E3-2. «Далі» -------------------------------------------------------------------------


def test_failed_next_keeps_step_draft_and_fields(window, connection, save_fault, warnings):
    wizard = window.wizard
    to_step(wizard, 2)
    wizard.remainder_input.setText("700")
    before = wizard.draft
    save_fault.error = ordinary()
    wizard.next_button.click()
    assert warnings == [(SAVE_FAILED_TITLE, SAVE_FAILED)]
    assert shown_step(wizard) == 2 and wizard.draft.step is SetupStep.GENERAL_REMAINDER
    assert wizard.draft == before  # чернетка не змінена до успішного збереження
    assert wizard.remainder_input.text() == "700"  # поле не очищено
    assert stored(connection)["step"] == 2


def test_corrected_value_is_used_after_a_failed_next(window, connection, save_fault, warnings):
    wizard = window.wizard
    to_step(wizard, 2)
    wizard.remainder_input.setText("700")
    save_fault.error = ordinary()
    wizard.next_button.click()
    save_fault.error = None
    wizard.remainder_input.setText("900")  # користувач виправив суму
    wizard.next_button.click()
    assert shown_step(wizard) == 3  # без пропуску кроку
    assert wizard.draft.general_remainder == Money(90_000)
    to_step(wizard, 5)
    assert any("900" in t for t in texts(wizard.review))  # підсумок — нова сума
    wizard.finish_button.click()
    services = AppServices.create(connection, FixedClock(datetime(2026, 10, 6, tzinfo=UTC)))
    assert services.setup.is_completed()
    assert services.balances.general_remainder() == Money(90_000)
    assert len(warnings) == 1


def test_real_locked_database_on_next_keeps_wizard_consistent(window, paths, connection, warnings):
    """Справжнє блокування SQLite (не підміна): друге з'єднання тримає запис."""
    wizard = window.wizard
    to_step(wizard, 2)
    wizard.remainder_input.setText("700")
    locker = sqlite3.connect(paths.database, isolation_level=None)
    try:
        locker.execute("BEGIN EXCLUSIVE")
        wizard.next_button.click()  # SQLITE_BUSY після тайм-ауту з'єднання
        assert warnings == [(SAVE_FAILED_TITLE, SAVE_FAILED)]
        assert shown_step(wizard) == 2 and wizard.draft.step is SetupStep.GENERAL_REMAINDER
        assert not connection.in_transaction
    finally:
        locker.execute("ROLLBACK")
        locker.close()
    wizard.next_button.click()  # повтор після зняття блокування
    assert shown_step(wizard) == 3 and wizard.draft.general_remainder == Money(70_000)


def test_failed_back_keeps_step(window, save_fault, warnings):
    wizard = window.wizard
    to_step(wizard, 3)
    save_fault.error = ordinary()
    wizard.back_button.click()
    assert shown_step(wizard) == 3 and wizard.draft.step is SetupStep.ACCUMULATIONS
    assert warnings == [(SAVE_FAILED_TITLE, SAVE_FAILED)]


# E3-3, E3-4. «Додати» ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(KINDS))
def test_failed_add_keeps_fields_and_draft(window, connection, save_fault, warnings, kind):
    spec, wizard = KINDS[kind], window.wizard
    to_step(wizard, spec["step"])
    spec["fill"](wizard)
    filled = spec["fields"](wizard)
    before = wizard.draft
    save_fault.error = ordinary()
    spec["add"](wizard).click()
    assert warnings == [(SAVE_FAILED_TITLE, SAVE_FAILED)]
    assert spec["fields"](wizard) == filled  # поля не очищено
    assert wizard.draft == before and spec["items"](wizard) == ()
    assert remove_buttons(spec["list"](wizard)) == []  # без фальшивого запису в переліку
    assert stored(connection)[spec["stored"]] == []


@pytest.mark.parametrize("kind", list(KINDS))
def test_retried_add_adds_exactly_once(window, connection, save_fault, warnings, kind):
    spec, wizard = KINDS[kind], window.wizard
    to_step(wizard, spec["step"])
    spec["fill"](wizard)
    save_fault.error = ordinary()
    spec["add"](wizard).click()
    save_fault.error = None
    spec["add"](wizard).click()
    assert len(spec["items"](wizard)) == 1
    assert len(remove_buttons(spec["list"](wizard))) == 1
    assert spec["fields"](wizard) == ("", "")  # очищено лише після успіху
    assert len(stored(connection)[spec["stored"]]) == 1


# E3-5. «Прибрати» ------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(KINDS))
def test_failed_remove_keeps_the_item(window, connection, save_fault, warnings, kind):
    spec, wizard = KINDS[kind], window.wizard
    to_step(wizard, spec["step"])
    spec["fill"](wizard)
    spec["add"](wizard).click()
    save_fault.error = ordinary()
    remove_buttons(spec["list"](wizard))[0].click()
    assert warnings == [(SAVE_FAILED_TITLE, SAVE_FAILED)]
    assert len(spec["items"](wizard)) == 1
    assert len(remove_buttons(spec["list"](wizard))) == 1
    assert len(stored(connection)[spec["stored"]]) == 1
    save_fault.error = None
    remove_buttons(spec["list"](wizard))[0].click()  # успішне прибирання — як і раніше
    assert spec["items"](wizard) == () and remove_buttons(spec["list"](wizard)) == []
    assert stored(connection)[spec["stored"]] == []


# E3-6. «Почати заново» ------------------------------------------------------------------------


def test_failed_reset_keeps_everything(window, connection, monkeypatch, warnings):
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Reset)
    )
    wizard = window.wizard
    to_step(wizard, 2)
    wizard.remainder_input.setText("700")
    wizard.next_button.click()
    KINDS["accumulation"]["fill"](wizard)
    wizard.add_accumulation_button.click()
    before = wizard.draft
    fault = Fault(monkeypatch, SetupStateRepository, "clear_draft")
    fault.error = ordinary()
    wizard.reset_button.click()
    assert warnings == [(SAVE_FAILED_TITLE, SAVE_FAILED)]
    assert wizard.draft == before and shown_step(wizard) == 3
    assert wizard.remainder_input.text() == "700"
    assert len(remove_buttons(wizard.accumulation_list)) == 1
    assert stored(connection)["accumulations"]
    fault.error = None
    wizard.reset_button.click()  # успішне скидання — як і раніше
    assert wizard.draft.accumulations == () and shown_step(wizard) == 1
    assert InitialSetupService(connection).state().draft is None


# E3-7, E3-11. «Завершити налаштування» ---------------------------------------------------------


def count(connection, table: str) -> int:
    return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def fill_everything(wizard) -> None:
    to_step(wizard, 2)
    wizard.remainder_input.setText("700")
    wizard.next_button.click()
    KINDS["accumulation"]["fill"](wizard)
    wizard.add_accumulation_button.click()
    wizard.next_button.click()
    KINDS["debt"]["fill"](wizard)
    wizard.add_debt_button.click()
    wizard.next_button.click()
    assert shown_step(wizard) == 5


def test_failed_finish_keeps_wizard_then_retry_completes_once(
    window, connection, clock, monkeypatch, warnings
):
    wizard = window.wizard
    fill_everything(wizard)
    draft = wizard.draft
    fault = Fault(monkeypatch, AccumulationRepository, "insert")
    fault.error = ordinary()
    wizard.finish_button.click()
    # E3-7: «Помилка», майстер відкритий, звичайний режим не ввімкнено, нічого не записано.
    assert warnings == [(SAVE_FAILED_TITLE, SAVE_FAILED)]
    assert window.wizard is wizard and window.sidebar is None
    assert wizard.draft == draft and shown_step(wizard) == 5
    assert not InitialSetupService(connection).is_completed()
    assert count(connection, "accumulations") == count(connection, "debts") == 0
    assert not connection.in_transaction
    # E3-11: повтор — рівно один комплект стартового стану.
    fault.error = None
    wizard.finish_button.click()
    assert window.wizard is None and window.sidebar is not None
    assert count(connection, "accumulations") == 1 and count(connection, "debts") == 1
    services = AppServices.create(connection, clock)
    assert services.balances.general_remainder() == Money(70_000)
    assert len(warnings) == 1


# E3-8, E3-9. Закриття вікна ----------------------------------------------------------------------


def test_failed_save_on_close_keeps_the_window_open(window, connection, save_fault, warnings):
    wizard = window.wizard
    to_step(wizard, 2)
    wizard.remainder_input.setText("700")  # ще не збережено
    save_fault.error = ordinary()
    assert window.close() is False  # справжня подія закриття відхилена
    assert window.isVisible() and window.wizard is wizard
    assert warnings == [(SAVE_FAILED_TITLE, CLOSE_SAVE_FAILED)]
    assert wizard.remainder_input.text() == "700"
    assert stored(connection)["general_remainder"] == 0  # у базі — ні, у майстрі — так
    save_fault.error = None
    assert window.close() is True  # повтор після усунення причини
    assert not window.isVisible()
    assert stored(connection)["general_remainder"] == 70_000 and stored(connection)["step"] == 2


def test_successful_close_saves_and_the_next_start_resumes(qtbot, window, connection, clock):
    wizard = window.wizard
    to_step(wizard, 3)
    KINDS["accumulation"]["fill"](wizard)
    wizard.add_accumulation_button.click()
    wizard.back_button.click()
    wizard.remainder_input.setText("700")
    assert window.close() is True
    resumed = MainWindow("Budget", AppServices.create(connection, clock))
    qtbot.addWidget(resumed)
    assert resumed.wizard.draft.step is SetupStep.GENERAL_REMAINDER
    assert resumed.wizard.draft.general_remainder == Money(70_000)
    assert [a.name for a in resumed.wizard.draft.accumulations] == ["Подорож"]


# E3-10. Пошкодження бази — не «Помилка» майстра, а guard ----------------------------------------


@pytest.fixture
def previous_hook(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "excepthook", lambda *args: calls.append(args[1]))
    return calls


@pytest.mark.parametrize("action", ["next", "add", "finish"])
def test_corruption_reaches_the_guard_not_the_wizard_message(
    qtbot, window, connection, monkeypatch, warnings, previous_hook, action
):
    wizard = window.wizard
    if action == "finish":
        fill_everything(wizard)
        fault = Fault(monkeypatch, AccumulationRepository, "insert")
        button = wizard.finish_button
    else:
        to_step(wizard, 3 if action == "add" else 2)
        if action == "add":
            KINDS["accumulation"]["fill"](wizard)
        fault = Fault(monkeypatch, SetupStateRepository, "save_draft")
        button = wizard.add_accumulation_button if action == "add" else wizard.next_button
    before, step = wizard.draft, shown_step(wizard)
    error = fault.error = with_code(sqlite3.DatabaseError, sqlite3.SQLITE_CORRUPT, "malformed")
    recoveries = []
    with RuntimeCorruptionGuard(recoveries.append):
        QTimer.singleShot(0, button.click)
        qtbot.waitUntil(lambda: bool(recoveries), timeout=5000)
    assert recoveries == [error] and recoveries[0] is error  # той самий об'єкт
    assert warnings == [] and previous_hook == []
    assert wizard.draft == before and shown_step(wizard) == step
    assert window.wizard is wizard and window.sidebar is None
    fault.error = None
