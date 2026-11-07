"""The ledger: validated, append-only journal entries with cached balances.

Write path for a template:

1. Read a snapshot through a View (no lock). The view records the version of
   every account it read.
2. The template turns params plus the snapshot into a Draft of postings.
3. In one `BEGIN IMMEDIATE` transaction: check the idempotency key, check that
   every version read in step 1 is unchanged (optimistic concurrency), insert
   the entry and postings, update the cached balances and versions, enforce
   sign rules and holds, store the idempotency response, commit.

If step 3 finds a moved version it raises ConflictError and `execute` retries
with jittered backoff. The API turns an exhausted retry into HTTP 409.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import sqlite3
import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ledgercore import chart, metrics
from ledgercore.clock import Clock, norm, parse, ts, utcnow
from ledgercore.db import connect, init_db, transaction
from ledgercore.errors import (
    ConflictError,
    IdempotencyMismatch,
    InsufficientFunds,
    InvalidOperation,
    LedgerError,
    NotFound,
    ValidationError,
)
from ledgercore.logs import log
from ledgercore.money import require_currency, require_positive
from ledgercore.templates import TEMPLATES, Draft, Posting, organizer_payout, ticket_purchase
from ledgercore.view import View

logger = logging.getLogger("ledgercore.ledger")

DEFAULT_AUTH_TTL = timedelta(days=7)
DEFAULT_PAYOUT_TTL = timedelta(days=3)


def _hash(req: dict) -> str:
    return hashlib.sha256(json.dumps(req, sort_keys=True, default=str).encode()).hexdigest()


class Ledger:
    def __init__(self, db_path: str | Path, clock: Clock = utcnow, init: bool = True) -> None:
        self.db_path = str(db_path)
        self.conn = connect(self.db_path)
        if init:
            init_db(self.conn)
        self.clock = clock
        # Test hook: runs after the snapshot is read and before the commit.
        self.on_snapshot: Callable[[], None] | None = None

    def close(self) -> None:
        self.conn.close()

    # --- reference data ------------------------------------------------------

    def add_organizer(self, organizer_id: str, name: str, currency: str) -> None:
        require_currency(currency)
        self.conn.execute(
            "INSERT OR IGNORE INTO organizers(id, name, currency) VALUES (?, ?, ?)",
            (organizer_id, name, currency),
        )

    def add_event(
        self, event_id: str, organizer_id: str, name: str, ends_at: str | datetime
    ) -> None:
        row = self.conn.execute(
            "SELECT currency FROM organizers WHERE id = ?", (organizer_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"unknown organizer {organizer_id!r}")
        self.conn.execute(
            "INSERT OR IGNORE INTO events(id, organizer_id, name, currency, ends_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (event_id, organizer_id, name, row["currency"], norm(ends_at)),
        )

    def organizers(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM organizers ORDER BY name")]

    def events(self, organizer_id: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM events", ()
        if organizer_id:
            sql, args = sql + " WHERE organizer_id = ?", (organizer_id,)
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY ends_at", args)]

    # --- core write path -----------------------------------------------------

    def _ensure_account(self, conn: sqlite3.Connection, spec: chart.AccountSpec, now: str) -> int:
        conn.execute(
            "INSERT OR IGNORE INTO accounts(code, type, normal_side, currency, owner, role,"
            " event_id, allow_negative, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (spec.code, spec.type, spec.normal_side, spec.currency, spec.owner, spec.role,
             spec.event_id, int(spec.allow_negative), now),
        )
        acc_id = conn.execute("SELECT id FROM accounts WHERE code = ?", (spec.code,)).fetchone()[0]
        conn.execute(
            "INSERT OR IGNORE INTO balances(account_id, balance, version, updated_at)"
            " VALUES (?, 0, 0, ?)",
            (acc_id, now),
        )
        return acc_id

    def _post(
        self,
        conn: sqlite3.Connection,
        draft: Draft,
        effective_at: str,
        reverses_entry_id: int | None = None,
    ) -> int:
        """Validate and insert one entry. Must run inside a transaction."""
        if len(draft.postings) < 2:
            raise ValidationError("an entry needs at least two postings")
        specs: dict[str, chart.AccountSpec] = {}
        per_ccy: dict[str, int] = defaultdict(int)
        for p in draft.postings:
            if not isinstance(p, Posting):
                raise ValidationError("postings must be Posting objects")
            spec = specs.get(p.account) or chart.spec_for(p.account)
            specs[p.account] = spec
            per_ccy[spec.currency] += p.amount
        unbalanced = {c: s for c, s in per_ccy.items() if s != 0}
        if unbalanced:
            raise ValidationError(f"entry does not balance per currency: {unbalanced}")

        now = self.clock()
        now_s = ts(now)
        ids = {code: self._ensure_account(conn, spec, now_s) for code, spec in specs.items()}
        try:
            cur = conn.execute(
                "INSERT INTO journal_entries(entry_uid, template, external_ref, memo,"
                " effective_at, recorded_at, reverses_entry_id, metadata)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (f"je_{uuid.uuid4().hex[:16]}", draft.template, draft.external_ref, draft.memo,
                 effective_at, now_s, reverses_entry_id,
                 json.dumps(draft.metadata, sort_keys=True)),
            )
        except sqlite3.IntegrityError as exc:
            if "reverses_entry_id" in str(exc):
                raise InvalidOperation("entry was already reversed") from exc
            raise InvalidOperation(
                f"{draft.template} already recorded for {draft.external_ref}"
            ) from exc
        entry_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO postings(entry_id, account_id, amount, currency) VALUES (?, ?, ?, ?)",
            [(entry_id, ids[p.account], p.amount, specs[p.account].currency)
             for p in draft.postings],
        )
        deltas: dict[str, int] = defaultdict(int)
        counts: dict[str, int] = defaultdict(int)
        for p in draft.postings:
            deltas[p.account] += p.amount
            counts[p.account] += 1
        for code, delta in deltas.items():
            spec = specs[code]
            conn.execute(
                "UPDATE balances SET balance = balance + ?, version = version + ?, updated_at = ?"
                " WHERE account_id = ?",
                (delta, counts[code], now_s, ids[code]),
            )
            sign = chart.normal_sign(spec.normal_side)
            normal = conn.execute(
                "SELECT balance FROM balances WHERE account_id = ?", (ids[code],)
            ).fetchone()[0] * sign
            if spec.allow_negative:
                continue
            if normal < 0:
                raise InsufficientFunds(f"{code} would go to {normal}")
            if delta * sign < 0:
                held = conn.execute(
                    "SELECT COALESCE(SUM(amount), 0) FROM holds WHERE account_id = ?"
                    " AND status = 'active' AND direction = 'debit' AND expires_at > ?",
                    (ids[code], now_s),
                ).fetchone()[0]
                if normal < held:
                    raise InsufficientFunds(
                        f"{code} would spend held funds: balance {normal}, held {held}"
                    )
        metrics.inc("ledgercore_entries_posted_total", template=draft.template)
        log(logger, "entry posted", entry_id=entry_id, template=draft.template,
            external_ref=draft.external_ref)
        return entry_id

    def _versions(self, conn: sqlite3.Connection, codes: list[str]) -> dict[str, int]:
        out = {c: 0 for c in codes}
        if codes:
            q = ",".join("?" * len(codes))
            for r in conn.execute(
                f"SELECT a.code, b.version FROM accounts a JOIN balances b ON b.account_id = a.id"
                f" WHERE a.code IN ({q})",
                codes,
            ):
                out[r["code"]] = r["version"]
        return out

    def _check_versions(self, conn: sqlite3.Connection, expected: dict[str, int]) -> None:
        current = self._versions(conn, list(expected))
        moved = {c: (v, current[c]) for c, v in expected.items() if current[c] != v}
        if moved:
            raise ConflictError(f"balances changed since read: {sorted(moved)}")

    def _replay(self, conn: sqlite3.Connection, key: str | None, h: str) -> dict | None:
        if not key:
            return None
        row = conn.execute(
            "SELECT request_hash, response FROM idempotency_keys WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != h:
            raise IdempotencyMismatch("Idempotency-Key was already used with a different request")
        metrics.inc("ledgercore_idempotent_replays_total")
        resp = json.loads(row["response"])
        resp["idempotent_replay"] = True
        return resp

    def _remember(self, conn: sqlite3.Connection, key: str | None, h: str, resp: dict) -> None:
        if key:
            conn.execute(
                "INSERT INTO idempotency_keys(key, request_hash, response, created_at)"
                " VALUES (?, ?, ?, ?)",
                (key, h, json.dumps(resp), ts(self.clock())),
            )

    def execute(
        self,
        template: str,
        params: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        expected_versions: dict[str, int] | None = None,
        max_retries: int = 3,
        effective_at: str | datetime | None = None,
    ) -> dict:
        """Run a named template. Returns the committed entry as a dict."""
        fn = TEMPLATES.get(template)
        if fn is None:
            raise NotFound(f"unknown template {template!r}; known: {sorted(TEMPLATES)}")
        params = dict(params)
        h = _hash({"op": "template", "template": template, "params": params})
        attempt = 0
        while True:
            try:
                return self._execute_once(
                    fn, params, h, idempotency_key, expected_versions, effective_at
                )
            except ConflictError as exc:
                metrics.inc("ledgercore_occ_conflicts_total")
                if attempt >= max_retries:
                    log(logger, "conflict, retries exhausted", logging.WARNING,
                        template=template, attempts=attempt + 1)
                    raise
                attempt += 1
                metrics.inc("ledgercore_occ_retries_total")
                time.sleep(random.uniform(0, exc.retry_after_ms / 1000 * attempt))
            except LedgerError as exc:
                metrics.inc("ledgercore_rejections_total", code=exc.code)
                raise

    def _execute_once(self, fn, params, h, key, expected_versions, effective_at) -> dict:
        replay = self._replay(self.conn, key, h)
        if replay:
            return replay
        now = self.clock()
        view = View(self.conn, now)
        try:
            draft = fn(params, view)
        except InsufficientFunds:
            # A stale snapshot can look short of funds. Retry instead of rejecting.
            current = self._versions(self.conn, list(view.reads))
            if current != view.reads:
                raise ConflictError("balances changed while reading") from None
            raise
        if self.on_snapshot:
            self.on_snapshot()
        eff = norm(effective_at) if effective_at else ts(now)
        with transaction(self.conn) as conn:
            replay = self._replay(conn, key, h)
            if replay:
                return replay
            self._check_versions(conn, {**view.reads, **(expected_versions or {})})
            entry_id = self._post(conn, draft, eff)
            resp = self.entry(entry_id, conn)
            self._remember(conn, key, h, resp)
        return resp

    def post(self, draft: Draft, effective_at: str | datetime | None = None) -> dict:
        """Post a hand-built draft (used by tests and admin tools). Same validation."""
        with transaction(self.conn) as conn:
            eid = self._post(conn, draft, norm(effective_at) if effective_at else ts(self.clock()))
            return self.entry(eid, conn)

    def reverse(self, entry_id: int, reason: str, idempotency_key: str | None = None) -> dict:
        """Correct a mistake by posting the exact opposite entry. Nothing is ever updated."""
        h = _hash({"op": "reverse", "entry_id": entry_id, "reason": reason})
        with transaction(self.conn) as conn:
            replay = self._replay(conn, idempotency_key, h)
            if replay:
                return replay
            orig = conn.execute(
                "SELECT * FROM journal_entries WHERE id = ?", (entry_id,)
            ).fetchone()
            if orig is None:
                raise NotFound(f"no entry {entry_id}")
            if orig["template"] == "reversal":
                raise InvalidOperation("a reversal cannot be reversed; post a new entry")
            rows = conn.execute(
                "SELECT a.code, p.amount FROM postings p JOIN accounts a ON a.id = p.account_id"
                " WHERE p.entry_id = ? ORDER BY p.id",
                (entry_id,),
            ).fetchall()
            draft = Draft(
                "reversal",
                [Posting(r["code"], -r["amount"]) for r in rows],
                memo=f"reversal of {orig['entry_uid']}: {reason}"[:200],
                external_ref=orig["external_ref"],
                metadata={"reverses": orig["entry_uid"], "reason": reason},
            )
            eid = self._post(conn, draft, ts(self.clock()), reverses_entry_id=entry_id)
            resp = self.entry(eid, conn)
            self._remember(conn, idempotency_key, h, resp)
        return resp

    # --- holds ---------------------------------------------------------------

    def _insert_hold(self, conn, code, kind, direction, amount, ref, params, expires_at) -> dict:
        now_s = ts(self.clock())
        acc_id = self._ensure_account(conn, chart.spec_for(code), now_s)
        uid = f"hold_{uuid.uuid4().hex[:12]}"
        params = {**params, "hold_uid": uid}
        conn.execute(
            "INSERT INTO holds(hold_uid, account_id, kind, direction, amount, status,"
            " external_ref, params, created_at, expires_at) VALUES (?, ?, ?, ?, ?, 'active',"
            " ?, ?, ?, ?)",
            (uid, acc_id, kind, direction, amount, ref, json.dumps(params), now_s, expires_at),
        )
        return self.hold(uid, conn)

    def authorize_card(
        self,
        order_id: str,
        organizer: str,
        event_id: str,
        amount: int,
        *,
        expires_at: str | datetime | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        """Card authorized but not captured. Shows as incoming on the event's escrow."""
        amount = require_positive(amount)
        exp = norm(expires_at) if expires_at else ts(self.clock() + DEFAULT_AUTH_TTL)
        h = _hash({"op": "authorize", "order_id": order_id, "organizer": organizer,
                   "event_id": event_id, "amount": amount})
        with transaction(self.conn) as conn:
            replay = self._replay(conn, idempotency_key, h)
            if replay:
                return replay
            view = View(conn, self.clock())
            ev = view.event(event_id)
            if ev["organizer_id"] != organizer:
                raise ValidationError(f"event {event_id} does not belong to {organizer}")
            if view.event_over(event_id):
                raise InvalidOperation(f"event {event_id} is over")
            code = chart.escrow(organizer, event_id, ev["currency"])
            resp = self._insert_hold(
                conn, code, "card_auth", "credit", amount, order_id,
                {"order_id": order_id, "organizer": organizer, "event_id": event_id}, exp,
            )
            self._remember(conn, idempotency_key, h, resp)
        return resp

    def request_payout(
        self,
        organizer: str,
        currency: str,
        amount: int,
        *,
        expires_at: str | datetime | None = None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> dict:
        """Reserve funds for a payout in flight. Available drops now, the posting comes later."""
        amount = require_positive(amount)
        require_currency(currency)
        exp = norm(expires_at) if expires_at else ts(self.clock() + DEFAULT_PAYOUT_TTL)
        code = chart.org(organizer, "available", currency)
        h = _hash({"op": "payout_hold", "organizer": organizer, "currency": currency,
                   "amount": amount})
        try:
            with transaction(self.conn) as conn:
                replay = self._replay(conn, idempotency_key, h)
                if replay:
                    return replay
                if expected_version is not None:
                    self._check_versions(conn, {code: expected_version})
                avail = View(conn, self.clock()).available(code)
                if amount > avail:
                    raise InsufficientFunds(f"payout {amount} exceeds available balance {avail}")
                resp = self._insert_hold(
                    conn, code, "payout", "debit", amount, None,
                    {"organizer": organizer, "currency": currency}, exp,
                )
                self._remember(conn, idempotency_key, h, resp)
            return resp
        except LedgerError as exc:
            name = ("ledgercore_occ_conflicts_total" if isinstance(exc, ConflictError)
                    else "ledgercore_rejections_total")
            metrics.inc(name, **({} if isinstance(exc, ConflictError) else {"code": exc.code}))
            raise

    def capture_hold(
        self, hold_uid: str, amount: int | None = None, idempotency_key: str | None = None
    ) -> dict:
        """Capture all or part of a hold. Any remainder is released in the same step."""
        h = _hash({"op": "capture", "hold": hold_uid, "amount": amount})
        with transaction(self.conn) as conn:
            replay = self._replay(conn, idempotency_key, h)
            if replay:
                return replay
            hold = conn.execute("SELECT * FROM holds WHERE hold_uid = ?", (hold_uid,)).fetchone()
            if hold is None:
                raise NotFound(f"no hold {hold_uid}")
            now = self.clock()
            if hold["status"] != "active":
                raise InvalidOperation(f"hold is {hold['status']}")
            if now >= parse(hold["expires_at"]):
                raise InvalidOperation("hold has expired")
            amt = hold["amount"] if amount is None else require_positive(amount)
            if amt > hold["amount"]:
                raise ValidationError(f"capture {amt} exceeds hold {hold['amount']}")
            conn.execute(
                "UPDATE holds SET status = 'captured', captured_amount = ?, closed_at = ?"
                " WHERE id = ?",
                (amt, ts(now), hold["id"]),
            )
            params = json.loads(hold["params"])
            view = View(conn, now)
            if hold["kind"] == "payout":
                draft = organizer_payout(
                    {**params, "amount": amt, "payout_ref": hold_uid}, view
                )
            else:
                draft = ticket_purchase({**params, "gross": amt}, view)
            eid = self._post(conn, draft, ts(now))
            conn.execute("UPDATE holds SET capture_entry_id = ? WHERE id = ?", (eid, hold["id"]))
            resp = {"hold": self.hold(hold_uid, conn), "entry": self.entry(eid, conn)}
            self._remember(conn, idempotency_key, h, resp)
        return resp

    def release_hold(self, hold_uid: str) -> dict:
        with transaction(self.conn) as conn:
            hold = conn.execute("SELECT * FROM holds WHERE hold_uid = ?", (hold_uid,)).fetchone()
            if hold is None:
                raise NotFound(f"no hold {hold_uid}")
            if hold["status"] == "released":
                return self.hold(hold_uid, conn)  # releasing twice is a no-op
            if hold["status"] != "active":
                raise InvalidOperation(f"hold is {hold['status']}")
            conn.execute(
                "UPDATE holds SET status = 'released', closed_at = ? WHERE id = ?",
                (ts(self.clock()), hold["id"]),
            )
            return self.hold(hold_uid, conn)

    def expire_holds(self) -> int:
        """Sweep holds past their expiry. Returns how many expired."""
        with transaction(self.conn) as conn:
            now_s = ts(self.clock())
            cur = conn.execute(
                "UPDATE holds SET status = 'expired', closed_at = expires_at"
                " WHERE status = 'active' AND expires_at <= ?",
                (now_s,),
            )
            return cur.rowcount

    def hold(self, hold_uid: str, conn: sqlite3.Connection | None = None) -> dict:
        conn = conn or self.conn
        row = conn.execute(
            "SELECT h.*, a.code AS account FROM holds h JOIN accounts a ON a.id = h.account_id"
            " WHERE h.hold_uid = ?",
            (hold_uid,),
        ).fetchone()
        if row is None:
            raise NotFound(f"no hold {hold_uid}")
        return {
            "hold_uid": row["hold_uid"], "account": row["account"], "kind": row["kind"],
            "direction": row["direction"], "amount": row["amount"],
            "captured_amount": row["captured_amount"], "status": row["status"],
            "external_ref": row["external_ref"], "created_at": row["created_at"],
            "expires_at": row["expires_at"], "closed_at": row["closed_at"],
            "capture_entry_id": row["capture_entry_id"],
        }

    # --- reads ---------------------------------------------------------------

    def entry(self, entry_id: int, conn: sqlite3.Connection | None = None) -> dict:
        conn = conn or self.conn
        e = conn.execute("SELECT * FROM journal_entries WHERE id = ?", (entry_id,)).fetchone()
        if e is None:
            raise NotFound(f"no entry {entry_id}")
        postings = [
            {
                "account": r["code"],
                "type": r["type"],
                "currency": r["currency"],
                "amount": r["amount"],
                "side": "debit" if r["amount"] > 0 else "credit",
            }
            for r in conn.execute(
                "SELECT a.code, a.type, p.currency, p.amount FROM postings p"
                " JOIN accounts a ON a.id = p.account_id WHERE p.entry_id = ? ORDER BY p.id",
                (entry_id,),
            )
        ]
        return {
            "id": e["id"],
            "entry_uid": e["entry_uid"],
            "template": e["template"],
            "external_ref": e["external_ref"],
            "memo": e["memo"],
            "effective_at": e["effective_at"],
            "recorded_at": e["recorded_at"],
            "reverses_entry_id": e["reverses_entry_id"],
            "metadata": json.loads(e["metadata"]),
            "postings": postings,
        }

    def entries(
        self, limit: int = 50, before_id: int | None = None, template: str | None = None
    ) -> list[dict]:
        sql, args = "SELECT id FROM journal_entries WHERE 1 = 1", []
        if before_id:
            sql += " AND id < ?"
            args.append(before_id)
        if template:
            sql += " AND template = ?"
            args.append(template)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(limit, 500)))
        return [self.entry(r[0]) for r in self.conn.execute(sql, args).fetchall()]

    def account(self, code: str) -> dict:
        row = self.conn.execute(
            "SELECT a.*, b.balance, b.version FROM accounts a JOIN balances b"
            " ON b.account_id = a.id WHERE a.code = ?",
            (code,),
        ).fetchone()
        if row is None:
            raise NotFound(f"no account {code!r}")
        view = View(self.conn, self.clock())
        normal = row["balance"] * chart.normal_sign(row["normal_side"])
        held = view.debit_holds(code)
        return {
            "code": code, "type": row["type"], "normal_side": row["normal_side"],
            "currency": row["currency"], "owner": row["owner"], "role": row["role"],
            "event_id": row["event_id"], "allow_negative": bool(row["allow_negative"]),
            "balance": normal, "held": held, "available": normal - held,
            "version": row["version"],
        }

    def accounts(self, owner: str | None = None) -> list[dict]:
        sql, args = "SELECT code FROM accounts", ()
        if owner:
            sql, args = sql + " WHERE owner = ?", (owner,)
        return [self.account(r[0]) for r in self.conn.execute(sql + " ORDER BY code", args)]

    def balance(self, code: str, as_of: str | datetime | None = None) -> int:
        """Normal-side balance. With as_of, computed from postings effective at or before it."""
        row = self.conn.execute(
            "SELECT id, normal_side FROM accounts WHERE code = ?", (code,)
        ).fetchone()
        if row is None:
            return 0
        sign = chart.normal_sign(row["normal_side"])
        if as_of is None:
            b = self.conn.execute(
                "SELECT balance FROM balances WHERE account_id = ?", (row["id"],)
            ).fetchone()[0]
            return b * sign
        total = self.conn.execute(
            "SELECT COALESCE(SUM(p.amount), 0) FROM postings p JOIN journal_entries e"
            " ON e.id = p.entry_id WHERE p.account_id = ? AND e.effective_at <= ?",
            (row["id"], norm(as_of)),
        ).fetchone()[0]
        return total * sign

    def _holds_at(self, organizer: str, at: str) -> tuple[int, int]:
        """(pending payouts, incoming card auths) active at time `at`."""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN h.direction = 'debit' THEN h.amount END), 0),"
            "       COALESCE(SUM(CASE WHEN h.direction = 'credit' THEN h.amount END), 0)"
            " FROM holds h JOIN accounts a ON a.id = h.account_id"
            " WHERE a.owner = ? AND h.created_at <= ? AND h.expires_at > ?"
            " AND (h.closed_at IS NULL OR h.closed_at > ?)",
            (organizer, at, at, at),
        ).fetchone()
        return row[0], row[1]

    def organizer_balances(self, organizer: str, as_of: str | datetime | None = None) -> dict:
        org = self.conn.execute("SELECT * FROM organizers WHERE id = ?", (organizer,)).fetchone()
        if org is None:
            raise NotFound(f"unknown organizer {organizer!r}")
        at = norm(as_of) if as_of else ts(self.clock())
        by_role = {"escrow": 0, "available": 0, "receivable": 0}
        for r in self.conn.execute(
            "SELECT a.role, a.normal_side, SUM(p.amount) AS s FROM postings p"
            " JOIN accounts a ON a.id = p.account_id JOIN journal_entries e ON e.id = p.entry_id"
            " WHERE a.owner = ? AND e.effective_at <= ? GROUP BY a.role, a.normal_side",
            (organizer, at),
        ):
            by_role[r["role"]] = r["s"] * chart.normal_sign(r["normal_side"])
        pending, incoming = self._holds_at(organizer, at)
        return {
            "organizer": organizer,
            "name": org["name"],
            "currency": org["currency"],
            "as_of": at,
            "escrow": by_role["escrow"],
            "posted_available": by_role["available"],
            "pending_payouts": pending,
            "available": by_role["available"] - pending,
            "incoming_authorizations": incoming,
            "receivable": by_role["receivable"],
            "net_position": by_role["escrow"] + by_role["available"] - by_role["receivable"],
        }

    def statement(
        self,
        code: str,
        start: str | datetime | None = None,
        end: str | datetime | None = None,
        limit: int = 500,
    ) -> dict:
        """Opening balance, each posting with a running balance, closing balance."""
        acc = self.account(code)
        sign = chart.normal_sign(acc["normal_side"])
        start_s = norm(start) if start else "0000"
        end_s = norm(end) if end else "9999"
        opening = 0
        if start:
            opening = self.balance(code, as_of=ts(parse(start_s) - timedelta(microseconds=1)))
        lines, running = [], opening
        for r in self.conn.execute(
            "SELECT e.id, e.template, e.external_ref, e.memo, e.effective_at, p.amount"
            " FROM postings p JOIN journal_entries e ON e.id = p.entry_id"
            " JOIN accounts a ON a.id = p.account_id"
            " WHERE a.code = ? AND e.effective_at >= ? AND e.effective_at <= ?"
            " ORDER BY e.effective_at, e.id LIMIT ?",
            (code, start_s, end_s, limit),
        ):
            running += r["amount"] * sign
            lines.append({
                "entry_id": r["id"], "effective_at": r["effective_at"],
                "template": r["template"], "external_ref": r["external_ref"],
                "memo": r["memo"], "amount": r["amount"] * sign, "running_balance": running,
            })
        return {"account": acc, "opening_balance": opening, "lines": lines,
                "closing_balance": running}

    def organizer_statement(self, organizer: str, limit: int = 100) -> list[dict]:
        """Newest first: every posting on any of the organizer's accounts."""
        rows = self.conn.execute(
            "SELECT e.id, e.template, e.external_ref, e.memo, e.effective_at, a.role, a.code,"
            " a.normal_side, p.amount FROM postings p JOIN accounts a ON a.id = p.account_id"
            " JOIN journal_entries e ON e.id = p.entry_id WHERE a.owner = ?"
            " ORDER BY e.effective_at DESC, e.id DESC, p.id DESC LIMIT ?",
            (organizer, limit),
        ).fetchall()
        return [
            {"entry_id": r["id"], "effective_at": r["effective_at"], "template": r["template"],
             "external_ref": r["external_ref"], "account": r["code"], "role": r["role"],
             "amount": r["amount"] * chart.normal_sign(r["normal_side"])}
            for r in rows
        ]

    def history(self, organizer: str, max_points: int = 400) -> list[dict]:
        """Balance series at every change: escrow, available, pending, receivable."""
        times = sorted({
            r[0] for r in self.conn.execute(
                "SELECT e.effective_at FROM journal_entries e JOIN postings p ON p.entry_id = e.id"
                " JOIN accounts a ON a.id = p.account_id WHERE a.owner = ?"
                " UNION SELECT h.created_at FROM holds h JOIN accounts a ON a.id = h.account_id"
                " WHERE a.owner = ? UNION SELECT COALESCE(h.closed_at, h.expires_at) FROM holds h"
                " JOIN accounts a ON a.id = h.account_id WHERE a.owner = ?",
                (organizer, organizer, organizer),
            )
        })
        if len(times) > max_points:
            step = len(times) / max_points
            times = [times[int(i * step)] for i in range(max_points)] + [times[-1]]
        out = []
        for t in times:
            b = self.organizer_balances(organizer, as_of=t)
            out.append({k: b[k] for k in (
                "as_of", "escrow", "available", "pending_payouts", "receivable", "net_position")})
        return out
