-- Postgres version of the ledgercore schema (src/ledgercore/db.py is the SQLite twin).
-- Amounts are BIGINT minor units. Ledger tables are append-only, enforced by triggers
-- and by granting the application role INSERT and SELECT only on them.
--
-- Concurrency in production: run money-moving transactions at SERIALIZABLE isolation and
-- retry on SQLSTATE 40001, or at READ COMMITTED with `SELECT ... FOR UPDATE` on the
-- balances rows an operation reads, locked in account id order to avoid deadlocks.
-- The `version` column supports the optimistic path:
--   UPDATE balances SET balance = balance + $1, version = version + 1
--   WHERE account_id = $2 AND version = $3;   -- 0 rows updated means conflict, retry

CREATE TYPE account_type AS ENUM ('asset', 'liability', 'equity', 'revenue', 'expense');
CREATE TYPE normal_side AS ENUM ('debit', 'credit');

CREATE TABLE accounts (
    id             BIGSERIAL PRIMARY KEY,
    code           TEXT NOT NULL UNIQUE,
    type           account_type NOT NULL,
    normal_side    normal_side NOT NULL,
    currency       CHAR(3) NOT NULL,
    owner          TEXT NOT NULL,
    role           TEXT NOT NULL,
    event_id       TEXT,
    allow_negative BOOLEAN NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((type IN ('asset', 'expense')) = (normal_side = 'debit'))
);

CREATE TABLE journal_entries (
    id                BIGSERIAL PRIMARY KEY,
    entry_uid         TEXT NOT NULL UNIQUE,
    template          TEXT NOT NULL,
    external_ref      TEXT,
    memo              TEXT NOT NULL DEFAULT '',
    effective_at      TIMESTAMPTZ NOT NULL,
    recorded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    reverses_entry_id BIGINT UNIQUE REFERENCES journal_entries(id),
    metadata          JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX ix_entries_effective ON journal_entries (effective_at, id);
CREATE INDEX ix_entries_ref ON journal_entries (external_ref);
CREATE UNIQUE INDEX ux_one_purchase ON journal_entries (external_ref)
    WHERE template = 'ticket_purchase';
CREATE UNIQUE INDEX ux_one_unwind ON journal_entries (external_ref)
    WHERE template IN ('refund_before_payout', 'refund_after_payout', 'chargeback');

CREATE TABLE postings (
    id         BIGSERIAL PRIMARY KEY,
    entry_id   BIGINT NOT NULL REFERENCES journal_entries(id),
    account_id BIGINT NOT NULL REFERENCES accounts(id),
    amount     BIGINT NOT NULL CHECK (amount <> 0),   -- positive debit, negative credit
    currency   CHAR(3) NOT NULL
);
CREATE INDEX ix_postings_account ON postings (account_id, entry_id);
CREATE INDEX ix_postings_entry ON postings (entry_id);

CREATE TABLE balances (
    account_id BIGINT PRIMARY KEY REFERENCES accounts(id),
    balance    BIGINT NOT NULL,
    version    BIGINT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE holds (
    id               BIGSERIAL PRIMARY KEY,
    hold_uid         TEXT NOT NULL UNIQUE,
    account_id       BIGINT NOT NULL REFERENCES accounts(id),
    kind             TEXT NOT NULL CHECK (kind IN ('payout', 'card_auth')),
    direction        normal_side NOT NULL,
    amount           BIGINT NOT NULL CHECK (amount > 0),
    captured_amount  BIGINT NOT NULL DEFAULT 0 CHECK (captured_amount BETWEEN 0 AND amount),
    status           TEXT NOT NULL CHECK (status IN ('active', 'captured', 'released', 'expired')),
    external_ref     TEXT,
    params           JSONB NOT NULL DEFAULT '{}',
    created_at       TIMESTAMPTZ NOT NULL,
    expires_at       TIMESTAMPTZ NOT NULL,
    closed_at        TIMESTAMPTZ,
    capture_entry_id BIGINT REFERENCES journal_entries(id)
);
CREATE INDEX ix_holds_active ON holds (account_id) WHERE status = 'active';

CREATE TABLE idempotency_keys (
    key          TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    response     JSONB NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE organizers (
    id       TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    currency CHAR(3) NOT NULL
);
CREATE TABLE events (
    id           TEXT PRIMARY KEY,
    organizer_id TEXT NOT NULL REFERENCES organizers(id),
    name         TEXT NOT NULL,
    currency     CHAR(3) NOT NULL,
    ends_at      TIMESTAMPTZ NOT NULL
);

-- Append-only ledger.
CREATE FUNCTION forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is immutable; post a reversal', TG_TABLE_NAME;
END $$;
CREATE TRIGGER entries_immutable BEFORE UPDATE OR DELETE ON journal_entries
    FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER postings_immutable BEFORE UPDATE OR DELETE ON postings
    FOR EACH ROW EXECUTE FUNCTION forbid_change();

-- Every entry balances per currency, checked at COMMIT after all postings are in.
CREATE FUNCTION check_entry_balanced() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM postings WHERE entry_id = NEW.entry_id
        GROUP BY currency HAVING SUM(amount) <> 0
    ) THEN
        RAISE EXCEPTION 'entry % does not balance per currency', NEW.entry_id;
    END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER postings_balanced AFTER INSERT ON postings
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION check_entry_balanced();
