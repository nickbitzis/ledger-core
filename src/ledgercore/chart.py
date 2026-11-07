"""Chart of accounts.

Account codes are structured strings, so templates can name an account and the
ledger can create it on first use with the right type and sign rule:

    platform:<role>:<CCY>                 platform:bank:USD
    org:<organizer>:<role>:<CCY>          org:northbeam:available:USD
    org:<organizer>:escrow:<event>:<CCY>  org:northbeam:escrow:evt-101:USD

Signed posting amounts: positive is a debit, negative is a credit. An account's
"normal balance" is the sum of its postings, flipped for credit-normal types,
so a healthy liability shows a positive number.
"""

from __future__ import annotations

from dataclasses import dataclass

from ledgercore.errors import ValidationError
from ledgercore.money import require_currency

NORMAL_SIDE = {
    "asset": "debit",
    "expense": "debit",
    "liability": "credit",
    "equity": "credit",
    "revenue": "credit",
}

# role -> (type, allow_negative, description)
PLATFORM_ROLES: dict[str, tuple[str, bool, str]] = {
    "bank": ("asset", False, "Platform operating bank account"),
    "processor_clearing": (
        "asset",
        True,
        "Owed to us by the card processor. Negative when refunds net against settlement.",
    ),
    "fee_revenue": ("revenue", False, "Platform fee earned on ticket sales"),
    "processing_fees": ("expense", False, "Card processing fees charged by the processor"),
    "chargeback_fees": ("expense", False, "Dispute fees charged by the processor"),
    "fx_position": ("asset", True, "FX trading account, one per currency, nets to the FX P&L"),
    "capital": ("equity", False, "Owner capital that funds the operating account"),
}

ORG_ROLES: dict[str, tuple[str, bool, str]] = {
    "escrow": ("liability", False, "Ticket money held until the event is over"),
    "available": ("liability", False, "Released funds the organizer can withdraw"),
    "receivable": (
        "asset",
        False,
        "Organizer owes us (a negative organizer balance). Recovered from future releases.",
    ),
}


@dataclass(frozen=True)
class AccountSpec:
    code: str
    type: str
    normal_side: str
    currency: str
    owner: str
    role: str
    event_id: str | None
    allow_negative: bool


def platform(role: str, ccy: str) -> str:
    return f"platform:{role}:{ccy}"


def org(organizer: str, role: str, ccy: str) -> str:
    return f"org:{organizer}:{role}:{ccy}"


def escrow(organizer: str, event_id: str, ccy: str) -> str:
    return f"org:{organizer}:escrow:{event_id}:{ccy}"


def _slug_ok(s: str) -> bool:
    return bool(s) and all(c.isalnum() or c in "-_" for c in s)


def spec_for(code: str) -> AccountSpec:
    """Derive type and sign rule from a code. Unknown shapes are rejected."""
    parts = code.split(":")
    if parts[0] == "platform" and len(parts) == 3 and parts[1] in PLATFORM_ROLES:
        typ, neg, _ = PLATFORM_ROLES[parts[1]]
        ccy = require_currency(parts[2])
        return AccountSpec(code, typ, NORMAL_SIDE[typ], ccy, "platform", parts[1], None, neg)
    if parts[0] == "org" and len(parts) in (4, 5) and _slug_ok(parts[1]):
        role = parts[2]
        if role == "escrow" and len(parts) == 5 and _slug_ok(parts[3]):
            event_id, ccy = parts[3], parts[4]
        elif role in ORG_ROLES and role != "escrow" and len(parts) == 4:
            event_id, ccy = None, parts[3]
        else:
            raise ValidationError(f"bad organizer account code {code!r}")
        typ, neg, _ = ORG_ROLES[role]
        require_currency(ccy)
        return AccountSpec(code, typ, NORMAL_SIDE[typ], ccy, parts[1], role, event_id, neg)
    raise ValidationError(f"unknown account code {code!r}")


def normal_sign(normal_side: str) -> int:
    return 1 if normal_side == "debit" else -1
