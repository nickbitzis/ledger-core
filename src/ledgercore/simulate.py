"""Synthetic marketplace activity for the demo, the UI, and reconciliation.

Four made-up organizers, nine events, six days. Every action goes through the
same templates and holds the API uses, on a manual clock. The script also
plays one story on purpose: Northbeam Live is paid out, then hit by a
chargeback and a late refund, goes negative, and is made whole from the
escrow of its next event.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ledgercore import invariants
from ledgercore.clock import ManualClock, ts
from ledgercore.errors import LedgerError
from ledgercore.ledger import Ledger

START = datetime(2026, 11, 1, tzinfo=UTC)

ORGANIZERS = [
    ("northbeam", "Northbeam Live", "USD"),
    ("copperline", "Copperline Events", "USD"),
    ("harborhall", "Harbor Hall Presents", "USD"),
    ("kestrel", "Kestrel Nights", "EUR"),
]

# (event id, organizer, name, ends on day N at 23:00)
EVENTS = [
    ("evt-nb-1", "northbeam", "Rooftop Sessions Vol. 1", 1),
    ("evt-nb-2", "northbeam", "Rooftop Sessions Vol. 2", 4),
    ("evt-nb-3", "northbeam", "Winter Warehouse Night", 9),
    ("evt-cl-1", "copperline", "Founders Breakfast", 2),
    ("evt-cl-2", "copperline", "Product Summit", 8),
    ("evt-hh-1", "harborhall", "Jazz on the Pier", 3),
    ("evt-hh-2", "harborhall", "Comedy Late Show", 10),
    ("evt-ks-1", "kestrel", "Kestrel Club Night", 2),
    ("evt-ks-2", "kestrel", "Kestrel Open Air", 9),
]

PRICES = [2500, 3500, 4900, 7500, 12000]


@dataclass
class SimResult:
    db_path: str
    orders: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    rejected: dict[str, int] = field(default_factory=dict)
    invariants_ok: bool = False
    end: str = ""


def _ends_at(day: int) -> datetime:
    return START + timedelta(days=day, hours=23)


def run_demo(db_path: str | Path, seed: int = 42, days: int = 6, orders_per_day: int = 120
             ) -> SimResult:
    rng = random.Random(seed)
    clock = ManualClock(START)
    led = Ledger(db_path, clock)
    res = SimResult(db_path=str(db_path))
    for oid, name, ccy in ORGANIZERS:
        led.add_organizer(oid, name, ccy)
    for eid, oid, name, day in EVENTS:
        led.add_event(eid, oid, name, _ends_at(day))
    ends = {eid: _ends_at(day) for eid, _, _, day in EVENTS}
    owner = {eid: oid for eid, oid, _, _ in EVENTS}

    def do(kind: str, fn: Callable[[], object]) -> object | None:
        try:
            out = fn()
            res.counts[kind] = res.counts.get(kind, 0) + 1
            return out
        except LedgerError as exc:
            res.rejected[exc.code] = res.rejected.get(exc.code, 0) + 1
            return None

    clock.set(START + timedelta(minutes=1))
    do("capital_injection", lambda: led.execute(
        "capital_injection", {"currency": "USD", "amount": 50_000_00}, idempotency_key="seed-cap"))
    do("fx_conversion", lambda: led.execute(
        "fx_conversion",
        {"from_currency": "USD", "to_currency": "EUR", "amount": 5_000_00, "rate": "0.9213"},
        idempotency_key="seed-fx"))

    sold: list[tuple[str, str]] = []  # (order_id, event_id)
    unwound: set[str] = set()
    n_order = 0
    for d in range(days):
        day0 = START + timedelta(days=d)
        actions: list[tuple[datetime, int, Callable[[], None]]] = []
        seq = 0

        def at(t: datetime, fn: Callable[[], None], actions=actions) -> None:
            nonlocal seq
            seq += 1
            actions.append((t, seq, fn))

        open_events = [e for e in ends if ends[e] > day0 + timedelta(hours=20)]
        for i in range(orders_per_day):
            n_order += 1
            oid = f"ord-{n_order:05d}"
            if i < 3:  # a few sales right before midnight, to exercise the cutoff
                t = day0 + timedelta(hours=23, minutes=56 + i, seconds=rng.randint(0, 50))
                cands = [e for e in open_events if ends[e] > t]
            else:
                t = day0 + timedelta(seconds=rng.randint(6 * 3600, 22 * 3600))
                cands = [e for e in open_events if ends[e] > t]
            if not cands:
                continue
            ev = rng.choice(cands)
            qty = rng.randint(1, 4)
            gross = rng.choice(PRICES) * qty
            org = owner[ev]
            if rng.random() < 0.1 and qty > 1:
                # Card authorized for qty, one seat turns out unavailable: partial capture.
                def auth_then_capture(oid=oid, ev=ev, org=org, gross=gross, qty=qty) -> None:
                    hold = do("card_auth", lambda: led.authorize_card(oid, org, ev, gross))
                    if hold:
                        clock.advance(minutes=2)
                        part = gross // qty * (qty - 1)
                        if do("partial_capture", lambda: led.capture_hold(hold["hold_uid"], part)):
                            sold.append((oid, ev))
                at(t, auth_then_capture)
            else:
                def buy(oid=oid, ev=ev, org=org, gross=gross) -> None:
                    if do("ticket_purchase", lambda: led.execute(
                        "ticket_purchase",
                        {"order_id": oid, "organizer": org, "event_id": ev, "gross": gross},
                        idempotency_key=f"buy-{oid}",
                    )):
                        sold.append((oid, ev))
                at(t, buy)

        # Refunds through the day, from earlier sales.
        for _ in range(max(1, orders_per_day // 30)):
            t = day0 + timedelta(seconds=rng.randint(7 * 3600, 21 * 3600))

            def refund() -> None:
                live = [o for o in sold if o[0] not in unwound]
                if not live:
                    return
                oid, _ev = rng.choice(live)
                if do("refund_before_payout", lambda: led.execute(
                        "refund_before_payout", {"order_id": oid})):
                    unwound.add(oid)
            at(t, refund)

        # Morning: settle yesterday with the processor, release finished events.
        def morning(day0=day0) -> None:
            for ccy in ("USD", "EUR"):
                clearing = led.balance(f"platform:processor_clearing:{ccy}")
                if clearing > 1000:
                    gross = clearing
                    fee = gross * 3 // 100
                    do("processor_settlement", lambda gross=gross, fee=fee, ccy=ccy: led.execute(
                        "processor_settlement",
                        {"currency": ccy, "gross": gross, "fee": fee,
                         "settlement_ref": f"stl-{day0:%Y%m%d}-{ccy}"}))
            for ev, end in ends.items():
                if end < day0 + timedelta(hours=9) and led.balance(
                        f"org:{owner[ev]}:escrow:{ev}:{led_ccy(ev)}") > 0:
                    do("escrow_release", lambda ev=ev: led.execute(
                        "escrow_release", {"organizer": owner[ev], "event_id": ev},
                        idempotency_key=f"release-{ev}"))
        at(day0 + timedelta(hours=8), morning)

        # Payouts: request at 10:00 (hold), bank confirms at 15:00 (capture).
        pending: list[str] = []

        def request_payouts(pending=pending) -> None:
            for oid, _name, ccy in ORGANIZERS:
                avail = led.organizer_balances(oid)["available"]
                if avail > 0:
                    hold = do("payout_hold", lambda oid=oid, ccy=ccy, avail=avail:
                              led.request_payout(oid, ccy, avail))
                    if hold:
                        pending.append(hold["hold_uid"])

        def confirm_payouts(pending=pending) -> None:
            for uid in pending:
                do("organizer_payout", lambda uid=uid: led.capture_hold(uid))
            pending.clear()
        at(day0 + timedelta(hours=10), request_payouts)
        at(day0 + timedelta(hours=15), confirm_payouts)

        if d == 3:
            # The story: Northbeam was paid for evt-nb-1. Now a dispute and a late refund.
            def dispute() -> None:
                nb1 = [o for o, e in sold if e == "evt-nb-1" and o not in unwound]
                if len(nb1) >= 2:
                    if do("chargeback", lambda: led.execute("chargeback", {"order_id": nb1[0]})):
                        unwound.add(nb1[0])
                    if do("refund_after_payout", lambda: led.execute(
                            "refund_after_payout", {"order_id": nb1[1]})):
                        unwound.add(nb1[1])
            at(day0 + timedelta(hours=16), dispute)

        def sweep() -> None:
            n = led.expire_holds()
            if n:
                res.counts["expired_holds"] = res.counts.get("expired_holds", 0) + n
        at(day0 + timedelta(hours=23, minutes=30), sweep)

        for t, _, fn in sorted(actions, key=lambda a: (a[0], a[1])):
            clock.set(max(t, clock.now))
            fn()

    res.orders = len(sold)
    res.end = ts(clock.now)
    res.invariants_ok = invariants.check_all(led.conn, ts(clock.now)).ok
    led.close()
    return res


def led_ccy(event_id: str) -> str:
    return "EUR" if event_id.startswith("evt-ks") else "USD"
