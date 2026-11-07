"""Read access for templates, with read-set tracking for optimistic concurrency.

A template reads balances through a View. Every account it reads is recorded
with the version it saw. Before the postings commit, the ledger checks that
none of those versions moved. If one did, the snapshot was stale, the
transaction aborts with ConflictError, and the caller retries.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from ledgercore import chart
from ledgercore.clock import parse, ts
from ledgercore.errors import NotFound


@dataclass(frozen=True)
class Order:
    order_id: str
    organizer: str
    event_id: str
    currency: str
    gross: int
    platform_fee: int
    entry_id: int
    unwound: bool


class View:
    def __init__(self, conn: sqlite3.Connection, now: datetime) -> None:
        self.conn = conn
        self.now = now
        self.reads: dict[str, int] = {}

    def _row(self, code: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT a.normal_side, b.balance, b.version FROM accounts a"
            " JOIN balances b ON b.account_id = a.id WHERE a.code = ?",
            (code,),
        ).fetchone()

    def balance(self, code: str) -> int:
        """Normal-side balance (positive is healthy). Missing accounts read as 0."""
        row = self._row(code)
        if row is None:
            self.reads.setdefault(code, 0)
            return 0
        self.reads.setdefault(code, row["version"])
        return row["balance"] * chart.normal_sign(row["normal_side"])

    def debit_holds(self, code: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(h.amount), 0) FROM holds h JOIN accounts a ON a.id = h.account_id"
            " WHERE a.code = ? AND h.status = 'active' AND h.direction = 'debit'"
            " AND h.expires_at > ?",
            (code, ts(self.now)),
        ).fetchone()
        return row[0]

    def available(self, code: str) -> int:
        """Balance minus funds reserved by active debit holds (payouts in flight)."""
        return self.balance(code) - self.debit_holds(code)

    def event(self, event_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        if row is None:
            raise NotFound(f"unknown event {event_id!r}")
        return row

    def event_over(self, event_id: str) -> bool:
        return self.now >= parse(self.event(event_id)["ends_at"])

    def order(self, order_id: str) -> Order:
        row = self.conn.execute(
            "SELECT e.id, e.metadata FROM journal_entries e WHERE e.template = 'ticket_purchase'"
            " AND e.external_ref = ? AND NOT EXISTS"
            " (SELECT 1 FROM journal_entries r WHERE r.reverses_entry_id = e.id)",
            (order_id,),
        ).fetchone()
        if row is None:
            raise NotFound(f"no captured purchase for order {order_id!r}")
        meta = json.loads(row["metadata"])
        unwound = self.conn.execute(
            "SELECT 1 FROM journal_entries WHERE external_ref = ? AND template IN"
            " ('refund_before_payout', 'refund_after_payout', 'chargeback')",
            (order_id,),
        ).fetchone()
        return Order(
            order_id=order_id,
            organizer=meta["organizer"],
            event_id=meta["event_id"],
            currency=meta["currency"],
            gross=meta["gross"],
            platform_fee=meta["platform_fee"],
            entry_id=row["id"],
            unwound=unwound is not None,
        )
