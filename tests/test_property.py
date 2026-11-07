"""Property-based tests: random operation sequences must never break an invariant."""

import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ledgercore import invariants
from ledgercore.clock import ManualClock, ts
from ledgercore.errors import LedgerError, ValidationError
from ledgercore.ledger import Ledger
from ledgercore.money import convert
from ledgercore.templates import Draft, Posting

T0 = datetime(2026, 11, 2, 12, tzinfo=UTC)
ORGS = ["northbeam", "copperline"]

op = st.one_of(
    st.tuples(st.just("buy"), st.sampled_from(ORGS), st.integers(1, 3),
              st.integers(500, 50_000)),
    st.tuples(st.just("refund"), st.integers(0, 40), st.booleans()),
    st.tuples(st.just("chargeback"), st.integers(0, 40)),
    st.tuples(st.just("release"), st.sampled_from(ORGS), st.integers(1, 3)),
    st.tuples(st.just("payout"), st.sampled_from(ORGS), st.integers(1, 30_000)),
    st.tuples(st.just("hold"), st.sampled_from(ORGS), st.integers(1, 30_000),
              st.sampled_from(["capture", "partial", "release", "leave"])),
    st.tuples(st.just("tick"), st.integers(1, 30)),
    st.tuples(st.just("reverse"), st.integers(0, 60)),
    st.tuples(st.just("expire")),
)

SETTINGS = settings(max_examples=40, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow])


def _ledger(tmp: str) -> tuple[Ledger, ManualClock]:
    clock = ManualClock(T0)
    led = Ledger(Path(tmp) / "p.db", clock)
    for o in ORGS:
        led.add_organizer(o, o.title(), "USD")
        for i in (1, 2, 3):
            led.add_event(f"{o}-{i}", o, f"Show {i}", T0 + timedelta(hours=24 * i))
    led.execute("capital_injection", {"currency": "USD", "amount": 10_000_000})
    return led, clock


def _apply(led: Ledger, clock: ManualClock, sold: list[str], step: tuple, n: int) -> None:
    kind = step[0]
    if kind == "buy":
        _, o, ev, gross = step
        led.execute("ticket_purchase", {"order_id": f"o{n}", "organizer": o,
                                        "event_id": f"{o}-{ev}", "gross": gross})
        sold.append(f"o{n}")
    elif kind == "refund" and sold:
        name = "refund_after_payout" if step[2] else "refund_before_payout"
        led.execute(name, {"order_id": sold[step[1] % len(sold)]})
    elif kind == "chargeback" and sold:
        led.execute("chargeback", {"order_id": sold[step[1] % len(sold)]})
    elif kind == "release":
        led.execute("escrow_release", {"organizer": step[1], "event_id": f"{step[1]}-{step[2]}"})
    elif kind == "payout":
        led.execute("organizer_payout", {"organizer": step[1], "currency": "USD",
                                         "amount": step[2]})
    elif kind == "hold":
        h = led.request_payout(step[1], "USD", step[2])
        if step[3] == "capture":
            led.capture_hold(h["hold_uid"])
        elif step[3] == "partial" and step[2] > 1:
            led.capture_hold(h["hold_uid"], step[2] // 2)
        elif step[3] == "release":
            led.release_hold(h["hold_uid"])
    elif kind == "tick":
        clock.advance(hours=step[1])
    elif kind == "reverse":
        last = led.conn.execute("SELECT MAX(id) FROM journal_entries").fetchone()[0]
        led.reverse(1 + step[1] % last, "property test")
    elif kind == "expire":
        led.expire_holds()


@SETTINGS
@given(st.lists(op, min_size=1, max_size=40))
def test_random_operations_keep_invariants(steps):
    with tempfile.TemporaryDirectory() as tmp:
        led, clock = _ledger(tmp)
        sold: list[str] = []
        for n, step in enumerate(steps):
            try:
                _apply(led, clock, sold, step, n)
            except LedgerError:
                pass  # rejected operations are fine; they must leave no trace
            rep = invariants.check_all(led.conn, ts(clock.now))
            assert rep.ok, (step, rep.as_dict())
        # Organizer net positions plus platform accounts still sum to zero.
        total = led.conn.execute("SELECT SUM(amount) FROM postings").fetchone()[0]
        assert total == 0
        led.close()


accounts = st.sampled_from([
    "platform:bank:USD", "platform:fx_position:USD", "platform:processor_clearing:USD",
    "platform:bank:EUR", "platform:fx_position:EUR",
])


@SETTINGS
@given(st.lists(st.tuples(accounts, st.integers(-10**9, 10**9).filter(bool)),
                min_size=2, max_size=8))
def test_only_balanced_entries_are_accepted(legs):
    with tempfile.TemporaryDirectory() as tmp:
        led, _ = _ledger(tmp)
        sums: dict[str, int] = {}
        for code, amt in legs:
            sums[code[-3:]] = sums.get(code[-3:], 0) + amt
        draft = Draft("manual", [Posting(c, a) for c, a in legs])
        before = led.conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0]
        try:
            led.post(draft)
            accepted = True
        except ValidationError:
            accepted = False
        except LedgerError:
            accepted = None  # balanced but broke a sign rule
        if any(sums.values()):
            assert accepted is False
            assert led.conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == before
        assert invariants.check_all(led.conn).ok
        led.close()


@settings(max_examples=200, deadline=None)
@given(st.integers(1, 10**12), st.decimals(min_value="0.0001", max_value="1000", places=4))
def test_fx_rounding_is_within_half_a_minor_unit(amount, rate):
    out = convert(amount, "USD", "EUR", rate)
    exact = Decimal(amount) * rate
    assert abs(Decimal(out) - exact) <= Decimal("0.5")
