"""Concurrency stress test: many threads moving money at once, then invariants.

Each thread has its own connection. Threads buy tickets, refund, charge back,
release escrow, and pay out, all against the same few organizers so they
fight over the same balances. Requests are retried at random with the same
Idempotency-Key, and some finished requests are sent again on purpose. At the
end every invariant must hold, and no idempotent request may have posted twice.
"""

from __future__ import annotations

import random
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ledgercore import invariants
from ledgercore.clock import ManualClock, ts
from ledgercore.errors import ConflictError, LedgerError
from ledgercore.ledger import Ledger

T0 = datetime(2026, 11, 10, 12, tzinfo=UTC)
ORGS = ["northbeam", "copperline", "harborhall"]


@dataclass
class StressResult:
    threads: int
    ops_per_thread: int
    outcomes: Counter = field(default_factory=Counter)
    conflicts_seen: int = 0
    duplicate_sends: int = 0
    purchases_ok: int = 0
    purchase_entries: int = 0
    invariants: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.invariants.get("ok", False) and self.purchases_ok == self.purchase_entries


def setup(db_path: str | Path, clock: ManualClock) -> None:
    led = Ledger(db_path, clock)
    clock.set(T0 - timedelta(days=10))
    led.execute("capital_injection", {"currency": "USD", "amount": 1_000_000_00})
    for o in ORGS:
        led.add_organizer(o, o.title(), "USD")
        led.add_event(f"{o}-past", o, "Past show", T0 - timedelta(days=1))
        led.add_event(f"{o}-open", o, "Next show", T0 + timedelta(days=30))
        for i in range(40):
            led.execute("ticket_purchase", {"order_id": f"pre-{o}-{i}", "organizer": o,
                                            "event_id": f"{o}-past", "gross": 5000 + 100 * i})
    clock.set(T0)
    led.close()


def run_stress(db_path: str | Path, threads: int = 8, ops: int = 40, seed: int = 1
               ) -> StressResult:
    clock = ManualClock(T0)
    setup(db_path, clock)
    res = StressResult(threads=threads, ops_per_thread=ops)
    lock = threading.Lock()
    sold: list[str] = [f"pre-{o}-{i}" for o in ORGS for i in range(40)]
    ok_purchases: set[str] = set()
    barrier = threading.Barrier(threads)

    def worker(n: int) -> None:
        rng = random.Random(seed * 1000 + n)
        led = Ledger(db_path, clock, init=False)
        barrier.wait()
        for i in range(ops):
            org = rng.choice(ORGS)
            op = rng.choices(
                ["purchase", "refund", "refund_after", "chargeback", "release", "payout",
                 "payout_hold"],
                weights=[40, 10, 6, 3, 6, 20, 15],
            )[0]
            key = f"t{n}-{i}"
            # Each request is fixed up front so a retry or resend is byte-for-byte the same.
            req: tuple[str, dict, str] | None = None
            hold_plan: tuple[int, bool, int] = (0, False, 0)
            if op == "purchase":
                oid = f"o-{n}-{i}"
                req = ("ticket_purchase", {"order_id": oid, "organizer": org,
                                           "event_id": f"{org}-open",
                                           "gross": rng.randint(20, 300) * 100}, f"buy-{oid}")
            elif op in ("refund", "refund_after", "chargeback"):
                with lock:
                    oid = rng.choice(sold)
                name = {"refund": "refund_before_payout", "refund_after": "refund_after_payout",
                        "chargeback": "chargeback"}[op]
                req = (name, {"order_id": oid}, key)
            elif op == "release":
                req = ("escrow_release", {"organizer": org, "event_id": f"{org}-past"},
                       f"release-{org}")
            elif op == "payout":
                req = ("organizer_payout", {"organizer": org, "currency": "USD",
                                            "amount": rng.randint(5, 80) * 100}, key)
            else:
                amt = rng.randint(5, 60) * 100
                hold_plan = (amt, rng.random() < 0.7, amt if rng.random() < 0.5 else amt // 2)

            def call(req=req, hold_plan=hold_plan, org=org, key=key) -> object:
                if req is not None:
                    return led.execute(req[0], req[1], idempotency_key=req[2],
                                       max_retries=rng.randint(0, 3))
                amt, capture, part = hold_plan
                hold = led.request_payout(org, "USD", amt, idempotency_key=key)
                if capture:
                    return led.capture_hold(hold["hold_uid"], part, idempotency_key=key + "-cap")
                return led.release_hold(hold["hold_uid"])

            outcome = "error"
            for _attempt in range(rng.randint(1, 6)):
                try:
                    call()
                    outcome = "ok"
                    if op == "purchase":
                        with lock:
                            ok_purchases.add(f"o-{n}-{i}")
                            sold.append(f"o-{n}-{i}")
                    if rng.random() < 0.2:  # client never saw the response: send again
                        call()
                        with lock:
                            res.duplicate_sends += 1
                    break
                except ConflictError:
                    outcome = "conflict"
                    with lock:
                        res.conflicts_seen += 1
                except LedgerError as exc:
                    outcome = exc.code
                    break
            with lock:
                res.outcomes[f"{op}:{outcome}"] += 1
        led.close()

    ts_ = [threading.Thread(target=worker, args=(n,)) for n in range(threads)]
    for t in ts_:
        t.start()
    for t in ts_:
        t.join()

    led = Ledger(db_path, clock, init=False)
    res.purchases_ok = len(ok_purchases)
    res.purchase_entries = led.conn.execute(
        "SELECT COUNT(*) FROM journal_entries WHERE template = 'ticket_purchase'"
        " AND external_ref LIKE 'o-%'"
    ).fetchone()[0]
    res.invariants = invariants.check_all(led.conn, ts(clock.now)).as_dict()
    led.close()
    return res
