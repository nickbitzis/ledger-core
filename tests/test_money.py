from decimal import Decimal

import pytest

from ledgercore.errors import ValidationError
from ledgercore.money import bps, convert, fmt, parse_rate, require_minor, require_positive
from ledgercore.templates import Posting


@pytest.mark.parametrize("bad", [1.0, 0.5, True, Decimal("1"), "100", None])
def test_non_integer_amounts_are_rejected(bad):
    with pytest.raises(ValidationError):
        require_minor(bad)


def test_posting_rejects_float():
    with pytest.raises(ValidationError):
        Posting("platform:bank:USD", 10.0)


def test_zero_and_negative_rules():
    with pytest.raises(ValidationError):
        require_minor(0)
    assert require_minor(0, allow_zero=True) == 0
    with pytest.raises(ValidationError):
        require_positive(-5)


def test_rate_must_be_decimal_string():
    with pytest.raises(ValidationError):
        parse_rate(0.92)
    with pytest.raises(ValidationError):
        parse_rate("-1")
    assert parse_rate("0.9213") == Decimal("0.9213")


def test_convert_uses_bankers_rounding_and_exponents():
    assert convert(10_000, "USD", "EUR", Decimal("0.9213")) == 9213
    assert convert(10_000, "USD", "JPY", Decimal("151.37")) == 15137
    assert convert(1, "USD", "EUR", Decimal("0.5")) == 0  # 0.5 rounds to even 0
    assert convert(3, "USD", "EUR", Decimal("0.5")) == 2  # 1.5 rounds to even 2


def test_bps_floors_and_fmt():
    assert bps(5000, 500) == 250
    assert bps(99, 500) == 4
    assert fmt(123456, "USD") == "1,234.56 USD"
    assert fmt(-5, "USD") == "-0.05 USD"
    assert fmt(1500, "JPY") == "1,500 JPY"
