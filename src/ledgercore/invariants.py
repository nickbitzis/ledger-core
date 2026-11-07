"""Invariant checker. Each check returns a list of violations; empty means it holds.

1. Every entry balances per currency, and all postings sum to zero per currency.
2. No account breaks its sign rule (non-negative unless allow_negative).
3. Cached balances and versions match a full recompute from postings.
4. Every payout is covered by escrow already released to that organizer.
5. Active debit holds never exceed the balance they reserve.
6. Every reversal exactly mirrors its original entry.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class Report:
    checks: dict[str, list[str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(not v for v in self.checks.values())

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checks": {k: {"ok": not v, "violations": v[:20], "count": len(v)}
                       for k, v in self.checks.items()},
        }


def zero_sum(conn: sqlite3.Connection) -> list[str]:
    out = [
        f"currency {r['currency']} postings sum to {r['s']}"
        for r in conn.execute(
            "SELECT currency, SUM(amount) AS s FROM postings GROUP BY currency HAVING s <> 0"
        )
    ]
    out += [
        f"entry {r['entry_id']} {r['currency']} sums to {r['s']}"
        for r in conn.execute(
            "SELECT entry_id, currency, SUM(amount) AS s FROM postings"
            " GROUP BY entry_id, currency HAVING s <> 0"
        )
    ]
    out += [
        f"posting {r['id']} currency {r['currency']} differs from account {r['acc']}"
        for r in conn.execute(
            "SELECT p.id, p.currency, a.currency AS acc FROM postings p"
            " JOIN accounts a ON a.id = p.account_id WHERE p.currency <> a.currency"
        )
    ]
    return out


def sign_rules(conn: sqlite3.Connection) -> list[str]:
    return [
        f"{r['code']} normal balance {r['nb']} is negative"
        for r in conn.execute(
            "SELECT a.code, b.balance * CASE a.normal_side WHEN 'debit' THEN 1 ELSE -1 END AS nb"
            " FROM accounts a JOIN balances b ON b.account_id = a.id"
            " WHERE a.allow_negative = 0 AND nb < 0"
        )
    ]


def cached_balances(conn: sqlite3.Connection) -> list[str]:
    return [
        f"{r['code']} cached {r['cached']} v{r['version']} but postings say {r['real']}"
        f" v{r['n']}"
        for r in conn.execute(
            "SELECT a.code, b.balance AS cached, b.version,"
            " COALESCE(SUM(p.amount), 0) AS real, COUNT(p.id) AS n"
            " FROM accounts a JOIN balances b ON b.account_id = a.id"
            " LEFT JOIN postings p ON p.account_id = a.id"
            " GROUP BY a.id HAVING cached <> real OR b.version <> n"
        )
    ]


def payouts_covered(conn: sqlite3.Connection) -> list[str]:
    """Walk each organizer's history in order: cumulative payouts <= cumulative releases."""
    released: dict[tuple, int] = defaultdict(int)
    paid: dict[tuple, int] = defaultdict(int)
    out = []
    for r in conn.execute(
        "SELECT id, template, metadata FROM journal_entries"
        " WHERE template IN ('escrow_release', 'organizer_payout') ORDER BY id"
    ):
        m = json.loads(r["metadata"])
        key = (m["organizer"], m["currency"])
        if r["template"] == "escrow_release":
            released[key] += m["to_available"]
        else:
            paid[key] += m["amount"]
            if paid[key] > released[key]:
                out.append(
                    f"payout entry {r['id']}: {key} paid {paid[key]} > released {released[key]}"
                )
    reversed_payouts = conn.execute(
        "SELECT COUNT(*) FROM journal_entries r JOIN journal_entries o"
        " ON o.id = r.reverses_entry_id WHERE o.template = 'escrow_release'"
    ).fetchone()[0]
    if reversed_payouts:
        out.append(f"{reversed_payouts} escrow releases were reversed; recheck payouts by hand")
    return out


def holds_within_balance(conn: sqlite3.Connection, now: str) -> list[str]:
    return [
        f"{r['code']} holds {r['held']} exceed balance {r['nb']}"
        for r in conn.execute(
            "SELECT a.code, SUM(h.amount) AS held,"
            " b.balance * CASE a.normal_side WHEN 'debit' THEN 1 ELSE -1 END AS nb"
            " FROM holds h JOIN accounts a ON a.id = h.account_id"
            " JOIN balances b ON b.account_id = a.id"
            " WHERE h.status = 'active' AND h.direction = 'debit' AND h.expires_at > ?"
            " GROUP BY a.id HAVING held > nb",
            (now,),
        )
    ]


def reversals_mirror(conn: sqlite3.Connection) -> list[str]:
    out = []
    for r in conn.execute(
        "SELECT id, reverses_entry_id FROM journal_entries WHERE reverses_entry_id IS NOT NULL"
    ):
        a = conn.execute(
            "SELECT account_id, SUM(amount) FROM postings WHERE entry_id = ? GROUP BY 1 ORDER BY 1",
            (r["id"],),
        ).fetchall()
        b = conn.execute(
            "SELECT account_id, -SUM(amount) FROM postings WHERE entry_id = ? GROUP BY 1"
            " ORDER BY 1",
            (r["reverses_entry_id"],),
        ).fetchall()
        if [tuple(x) for x in a] != [tuple(x) for x in b]:
            out.append(f"reversal {r['id']} does not mirror entry {r['reverses_entry_id']}")
    return out


def check_all(conn: sqlite3.Connection, now: str = "0000") -> Report:
    return Report(
        checks={
            "zero_sum_per_currency": zero_sum(conn),
            "sign_rules": sign_rules(conn),
            "cached_balances_match": cached_balances(conn),
            "payouts_covered_by_released_escrow": payouts_covered(conn),
            "holds_within_balance": holds_within_balance(conn, now),
            "reversals_mirror_originals": reversals_mirror(conn),
        }
    )
