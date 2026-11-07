"""Timestamps are UTC, stored as fixed-width ISO strings so text order is time order."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from ledgercore.errors import ValidationError

FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
Clock = Callable[[], datetime]


def utcnow() -> datetime:
    return datetime.now(UTC)


def ts(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime(FORMAT)


def parse(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise ValidationError(f"bad timestamp {value!r}") from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def norm(value: str | datetime) -> str:
    """Any accepted timestamp input -> canonical stored form."""
    return ts(parse(value))


class ManualClock:
    """A clock tests and the simulator can move forward by hand."""

    def __init__(self, start: datetime) -> None:
        self.now = start if start.tzinfo else start.replace(tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> datetime:
        self.now = self.now + timedelta(**kw)
        return self.now

    def set(self, dt: datetime) -> None:
        self.now = dt if dt.tzinfo else dt.replace(tzinfo=UTC)
