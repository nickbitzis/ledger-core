from datetime import timedelta

import pytest

from ledgercore.errors import InsufficientFunds, InvalidOperation, NotFound, ValidationError
from ledgercore.templates import CHARGEBACK_FEE, expected_processor_fee, platform_fee_for
from tests.helpers import T0, buy, by_account, release

ESC = "org:northbeam:escrow:evt-1:USD"
AVAIL = "org:northbeam:available:USD"
RECV = "org:northbeam:receivable:USD"
CLEAR = "platform:processor_clearing:USD"
REV = "platform:fee_revenue:USD"


def test_fee_schedules():
    assert platform_fee_for(5000) == 349
    assert expected_processor_fee(5000) == 175
    assert expected_processor_fee(4900) == 172  # 142.1 rounds to 142, plus 30


def test_ticket_purchase_postings(led):
    e = buy(led, "o1", 5000)
    assert by_account(e) == {CLEAR: 5000, ESC: -5000 + 349, REV: -349}
    assert e["metadata"]["processor_ref"] == "psp_o1"


def test_purchase_rules(led, clock):
    with pytest.raises(ValidationError):
        buy(led, "o1", 50)  # below the fee
    with pytest.raises(ValidationError):
        buy(led, "o2", event_id="evt-k")  # kestrel's event
    with pytest.raises(NotFound):
        buy(led, "o3", event_id="evt-missing")
    clock.advance(days=2)
    with pytest.raises(InvalidOperation, match="over"):
        buy(led, "o4")


def test_platform_fee_template_alone(led):
    buy(led, "o1", 5000)
    e = led.execute("platform_fee", {"organizer": "northbeam", "event_id": "evt-1",
                                     "amount": 100})
    assert by_account(e) == {ESC: 100, REV: -100}


def test_escrow_release_waits_for_event_end(led, clock):
    buy(led, "o1", 5000)
    with pytest.raises(InvalidOperation, match="not ended"):
        led.execute("escrow_release", {"organizer": "northbeam", "event_id": "evt-1"})
    e = release(led, clock)
    assert by_account(e) == {ESC: 4651, AVAIL: -4651}
    with pytest.raises(InvalidOperation, match="nothing"):
        release(led, clock)


def test_payout_and_overdraft(led, clock):
    buy(led, "o1", 5000)
    release(led, clock)
    e = led.execute("organizer_payout", {"organizer": "northbeam", "currency": "USD",
                                         "amount": 4000})
    assert by_account(e) == {AVAIL: 4000, "platform:bank:USD": -4000}
    with pytest.raises(InsufficientFunds):
        led.execute("organizer_payout", {"organizer": "northbeam", "currency": "USD",
                                         "amount": 652})


def test_refund_before_payout_from_escrow(led):
    buy(led, "o1", 5000)
    e = led.execute("refund_before_payout", {"order_id": "o1"})
    assert by_account(e) == {ESC: 4651, REV: 349, CLEAR: -5000}
    assert led.balance(ESC) == 0 and led.balance(REV) == 0 and led.balance(CLEAR) == 0


def test_refund_before_payout_from_available(led, clock):
    buy(led, "o1", 5000)
    release(led, clock)
    e = led.execute("refund_before_payout", {"order_id": "o1"})
    assert by_account(e)[AVAIL] == 4651


def test_refund_after_payout_creates_organizer_debt(led, clock):
    buy(led, "o1", 5000)
    buy(led, "o2", 5000)
    release(led, clock)
    led.execute("organizer_payout", {"organizer": "northbeam", "currency": "USD",
                                     "amount": 9000})
    with pytest.raises(InsufficientFunds, match="refund_after_payout"):
        led.execute("refund_before_payout", {"order_id": "o1"})
    e = led.execute("refund_after_payout", {"order_id": "o1"})
    assert by_account(e) == {AVAIL: 302, RECV: 4349, REV: 349, CLEAR: -5000}
    b = led.organizer_balances("northbeam")
    assert b["receivable"] == 4349 and b["net_position"] == -4349


def test_order_can_only_be_unwound_once(led):
    buy(led, "o1")
    led.execute("refund_before_payout", {"order_id": "o1"})
    with pytest.raises(InvalidOperation):
        led.execute("refund_after_payout", {"order_id": "o1"})
    with pytest.raises(InvalidOperation):
        led.execute("chargeback", {"order_id": "o1"})


def test_chargeback_postings(led):
    buy(led, "o1", 5000)
    e = led.execute("chargeback", {"order_id": "o1"})
    assert by_account(e) == {ESC: 4651, REV: 349, "platform:chargeback_fees:USD": CHARGEBACK_FEE,
                             CLEAR: -(5000 + CHARGEBACK_FEE)}


def test_negative_balance_recovered_from_next_release(led, clock):
    buy(led, "o1", 5000)
    led.execute("ticket_purchase", {"order_id": "o9", "organizer": "northbeam",
                                    "event_id": "evt-2", "gross": 20000})
    release(led, clock)
    led.execute("organizer_payout", {"organizer": "northbeam", "currency": "USD",
                                     "amount": 4651})
    led.execute("chargeback", {"order_id": "o1"})
    assert led.balance(RECV) == 4651
    clock.set(T0 + timedelta(days=6))
    e = led.execute("escrow_release", {"organizer": "northbeam", "event_id": "evt-2"})
    assert e["metadata"]["recovered"] == 4651
    assert by_account(e) == {"org:northbeam:escrow:evt-2:USD": 18901, RECV: -4651,
                             AVAIL: -(18901 - 4651)}
    assert led.balance(RECV) == 0


def test_fx_conversion_balances_per_currency(led):
    e = led.execute("fx_conversion", {"from_currency": "USD", "to_currency": "EUR",
                                      "amount": 100_000, "rate": "0.9213"})
    assert by_account(e) == {"platform:bank:USD": -100_000, "platform:fx_position:USD": 100_000,
                             "platform:bank:EUR": 92_130, "platform:fx_position:EUR": -92_130}
    assert led.balance("platform:bank:EUR") == 92_130
    with pytest.raises(InsufficientFunds):
        led.execute("fx_conversion", {"from_currency": "EUR", "to_currency": "USD",
                                      "amount": 92_131, "rate": "1.0854"})
    with pytest.raises(ValidationError):
        led.execute("fx_conversion", {"from_currency": "USD", "to_currency": "EUR",
                                      "amount": 100, "rate": 0.92})


def test_processor_settlement(led):
    buy(led, "o1", 5000)
    e = led.execute("processor_settlement", {"currency": "USD", "gross": 5000, "fee": 175})
    assert by_account(e) == {"platform:bank:USD": 4825, "platform:processing_fees:USD": 175,
                             CLEAR: -5000}


def test_transfer_must_stay_in_one_currency(led):
    with pytest.raises(ValidationError, match="fx_conversion"):
        led.execute("manual_transfer", {"debit_account": "platform:bank:EUR",
                                        "credit_account": "platform:bank:USD", "amount": 1})


def test_unknown_template(led):
    with pytest.raises(NotFound):
        led.execute("free_money", {})
