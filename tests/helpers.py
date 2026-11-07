from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ledgercore.clock import ManualClock
from ledgercore.ledger import Ledger

T0 = datetime(2026, 11, 2, 12, tzinfo=UTC)


def seed(led: Ledger, clock: ManualClock) -> None:
    led.add_organizer("northbeam", "Northbeam Live", "USD")
    led.add_organizer("kestrel", "Kestrel Nights", "EUR")
    led.add_event("evt-1", "northbeam", "Rooftop Sessions", T0 + timedelta(days=1))
    led.add_event("evt-2", "northbeam", "Winter Night", T0 + timedelta(days=5))
    led.add_event("evt-k", "kestrel", "Club Night", T0 + timedelta(days=1))
    led.execute("capital_injection", {"currency": "USD", "amount": 1_000_000})


def buy(led: Ledger, order_id: str, gross: int = 5000, event_id: str = "evt-1",
        organizer: str = "northbeam") -> dict:
    return led.execute("ticket_purchase", {"order_id": order_id, "organizer": organizer,
                                           "event_id": event_id, "gross": gross})


def release(led: Ledger, clock: ManualClock, event_id: str = "evt-1",
            organizer: str = "northbeam") -> dict:
    if clock.now < T0 + timedelta(days=1, hours=1):
        clock.set(T0 + timedelta(days=1, hours=1))
    return led.execute("escrow_release", {"organizer": organizer, "event_id": event_id})


def by_account(entry: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in entry["postings"]:
        out[p["account"]] = out.get(p["account"], 0) + p["amount"]
    return out
