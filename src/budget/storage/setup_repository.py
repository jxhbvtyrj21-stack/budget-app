"""Зберігання стану первинного налаштування (ADR 0010–0013).

Репозиторій лише читає й записує рядок ``setup_state``; транзакцію відкриває сервіс.
"""

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from budget.domain.calendar import CalendarMonth
from budget.domain.models import SetupStatus


@dataclass(frozen=True, slots=True)
class SetupState:
    status: SetupStatus
    draft: dict[str, Any] | None
    completed_month: CalendarMonth | None


class SetupStateRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def get(self) -> SetupState:
        status, draft_json, completed = self._connection.execute(
            "SELECT status, draft_json, completed_month FROM setup_state WHERE id = 1"
        ).fetchone()
        return SetupState(
            status=SetupStatus(status),
            draft=json.loads(draft_json) if draft_json is not None else None,
            completed_month=CalendarMonth.parse(completed) if completed is not None else None,
        )

    def save_draft(self, draft: dict[str, Any]) -> None:
        self._connection.execute(
            "UPDATE setup_state SET status = 'in_progress', draft_json = ? WHERE id = 1",
            (json.dumps(draft, ensure_ascii=False),),
        )

    def clear_draft(self) -> None:
        self._connection.execute(
            "UPDATE setup_state SET status = 'not_started', draft_json = NULL WHERE id = 1"
        )
