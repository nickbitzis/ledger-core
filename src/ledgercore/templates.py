"""Money flows as named templates.

Each template is a function `(params, view) -> Draft`. It reads balances
through the view, decides the postings, and returns them. It never writes.
The ledger validates the draft (balanced per currency, integer amounts, sign
rules) and commits it in one transaction. The README has a T-account table
for every template.

Signed amounts: positive is a debit, negative is a credit.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ledgercore import chart
from ledgercore.errors import InsufficientFunds, InvalidOperation, ValidationError
from ledgercore.money import (
    bps,
    convert,
    parse_rate,
    require_currency,
    require_minor,
    require_positive,
)
from ledgercore.view import View

# Fee schedule. Integers only: basis points and fixed minor units.
PLATFORM_FEE_BPS = 500  # 5.00%
PLATFORM_FEE_FIXED = 99  # plus 0.99 per order
PROCESSOR_FEE_BPS = 290  # what the card processor is expected to charge: 2.9%
PROCESSOR_FEE_FIXED = 30  # plus 0.30
CHARGEBACK_FEE = 1500  # 15.00 dispute fee


@dataclass(frozen=True)
class Posting:
    account: str
    amount: int

    def __post_init__(self) -> None:
        require_minor(self.amount, field=f"posting amount for {self.account}")


def dr(account: str, amount: int) -> Posting:
    return Posting(account, require_positive(amount))


def cr(account: str, amount: int) -> Posting:
    return Posting(account, -require_positive(amount))


@dataclass
class Draft:
    template: str
    postings: list[Posting]
    memo: str = ""
    external_ref: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def platform_fee_for(gross: int) -> int:
    return bps(gross, PLATFORM_FEE_BPS) + PLATFORM_FEE_FIXED


def expected_processor_fee(gross: int) -> int:
    """2.9% rounded half up, plus 0.30. Used by reconciliation to spot fee differences."""
    return (gross * PROCESSOR_FEE_BPS + 5_000) // 10_000 + PROCESSOR_FEE_FIXED


def _str(p: dict, key: str) -> str:
    v = p.get(key)
    if not isinstance(v, str) or not v:
        raise ValidationError(f"{key} is required")
    return v


# --- templates ---------------------------------------------------------------


def platform_fee(p: dict, v: View) -> Draft:
    """Move the platform's cut from an event's escrow into fee revenue."""
    organizer, event_id = _str(p, "organizer"), _str(p, "event_id")
    amount = require_positive(p.get("amount"))
    ccy = v.event(event_id)["currency"]
    return Draft(
        "platform_fee",
        [dr(chart.escrow(organizer, event_id, ccy), amount),
         cr(chart.platform("fee_revenue", ccy), amount)],
        memo=f"platform fee {event_id}",
        external_ref=p.get("order_id"),
        metadata={"organizer": organizer, "event_id": event_id, "amount": amount},
    )


def ticket_purchase(p: dict, v: View) -> Draft:
    """Card capture for a ticket order. Gross goes to event escrow, minus the platform fee."""
    order_id, organizer, event_id = _str(p, "order_id"), _str(p, "organizer"), _str(p, "event_id")
    gross = require_positive(p.get("gross"), field="gross")
    ev = v.event(event_id)
    if ev["organizer_id"] != organizer:
        raise ValidationError(f"event {event_id} does not belong to {organizer}")
    if v.event_over(event_id):
        raise InvalidOperation(f"event {event_id} is over; ticket sales are closed")
    ccy = ev["currency"]
    fee = platform_fee_for(gross)
    if fee >= gross:
        raise ValidationError(f"gross {gross} is below the minimum ticket price")
    fee_draft = platform_fee({"organizer": organizer, "event_id": event_id, "amount": fee}, v)
    postings = [
        dr(chart.platform("processor_clearing", ccy), gross),
        cr(chart.escrow(organizer, event_id, ccy), gross),
        *fee_draft.postings,
    ]
    return Draft(
        "ticket_purchase",
        postings,
        memo=f"ticket order {order_id}",
        external_ref=order_id,
        metadata={
            "order_id": order_id,
            "organizer": organizer,
            "event_id": event_id,
            "currency": ccy,
            "gross": gross,
            "platform_fee": fee,
            "processor_ref": f"psp_{order_id}",
            "expected_processor_fee": expected_processor_fee(gross),
        },
    )


def escrow_release(p: dict, v: View) -> Draft:
    """After the event, move its escrow to the organizer. Any organizer debt is repaid first."""
    organizer, event_id = _str(p, "organizer"), _str(p, "event_id")
    ev = v.event(event_id)
    if ev["organizer_id"] != organizer:
        raise ValidationError(f"event {event_id} does not belong to {organizer}")
    if not v.event_over(event_id):
        raise InvalidOperation(f"event {event_id} has not ended; escrow stays held")
    ccy = ev["currency"]
    esc = chart.escrow(organizer, event_id, ccy)
    amount = v.balance(esc)
    if amount <= 0:
        raise InvalidOperation(f"nothing in escrow for {event_id}")
    owed = v.balance(chart.org(organizer, "receivable", ccy))
    recovered = min(amount, owed)
    postings = [dr(esc, amount)]
    if recovered:
        postings.append(cr(chart.org(organizer, "receivable", ccy), recovered))
    if amount - recovered:
        postings.append(cr(chart.org(organizer, "available", ccy), amount - recovered))
    return Draft(
        "escrow_release",
        postings,
        memo=f"escrow release {event_id}",
        external_ref=event_id,
        metadata={
            "organizer": organizer,
            "event_id": event_id,
            "currency": ccy,
            "released": amount,
            "recovered": recovered,
            "to_available": amount - recovered,
        },
    )


def organizer_payout(p: dict, v: View) -> Draft:
    """Pay the organizer's available balance out to their bank. Never overdraws."""
    organizer, ccy = _str(p, "organizer"), require_currency(p.get("currency"))
    amount = require_positive(p.get("amount"))
    avail_code = chart.org(organizer, "available", ccy)
    avail = v.available(avail_code)
    if amount > avail:
        raise InsufficientFunds(f"payout {amount} exceeds available balance {avail}")
    return Draft(
        "organizer_payout",
        [dr(avail_code, amount), cr(chart.platform("bank", ccy), amount)],
        memo=f"payout to {organizer}",
        external_ref=p.get("payout_ref"),
        metadata={"organizer": organizer, "currency": ccy, "amount": amount},
    )


