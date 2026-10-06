import pytest

from budget.domain.money import MAX_KOPIYKY, Money, parse_money, require_positive
from budget.errors import ValidationError


@pytest.mark.parametrize(
    ("text", "kopiyky"),
    [
        ("5000", 500_000),
        ("5 000", 500_000),
        ("5 000", 500_000),
        ("5 000,5", 500_050),
        ("5000,50", 500_050),
        ("5000.05", 500_005),
        (" 0,01 ", 1),
        ("0", 0),
    ],
)
def test_parse_valid(text, kopiyky):
    assert parse_money(text) == Money(kopiyky)


@pytest.mark.parametrize("text", ["", "-5", "5,123", "abc", "5,", "1e3", "+5"])
def test_parse_invalid(text):
    with pytest.raises(ValidationError):
        parse_money(text)


def test_parse_too_large():
    with pytest.raises(ValidationError):
        parse_money(str(MAX_KOPIYKY))


def test_money_requires_int():
    with pytest.raises(TypeError):
        Money(1.5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Money(True)


def test_arithmetic_and_comparison():
    a, b = Money(500), Money(200)
    assert a + b == Money(700)
    assert b - a == Money(-300)
    assert (b - a).is_negative and a > b and Money.zero().is_zero


def test_split():
    assert Money(500_050).split() == (False, 5000, 50)
    assert Money(-1).split() == (True, 0, 1)


def test_require_positive():
    with pytest.raises(ValidationError):
        require_positive(Money.zero())
    assert require_positive(Money(1)) == Money(1)
