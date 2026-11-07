import sqlite3
import threading
from datetime import timedelta

import pytest

from ledgercore.errors import (
    ConflictError,
    IdempotencyMismatch,
    InsufficientFunds,
    InvalidOperation,
    ValidationError,
)
from ledgercore.ledger import Ledger
from ledgercore.templates import Draft, Posting, cr, dr
from tests.helpers import T0, buy


def count(led, table="journal_entries"):
    return led.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_balanced_entry_updates_cached_balance_and_version(led):
    led.post(Draft("manual", [dr("platform:processing_fees:USD", 700),
                              cr("platform:bank:USD", 700)]))
    acc = led.account("platform:processing_fees:USD")
    assert acc["balance"] == 700 and acc["version"] == 1
    assert led.balance("platform:bank:USD") == 1_000_000 - 700


def test_unbalanced_entry_is_rejected_and_nothing_written(led):
    before = count(led)
    with pytest.raises(ValidationError, match="does not balance"):
        led.post(Draft("manual", [dr("platform:bank:USD", 100),
                                  cr("platform:capital:USD", 99)]))
    assert count(led) == before


def test_currencies_balance_separately(led):
    with pytest.raises(ValidationError):
        led.post(Draft("manual", [dr("platform:bank:USD", 100), cr("platform:capital:EUR", 100)]))
    led.post(Draft("manual", [
        dr("platform:bank:USD", 100), cr("platform:capital:USD", 100),
        dr("platform:bank:EUR", 90), cr("platform:capital:EUR", 90),
    ]))


def test_unknown_account_code_rejected(led):
    with pytest.raises(ValidationError):
        led.post(Draft("manual", [dr("platform:petty_cash:USD", 1), cr("platform:bank:USD", 1)]))


def test_database_rejects_non_integer_amounts(led):
    with pytest.raises(sqlite3.IntegrityError):
        led.conn.execute("INSERT INTO postings(entry_id, account_id, amount, currency)"
                         " VALUES (1, 1, 1.5, 'USD')")


def test_entries_and_postings_are_immutable(led):
    buy(led, "o1")
    for sql in ("UPDATE postings SET amount = amount + 1",
                "DELETE FROM postings",
                "UPDATE journal_entries SET memo = 'edited'",
                "DELETE FROM journal_entries"):
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            led.conn.execute(sql)


def test_reversal_restores_balances_and_cannot_repeat(led):
    e = buy(led, "o1")
    rev = led.reverse(e["id"], "captured twice by mistake")
    assert rev["reverses_entry_id"] == e["id"]
    assert led.balance("org:northbeam:escrow:evt-1:USD") == 0
    assert led.balance("platform:fee_revenue:USD") == 0
    with pytest.raises(InvalidOperation):
        led.reverse(e["id"], "again")
    with pytest.raises(InvalidOperation):
        led.reverse(rev["id"], "undo the undo")


def test_reversal_respects_sign_rules(led, clock):
    e = buy(led, "o1")
    clock.advance(days=2)
    led.execute("escrow_release", {"organizer": "northbeam", "event_id": "evt-1"})
    with pytest.raises(InsufficientFunds):
        led.reverse(e["id"], "escrow is gone")


def test_point_in_time_balance(led, clock):
    buy(led, "o1", 5000)
    mid = clock.now
    clock.advance(hours=1)
    buy(led, "o2", 10000)
    esc = "org:northbeam:escrow:evt-1:USD"
    assert led.balance(esc, as_of=mid) == 5000 - 349
    assert led.balance(esc, as_of=mid - timedelta(seconds=1)) == 0
    assert led.balance(esc) == (5000 - 349) + (10000 - 599)


def test_statement_running_balance(led, clock):
    buy(led, "o1", 5000)
    clock.advance(hours=1)
    start = clock.now
    buy(led, "o2", 10000)
    st = led.statement("org:northbeam:escrow:evt-1:USD", start=start)
    assert st["opening_balance"] == 4651
    assert [ln["amount"] for ln in st["lines"]] == [10000, -599]
    assert st["closing_balance"] == 4651 + 9401


def test_sign_rule_blocks_overdraft(led):
    with pytest.raises(InsufficientFunds):
        led.execute("manual_transfer", {"debit_account": "org:northbeam:available:USD",
                                        "credit_account": "platform:bank:USD", "amount": 1})


def test_idempotency_replay_and_mismatch(led):
    p = {"order_id": "o1", "organizer": "northbeam", "event_id": "evt-1", "gross": 5000}
    first = led.execute("ticket_purchase", p, idempotency_key="k1")
    again = led.execute("ticket_purchase", p, idempotency_key="k1")
    assert again["idempotent_replay"] and again["id"] == first["id"]
    assert count(led, "journal_entries") == 2  # capital + one purchase
    with pytest.raises(IdempotencyMismatch):
        led.execute("ticket_purchase", {**p, "gross": 6000}, idempotency_key="k1")


def test_one_purchase_per_order(led):
    buy(led, "o1")
    with pytest.raises(InvalidOperation, match="already recorded"):
        buy(led, "o1")


def _concurrent_write(led, clock):
    """Hook that commits a competing write after the snapshot is read."""
    other = Ledger(led.db_path, clock, init=False)
    fired = {"n": 0}

    def hook():
        if fired["n"] == 0:
            fired["n"] += 1
            buy(other, f"race-{fired['n']}")
    return hook


def test_optimistic_conflict_detected(led, clock):
    buy(led, "o1")
    led.on_snapshot = _concurrent_write(led, clock)
    with pytest.raises(ConflictError):
        led.execute("refund_before_payout", {"order_id": "o1"}, max_retries=0)


def test_optimistic_conflict_retried(led, clock):
    buy(led, "o1")
    led.on_snapshot = _concurrent_write(led, clock)
    out = led.execute("refund_before_payout", {"order_id": "o1"}, max_retries=2)
    assert out["template"] == "refund_before_payout"


def test_expected_version_mismatch_conflicts(led):
    with pytest.raises(ConflictError):
        led.execute("capital_injection", {"currency": "USD", "amount": 5},
                    expected_versions={"platform:bank:USD": 99})


def test_threads_never_double_spend(led, clock):
    """20 threads race to pay out 30.00 from 100.00 available: at most 3 can win."""
    led.post(Draft("seed", [dr("platform:processing_fees:USD", 10000),
                            cr("org:northbeam:available:USD", 10000)]))
    wins, errors = [], []

    def go(i):
        mine = Ledger(led.db_path, clock, init=False)
        try:
            mine.execute("manual_transfer", {"debit_account": "org:northbeam:available:USD",
                                             "credit_account": "platform:bank:USD",
                                             "amount": 3000}, max_retries=10)
            wins.append(i)
        except (InsufficientFunds, ConflictError) as exc:
            errors.append(exc)
        finally:
            mine.close()
    threads = [threading.Thread(target=go, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wins) == 3
    assert led.balance("org:northbeam:available:USD") == 1000


def test_add_event_requires_known_organizer(led):
    from ledgercore.errors import NotFound
    with pytest.raises(NotFound):
        led.add_event("x", "nobody", "x", T0)


def test_hand_built_posting_list_must_have_two_legs(led):
    with pytest.raises(ValidationError):
        led.post(Draft("manual", [Posting("platform:bank:USD", 0 + 1)]))