def _draw(v: View, organizer: str, event_id: str, ccy: str, amount: int, allow_debt: bool):
    """Take `amount` from the organizer: event escrow, then available, then (if allowed) debt."""
    split: dict[str, int] = {}
    left = amount
    for role, code in (
        ("escrow", chart.escrow(organizer, event_id, ccy)),
        ("available", chart.org(organizer, "available", ccy)),
    ):
        have = v.available(code) if role == "available" else v.balance(code)
        take = min(left, max(have, 0))
        if take:
            split[code] = take
            left -= take
    if left:
        if not allow_debt:
            raise InsufficientFunds(
                f"organizer {organizer} cannot cover {amount}; short by {left}."
                " Use refund_after_payout to record a negative organizer balance."
            )
        split[chart.org(organizer, "receivable", ccy)] = left
    return [dr(code, amt) for code, amt in split.items()], split


def _refund(p: dict, v: View, name: str, allow_debt: bool) -> Draft:
    order = v.order(_str(p, "order_id"))
    if order.unwound:
        raise InvalidOperation(f"order {order.order_id} was already refunded or charged back")
    ccy, net = order.currency, order.gross - order.platform_fee
    debits, split = _draw(v, order.organizer, order.event_id, ccy, net, allow_debt)
    postings = [
        *debits,
        dr(chart.platform("fee_revenue", ccy), order.platform_fee),
        cr(chart.platform("processor_clearing", ccy), order.gross),
    ]
    return Draft(
        name,
        postings,
        memo=f"refund order {order.order_id}",
        external_ref=order.order_id,
        metadata={
            "order_id": order.order_id,
            "organizer": order.organizer,
            "event_id": order.event_id,
            "currency": ccy,
            "gross": order.gross,
            "processor_ref": f"psp_{order.order_id}",
            "split": split,
        },
    )


def refund_before_payout(p: dict, v: View) -> Draft:
    """Full refund while the money is still with us (escrow or available). Fails otherwise."""
    return _refund(p, v, "refund_before_payout", allow_debt=False)


def refund_after_payout(p: dict, v: View) -> Draft:
    """Full refund after the organizer was paid. The shortfall becomes organizer debt."""
    return _refund(p, v, "refund_after_payout", allow_debt=True)


