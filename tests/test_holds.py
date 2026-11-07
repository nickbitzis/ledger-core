from datetime import timedelta

import pytest

from ledgercore.errors import InsufficientFunds, InvalidOperation, ValidationError
from tests.helpers import buy, release

AVAIL = "org:northbeam:available:USD"


@pytest.fixture
def funded(led, clock):
    buy(led, "o1", 10000)  # net 9401 after the 599 fee
    release(led, clock)
    return led


def test_payout_hold_moves_money_from_available_to_pending(funded):
    hold = funded.request_payout("northbeam", "USD", 4000)
    b = funded.organizer_balances("northbeam")
    assert (b["posted_available"], b["pending_payouts"], b["available"]) == (9401, 4000, 5401)
    assert hold["status"] == "active"


def test_holds_cannot_exceed_available(funded):
    funded.request_payout("northbeam", "USD", 9000)
    with pytest.raises(InsufficientFunds):
        funded.request_payout("northbeam", "USD", 402)
    funded.request_payout("northbeam", "USD", 401)


def test_held_funds_cannot_be_spent_elsewhere(funded):
    funded.request_payout("northbeam", "USD", 9000)
    with pytest.raises(InsufficientFunds, match="held"):
        funded.execute("manual_transfer", {"debit_account": AVAIL,
                                           "credit_account": "platform:bank:USD",
                                           "amount": 1000})


def test_partial_capture_releases_the_remainder(funded):
    hold = funded.request_payout("northbeam", "USD", 4000)
    out = funded.capture_hold(hold["hold_uid"], 2500)
    assert out["hold"]["status"] == "captured" and out["hold"]["captured_amount"] == 2500
    assert out["entry"]["template"] == "organizer_payout"
    b = funded.organizer_balances("northbeam")
    assert (b["posted_available"], b["pending_payouts"], b["available"]) == (6901, 0, 6901)
    with pytest.raises(InvalidOperation):
        funded.capture_hold(hold["hold_uid"])


def test_capture_cannot_exceed_hold(funded):
    hold = funded.request_payout("northbeam", "USD", 1000)
    with pytest.raises(ValidationError):
        funded.capture_hold(hold["hold_uid"], 1001)


def test_expired_hold_frees_funds(funded, clock):
    exp = clock.now + timedelta(hours=1)
    hold = funded.request_payout("northbeam", "USD", 9000, expires_at=exp)
    clock.advance(hours=2)
    assert funded.organizer_balances("northbeam")["available"] == 9401
    with pytest.raises(InvalidOperation, match="expired"):
        funded.capture_hold(hold["hold_uid"])
    assert funded.expire_holds() == 1
    assert funded.hold(hold["hold_uid"])["status"] == "expired"


def test_release_is_idempotent(funded):
    hold = funded.request_payout("northbeam", "USD", 1000)
    funded.release_hold(hold["hold_uid"])
    assert funded.release_hold(hold["hold_uid"])["status"] == "released"
    assert funded.organizer_balances("northbeam")["pending_payouts"] == 0


def test_card_authorization_partial_capture(led):
    hold = led.authorize_card("o7", "northbeam", "evt-1", 15000)
    b = led.organizer_balances("northbeam")
    assert b["incoming_authorizations"] == 15000 and b["escrow"] == 0
    out = led.capture_hold(hold["hold_uid"], 10000)  # one of three seats was unavailable
    assert out["entry"]["template"] == "ticket_purchase"
    assert out["entry"]["metadata"]["gross"] == 10000
    b = led.organizer_balances("northbeam")
    assert b["incoming_authorizations"] == 0 and b["escrow"] == 10000 - 599


def test_pending_balance_point_in_time(funded, clock):
    before = clock.now
    clock.advance(minutes=5)
    hold = funded.request_payout("northbeam", "USD", 3000)
    clock.advance(minutes=5)
    mid = clock.now
    clock.advance(minutes=1)
    funded.capture_hold(hold["hold_uid"])
    assert funded.organizer_balances("northbeam", as_of=before)["pending_payouts"] == 0
    assert funded.organizer_balances("northbeam", as_of=mid)["pending_payouts"] == 3000
    assert funded.organizer_balances("northbeam")["pending_payouts"] == 0


def test_payout_hold_with_stale_version_conflicts(funded):
    from ledgercore.errors import ConflictError
    v = funded.account(AVAIL)["version"]
    with pytest.raises(ConflictError):
        funded.request_payout("northbeam", "USD", 100, expected_version=v - 1)
    funded.request_payout("northbeam", "USD", 100, expected_version=v)
