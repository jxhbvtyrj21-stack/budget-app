"""Первинне налаштування (ADR 0010–0013, ADR 0018, ADR 0022).

До першого успішного завершення майстра нормальна фінансова робота недоступна
(Q190), а чернетка майстра не є фінансовим записом (Q176). Завершення майстра
(створення стартового стану) реалізується на наступному етапі.
"""

import sqlite3
from typing import Any

from budget.domain.models import SetupStatus
from budget.errors import DomainRuleError
from budget.storage.setup_repository import SetupState, SetupStateRepository
from budget.storage.transaction import transaction


class InitialSetupService:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._repository = SetupStateRepository(connection)

    def state(self) -> SetupState:
        return self._repository.get()

    def is_completed(self) -> bool:
        return self.state().status is SetupStatus.COMPLETED

    def save_draft(self, draft: dict[str, Any]) -> None:
        """Зберігає незавершений стан майстра без строку дії (Q176, Q178)."""
        self._ensure_not_completed()
        with transaction(self._connection):
            self._repository.save_draft(draft)

    def reset(self) -> None:
        """Повністю скидає незавершений майстер (Q179); фінансових записів не створює."""
        self._ensure_not_completed()
        with transaction(self._connection):
            self._repository.clear_draft()

    def _ensure_not_completed(self) -> None:
        if self.is_completed():
            raise DomainRuleError("Первинне налаштування вже завершено.")


def require_normal_operation(connection: sqlite3.Connection) -> None:
    """Блокує фінансові операції до завершення первинного налаштування (Q190)."""
    if not InitialSetupService(connection).is_completed():
        raise DomainRuleError("Спершу завершіть первинне налаштування.")
