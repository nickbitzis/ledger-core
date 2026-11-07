from ledgercore import invariants
from tests.helpers import buy, release


def test_clean_ledger_passes_every_check(led, clock):
    buy(led, "o1")
    release(led, clock)
    led.execute("organizer_payout", {"organizer": "northbeam", "currency": "USD", "amount": 100})
    rep = invariants.check_all(led.conn)
    assert rep.ok, rep.as_dict()
    assert len(rep.checks) == 6


def test_tampered_posting_is_caught(led):
    buy(led, "o1")
    led.conn.execute("DROP TRIGGER postings_no_update")  # simulate a bad migration
    led.conn.execute("UPDATE postings SET amount = amount + 1 WHERE id = 3")
    rep = invariants.check_all(led.conn)
    assert rep.checks["zero_sum_per_currency"]
    assert rep.checks["cached_balances_match"]


def test_drifted_cache_and_sign_rule_are_caught(led):
    buy(led, "o1")
    led.conn.execute(
        "UPDATE balances SET balance = 999 WHERE account_id ="
        " (SELECT id FROM accounts WHERE code = 'org:northbeam:escrow:evt-1:USD')")
    rep = invariants.check_all(led.conn)
    assert rep.checks["cached_balances_match"]
    assert rep.checks["sign_rules"]


def test_payout_without_released_escrow_is_caught(led):
    # A manual credit to the organizer bypasses escrow; a payout of it must be flagged.
    led.execute("manual_transfer", {"debit_account": "platform:processing_fees:USD",
                                    "credit_account": "org:northbeam:available:USD",
                                    "amount": 1000})
    led.execute("organizer_payout", {"organizer": "northbeam", "currency": "USD", "amount": 1000})
    rep = invariants.check_all(led.conn)
    assert rep.checks["payouts_covered_by_released_escrow"]
    assert not rep.ok


def test_holds_above_balance_are_caught(led, clock):
    buy(led, "o1")
    release(led, clock)
    led.request_payout("northbeam", "USD", 4000)
    led.conn.execute("UPDATE holds SET amount = 999999")
    rep = invariants.check_all(led.conn)
    assert rep.checks["holds_within_balance"]
