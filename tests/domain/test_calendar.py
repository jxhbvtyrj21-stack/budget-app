from datetime import UTC, datetime

import pytest

from budget.domain.calendar import CalendarMonth, FixedClock, current_month


def test_month_boundary_uses_kyiv_time():
    # 31 жовтня 22:30 UTC — уже 1 листопада в Києві (UTC+2 після переходу на зимовий час).
    clock = FixedClock(datetime(2026, 10, 31, 22, 30, tzinfo=UTC))
    assert current_month(clock) == CalendarMonth(2026, 11)


def test_month_navigation_and_text():
    assert CalendarMonth(2026, 12).next() == CalendarMonth(2027, 1)
    assert CalendarMonth(2027, 1).previous() == CalendarMonth(2026, 12)
    assert str(CalendarMonth(2026, 3)) == "2026-03"
    assert CalendarMonth.parse("2026-03") == CalendarMonth(2026, 3)


@pytest.mark.parametrize("text", ["2026-13", "2026-3", "26-03", "2026/03"])
def test_invalid_month(text):
    with pytest.raises(ValueError):
        CalendarMonth.parse(text)


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        FixedClock(datetime(2026, 1, 1))
