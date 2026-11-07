"""Integer minor units only.

Every amount in the ledger is an `int` count of the currency's minor unit
(cents for USD, yen for JPY). Floats are rejected at every entry point, and
the SQLite schema rejects non-integer values with a CHECK on typeof().
FX rates are decimal strings parsed with `Decimal`, never floats.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from ledgercore.errors import ValidationError

# ISO 4217 minor-unit exponents for the currencies this demo supports.
CURRENCIES: dict[str, int] = {"USD": 2, "EUR": 2, "GBP": 2, "JPY": 0}

# Postgres BIGINT range. SQLite INTEGER has the same range.
MAX_ABS = 2**63 - 1


def require_minor(value: object, *, field: str = "amount", allow_zero: bool = False) -> int:
    """Return `value` if it is a plain int in range, else raise ValidationError."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(
            f"{field} must be an integer number of minor units, got {type(value).__name__}"
        )
    if abs(value) > MAX_ABS:
        raise ValidationError(f"{field} out of range")
    if value == 0 and not allow_zero:
        raise ValidationError(f"{field} must not be zero")
    return value


def require_positive(value: object, *, field: str = "amount") -> int:
    v = require_minor(value, field=field)
    if v <= 0:
        raise ValidationError(f"{field} must be positive")
    return v


def require_currency(ccy: object) -> str:
    if not isinstance(ccy, str) or ccy not in CURRENCIES:
        raise ValidationError(f"unsupported currency {ccy!r}; supported: {sorted(CURRENCIES)}")
    return ccy


def parse_rate(rate: object) -> Decimal:
    """Parse an FX rate given as a decimal string such as "0.9213"."""
    if not isinstance(rate, str):
        raise ValidationError("rate must be a decimal string, for example \"0.9213\"")
    try:
        d = Decimal(rate)
    except InvalidOperation as exc:
        raise ValidationError(f"bad rate {rate!r}") from exc
    if not d.is_finite() or d <= 0:
        raise ValidationError("rate must be a positive finite decimal")
    return d


def convert(amount: int, from_ccy: str, to_ccy: str, rate: Decimal) -> int:
    """Convert minor units with banker's rounding. 10000 USD cents at 0.9213 -> 9213 EUR cents."""
    require_positive(amount)
    major = Decimal(amount).scaleb(-CURRENCIES[from_ccy])
    out = (major * rate).scaleb(CURRENCIES[to_ccy]).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
    return int(out)


def bps(amount: int, basis_points: int) -> int:
    """Floor of amount * bps / 10000, in integers only."""
    return (amount * basis_points) // 10_000


def fmt(amount: int, ccy: str) -> str:
    """Human display: 12345 USD -> '123.45 USD'. Display only, never parsed back."""
    exp = CURRENCIES.get(ccy, 2)
    sign = "-" if amount < 0 else ""
    a = abs(amount)
    if exp == 0:
        return f"{sign}{a:,} {ccy}"
    return f"{sign}{a // 10**exp:,}.{a % 10**exp:0{exp}d} {ccy}"
