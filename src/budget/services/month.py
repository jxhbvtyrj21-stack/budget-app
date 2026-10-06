"""Календарний місяць і перехід між місяцями (ADR 0009, ADR 0010).

Поточний місяць визначає лише годинник сервісного шару (Europe/Kyiv). Обробка
переходу між місяцями й процедура після тривалої перерви реалізуються на наступних
етапах; окремого стану місяця не існує.
"""

from budget.domain.calendar import CalendarMonth, Clock, current_month


class MonthService:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def current_month(self) -> CalendarMonth:
        return current_month(self._clock)

    def is_current(self, month: CalendarMonth) -> bool:
        """Редагувати можна лише операції поточного місяця; минулі — лише перегляд."""
        return month == self.current_month()
