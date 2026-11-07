"""SQLite connection helpers and schema.

WAL mode lets readers run while one writer commits. Writes use
`BEGIN IMMEDIATE`, which takes the write lock up front, so two writers never
deadlock on a lock upgrade. Ledger tables are append-only: triggers abort any
UPDATE or DELETE on journal entries and postings. The Postgres version of
this schema is in schema/postgres.sql.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id             INTEGER PRIMARY KEY,
    code           TEXT NOT NULL UNIQUE,
    type           TEXT NOT NULL CHECK (type IN ('asset','liability','equity','revenue','expense')),
    normal_side    TEXT NOT NULL CHECK (normal_side IN ('debit','credit')),
    currency       TEXT NOT NULL CHECK (length(currency) = 3),
    owner          TEXT NOT NULL,
    role           TEXT NOT NULL,
    event_id       TEXT,
    allow_negative INTEGER NOT NULL CHECK (allow_negative IN (0, 1)),
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS journal_entries (
    id                INTEGER PRIMARY KEY,
    entry_uid         TEXT NOT NULL UNIQUE,
    template          TEXT NOT NULL,
    external_ref      TEXT,
    memo              TEXT NOT NULL DEFAULT '',
    effective_at      TEXT NOT NULL,
    recorded_at       TEXT NOT NULL,
    reverses_entry_id INTEGER UNIQUE REFERENCES journal_entries(id),
    metadata          TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_entries_effective ON journal_entries(effective_at, id);
CREATE INDEX IF NOT EXISTS ix_entries_ref ON journal_entries(external_ref);
-- One purchase per order, and at most one refund or chargeback per order.
CREATE UNIQUE INDEX IF NOT EXISTS ux_one_purchase ON journal_entries(external_ref)
    WHERE template = 'ticket_purchase';
CREATE UNIQUE INDEX IF NOT EXISTS ux_one_unwind ON journal_entries(external_ref)
    WHERE template IN ('refund_before_payout', 'refund_after_payout', 'chargeback');

CREATE TABLE IF NOT EXISTS postings (
    id         INTEGER PRIMARY KEY,
    entry_id   INTEGER NOT NULL REFERENCES journal_entries(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    amount     INTEGER NOT NULL CHECK (typeof(amount) = 'integer' AND amount <> 0),
    currency   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_postings_account ON postings(account_id, entry_id);
CREATE INDEX IF NOT EXISTS ix_postings_entry ON postings(entry_id);

-- Cached balance per account. Signed: debits positive. Updated in the same
-- transaction as the postings. `version` counts postings and drives
-- optimistic concurrency.
CREATE TABLE IF NOT EXISTS balances (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id),
    balance    INTEGER NOT NULL CHECK (typeof(balance) = 'integer'),
    version    INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

-- Holds reserve money without moving it. A debit hold on an organizer's
-- available account is a payout in flight (pending). A credit hold on an
-- escrow account is a card authorization not yet captured (incoming).
CREATE TABLE IF NOT EXISTS holds (
    id               INTEGER PRIMARY KEY,
    hold_uid         TEXT NOT NULL UNIQUE,
    account_id       INTEGER NOT NULL REFERENCES accounts(id),
    kind             TEXT NOT NULL CHECK (kind IN ('payout', 'card_auth')),
    direction        TEXT NOT NULL CHECK (direction IN ('debit', 'credit')),
    amount           INTEGER NOT NULL CHECK (typeof(amount) = 'integer' AND amount > 0),
    captured_amount  INTEGER NOT NULL DEFAULT 0 CHECK (typeof(captured_amount) = 'integer'),
    status           TEXT NOT NULL CHECK (status IN ('active','captured','released','expired')),
    external_ref     TEXT,
    params           TEXT NOT NULL DEFAULT '{}',
    created_at       TEXT NOT NULL,
    expires_at       TEXT NOT NULL,
    closed_at        TEXT,
    capture_entry_id INTEGER REFERENCES journal_entries(id),
    CHECK (captured_amount >= 0 AND captured_amount <= amount)
);
CREATE INDEX IF NOT EXISTS ix_holds_active ON holds(account_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key          TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    response     TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

-- Reference data (mutable, not part of the ledger).
CREATE TABLE IF NOT EXISTS organizers (
    id       TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    currency TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id           TEXT PRIMARY KEY,
    organizer_id TEXT NOT NULL REFERENCES organizers(id),
    name         TEXT NOT NULL,
    currency     TEXT NOT NULL,
    ends_at      TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS entries_no_update BEFORE UPDATE ON journal_entries
BEGIN SELECT RAISE(ABORT, 'journal entries are immutable; post a reversal'); END;
CREATE TRIGGER IF NOT EXISTS entries_no_delete BEFORE DELETE ON journal_entries
BEGIN SELECT RAISE(ABORT, 'journal entries are immutable; post a reversal'); END;
CREATE TRIGGER IF NOT EXISTS postings_no_update BEFORE UPDATE ON postings
BEGIN SELECT RAISE(ABORT, 'postings are immutable; post a reversal'); END;
CREATE TRIGGER IF NOT EXISTS postings_no_delete BEFORE DELETE ON postings
BEGIN SELECT RAISE(ABORT, 'postings are immutable; post a reversal'); END;
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """Autocommit connection (isolation_level=None); transactions are explicit."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