def chargeback(p: dict, v: View) -> Draft:
    """Cardholder dispute. Processor pulls gross plus a fee; organizer bears the net."""
    order = v.order(_str(p, "order_id"))
    if order.unwound:
        raise InvalidOperation(f"order {order.order_id} was already refunded or charged back")
    fee = require_positive(p.get("dispute_fee", CHARGEBACK_FEE), field="dispute_fee")
    ccy, net = order.currency, order.gross - order.platform_fee
    debits, split = _draw(v, order.organizer, order.event_id, ccy, net, allow_debt=True)
    postings = [
        *debits,
        dr(chart.platform("fee_revenue", ccy), order.platform_fee),
        dr(chart.platform("chargeback_fees", ccy), fee),
        cr(chart.platform("processor_clearing", ccy), order.gross + fee),
    ]
    return Draft(
        "chargeback",
        postings,
        memo=f"chargeback order {order.order_id}",
        external_ref=order.order_id,
        metadata={
            "order_id": order.order_id,
            "organizer": order.organizer,
            "currency": ccy,
            "gross": order.gross,
            "dispute_fee": fee,
            "split": split,
        },
    )


def processor_settlement(p: dict, v: View) -> Draft:
    """The processor pays a batch into our bank, net of its fees."""
    ccy = require_currency(p.get("currency"))
    gross = require_positive(p.get("gross"), field="gross")
    fee = require_minor(p.get("fee", 0), field="fee", allow_zero=True)
    if not 0 <= fee < gross:
        raise ValidationError("fee must be between 0 and gross")
    postings = [dr(chart.platform("bank", ccy), gross - fee)]
    if fee:
        postings.append(dr(chart.platform("processing_fees", ccy), fee))
    postings.append(cr(chart.platform("processor_clearing", ccy), gross))
    return Draft(
        "processor_settlement",
        postings,
        memo="processor settlement",
        external_ref=p.get("settlement_ref"),
        metadata={"currency": ccy, "gross": gross, "fee": fee},
    )


def fx_conversion(p: dict, v: View) -> Draft:
    """Convert bank cash between currencies through per-currency FX trading accounts.

    Each currency balances on its own, so the per-currency zero-sum rule holds.
    The FX position accounts carry the conversion, and their sum at reporting
    rates is the FX gain or loss.
    """
    src, dst = require_currency(p.get("from_currency")), require_currency(p.get("to_currency"))
    if src == dst:
        raise ValidationError("from_currency and to_currency must differ")
    amount = require_positive(p.get("amount"))
    rate = parse_rate(p.get("rate"))
    out = convert(amount, src, dst, rate)
    if out <= 0:
        raise ValidationError("converted amount rounds to zero")
    have = v.balance(chart.platform("bank", src))
    if amount > have:
        raise InsufficientFunds(f"bank {src} has {have}, cannot convert {amount}")
    return Draft(
        "fx_conversion",
        [
            cr(chart.platform("bank", src), amount),
            dr(chart.platform("fx_position", src), amount),
            dr(chart.platform("bank", dst), out),
            cr(chart.platform("fx_position", dst), out),
        ],
        memo=f"FX {src}->{dst} at {rate}",
        external_ref=p.get("fx_ref"),
        metadata={"from": src, "to": dst, "amount": amount, "rate": str(rate), "converted": out},
    )


def capital_injection(p: dict, v: View) -> Draft:
    """Owner funds the operating account. Used to seed the demo."""
    ccy = require_currency(p.get("currency"))
    amount = require_positive(p.get("amount"))
    return Draft(
        "capital_injection",
        [dr(chart.platform("bank", ccy), amount), cr(chart.platform("capital", ccy), amount)],
        memo="capital injection",
        metadata={"currency": ccy, "amount": amount},
    )


def manual_transfer(p: dict, v: View) -> Draft:
    """Generic two-leg transfer between accounts in the same currency. Sign rules still apply."""
    debit, credit = _str(p, "debit_account"), _str(p, "credit_account")
    amount = require_positive(p.get("amount"))
    if debit == credit:
        raise ValidationError("debit and credit accounts must differ")
    da, ca = chart.spec_for(debit), chart.spec_for(credit)
    if da.currency != ca.currency:
        raise ValidationError("a transfer must stay in one currency; use fx_conversion")
    v.balance(debit)
    v.balance(credit)
    return Draft(
        "manual_transfer",
        [dr(debit, amount), cr(credit, amount)],
        memo=str(p.get("memo", "manual transfer"))[:200],
        external_ref=p.get("transfer_ref"),
        metadata={"amount": amount, "currency": da.currency},
    )


Template = Callable[[dict, View], Draft]

TEMPLATES: dict[str, Template] = {
    "ticket_purchase": ticket_purchase,
    "platform_fee": platform_fee,
    "escrow_release": escrow_release,
    "organizer_payout": organizer_payout,
    "refund_before_payout": refund_before_payout,
    "refund_after_payout": refund_after_payout,
    "chargeback": chargeback,
    "processor_settlement": processor_settlement,
    "fx_conversion": fx_conversion,
    "capital_injection": capital_injection,
    "manual_transfer": manual_transfer,
}
