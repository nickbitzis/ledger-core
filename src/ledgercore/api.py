"""HTTP API. Every money-moving POST requires an Idempotency-Key header.

Errors share one shape: {"error": code, "message": ..., "retryable": bool}.
A 409 means an optimistic concurrency conflict: retry the same request with
the same Idempotency-Key after the Retry-After hint.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from collections.abc import Iterator
from importlib import resources
from typing import Any

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, StrictInt

from ledgercore import invariants, metrics
from ledgercore.clock import Clock, ts, utcnow
from ledgercore.db import connect, init_db
from ledgercore.errors import (
    ConflictError,
    IdempotencyMismatch,
    InsufficientFunds,
    InvalidOperation,
    LedgerError,
    NotFound,
    ValidationError,
)
from ledgercore.ledger import Ledger
from ledgercore.logs import log, request_id, setup_logging
from ledgercore.templates import TEMPLATES

logger = logging.getLogger("ledgercore.api")

STATUS = {
    ConflictError: 409,
    NotFound: 404,
    ValidationError: 422,
    InsufficientFunds: 422,
    InvalidOperation: 422,
    IdempotencyMismatch: 422,
}


class TemplateIn(BaseModel):
    params: dict[str, Any]
    expected_versions: dict[str, StrictInt] | None = None


class TransferIn(BaseModel):
    debit_account: str
    credit_account: str
    amount: StrictInt = Field(gt=0)
    memo: str = Field(default="manual transfer", max_length=200)


class PayoutIn(BaseModel):
    organizer: str
    currency: str = Field(min_length=3, max_length=3)
    amount: StrictInt = Field(gt=0)
    expected_version: StrictInt | None = None
    expires_at: str | None = None


class AuthIn(BaseModel):
    order_id: str
    organizer: str
    event_id: str
    amount: StrictInt = Field(gt=0)
    expires_at: str | None = None


class CaptureIn(BaseModel):
    amount: StrictInt | None = Field(default=None, gt=0)


class ReverseIn(BaseModel):
    reason: str = Field(min_length=3, max_length=200)


def _err(exc: LedgerError) -> JSONResponse:
    status = next((s for cls, s in STATUS.items() if isinstance(exc, cls)), 400)
    body: dict[str, Any] = {"error": exc.code, "message": str(exc), "retryable": status == 409}
    headers = {}
    if isinstance(exc, ConflictError):
        body["retry_after_ms"] = exc.retry_after_ms
        body["hint"] = "Retry the same request with the same Idempotency-Key."
        headers["Retry-After"] = "1"
    return JSONResponse(body, status_code=status, headers=headers)


def _need_key(key: str | None) -> str:
    if not key:
        raise ValidationError("Idempotency-Key header is required")
    if len(key) > 200:
        raise ValidationError("Idempotency-Key is too long")
    return key


def _created(resp: dict) -> JSONResponse:
    replay = bool(resp.pop("idempotent_replay", False))
    return JSONResponse(resp, status_code=200 if replay else 201,
                        headers={"Idempotent-Replayed": "true"} if replay else {})


def create_app(db_path: str | None = None, clock: Clock = utcnow) -> FastAPI:
    db_path = db_path or os.environ.get("LEDGERCORE_DB_PATH", "data/ledgercore.db")
    setup_logging(os.environ.get("LEDGERCORE_LOG_LEVEL", "INFO"))
    boot = connect(db_path)
    init_db(boot)
    boot.close()
    app = FastAPI(title="ledgercore", version="1.0.0")

    def get_ledger() -> Iterator[Ledger]:
        led = Ledger(db_path, clock, init=False)
        try:
            yield led
        finally:
            led.close()

    @app.exception_handler(LedgerError)
    async def ledger_error(_: Request, exc: LedgerError):
        return _err(exc)

    @app.middleware("http")
    async def observe(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        token = request_id.set(rid)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            request_id.reset(token)
        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")
        dt = time.perf_counter() - start
        metrics.inc("ledgercore_http_requests_total", method=request.method, route=path,
                    status=str(response.status_code))
        metrics.observe("ledgercore_http_request_seconds", dt, route=path)
        token = request_id.set(rid)
        log(logger, "request", method=request.method, route=path, status=response.status_code,
            ms=round(dt * 1000, 2))
        request_id.reset(token)
        response.headers["x-request-id"] = rid
        return response

    # --- writes --------------------------------------------------------------

    @app.post("/v1/templates/{name}", status_code=201)
    def run_template(name: str, body: TemplateIn,
                     idempotency_key: str | None = Header(default=None),
                     led: Ledger = Depends(get_ledger)):
        resp = led.execute(name, body.params, idempotency_key=_need_key(idempotency_key),
                           expected_versions=body.expected_versions)
        return _created(resp)

    @app.post("/v1/transfers", status_code=201)
    def transfer(body: TransferIn, idempotency_key: str | None = Header(default=None),
                 led: Ledger = Depends(get_ledger)):
        resp = led.execute("manual_transfer", body.model_dump(),
                           idempotency_key=_need_key(idempotency_key))
        return _created(resp)

    @app.post("/v1/payouts", status_code=201)
    def payout(body: PayoutIn, idempotency_key: str | None = Header(default=None),
               led: Ledger = Depends(get_ledger)):
        resp = led.request_payout(
            body.organizer, body.currency, body.amount, expires_at=body.expires_at,
            idempotency_key=_need_key(idempotency_key), expected_version=body.expected_version)
        return _created(resp)

    @app.post("/v1/authorizations", status_code=201)
    def authorize(body: AuthIn, idempotency_key: str | None = Header(default=None),
                  led: Ledger = Depends(get_ledger)):
        resp = led.authorize_card(body.order_id, body.organizer, body.event_id, body.amount,
                                  expires_at=body.expires_at,
                                  idempotency_key=_need_key(idempotency_key))
        return _created(resp)

    @app.post("/v1/holds/{hold_uid}/capture", status_code=201)
    def capture(hold_uid: str, body: CaptureIn,
                idempotency_key: str | None = Header(default=None),
                led: Ledger = Depends(get_ledger)):
        resp = led.capture_hold(hold_uid, body.amount, idempotency_key=_need_key(idempotency_key))
        return _created(resp)

    @app.post("/v1/holds/{hold_uid}/release")
    def release(hold_uid: str, led: Ledger = Depends(get_ledger)):
        return led.release_hold(hold_uid)

    @app.post("/v1/holds/expire")
    def expire(led: Ledger = Depends(get_ledger)):
        return {"expired": led.expire_holds()}

    @app.post("/v1/entries/{entry_id}/reverse", status_code=201)
    def reverse(entry_id: int, body: ReverseIn,
                idempotency_key: str | None = Header(default=None),
                led: Ledger = Depends(get_ledger)):
        return _created(led.reverse(entry_id, body.reason, _need_key(idempotency_key)))

    # --- reads ---------------------------------------------------------------

    @app.get("/v1/templates")
    def list_templates():
        return [{"name": n, "doc": (f.__doc__ or "").strip().splitlines()[0]}
                for n, f in TEMPLATES.items()]

    @app.get("/v1/entries")
    def list_entries(limit: int = Query(50, ge=1, le=500), before_id: int | None = None,
                     template: str | None = None, led: Ledger = Depends(get_ledger)):
        items = led.entries(limit=limit, before_id=before_id, template=template)
        return {"entries": items, "next_before_id": items[-1]["id"] if items else None}

    @app.get("/v1/entries/{entry_id}")
    def get_entry(entry_id: int, led: Ledger = Depends(get_ledger)):
        return led.entry(entry_id)

    @app.get("/v1/accounts")
    def list_accounts(owner: str | None = None, led: Ledger = Depends(get_ledger)):
        return led.accounts(owner)

    @app.get("/v1/accounts/{code}")
    def get_account(code: str, led: Ledger = Depends(get_ledger)):
        return led.account(code)

    @app.get("/v1/accounts/{code}/balance")
    def get_balance(code: str, as_of: str | None = None, led: Ledger = Depends(get_ledger)):
        acc = led.account(code)
        if as_of:
            return {"code": code, "as_of": as_of, "balance": led.balance(code, as_of),
                    "currency": acc["currency"]}
        return {k: acc[k] for k in ("code", "currency", "balance", "held", "available",
                                    "version")}

    @app.get("/v1/accounts/{code}/statement")
    def get_statement(code: str, start: str | None = None, end: str | None = None,
                      led: Ledger = Depends(get_ledger)):
        return led.statement(code, start, end)

    @app.get("/v1/organizers")
    def list_organizers(led: Ledger = Depends(get_ledger)):
        return [{**o, "events": led.events(o["id"])} for o in led.organizers()]

    @app.get("/v1/organizers/{organizer}/balances")
    def org_balances(organizer: str, as_of: str | None = None,
                     led: Ledger = Depends(get_ledger)):
        return led.organizer_balances(organizer, as_of)

    @app.get("/v1/organizers/{organizer}/history")
    def org_history(organizer: str, led: Ledger = Depends(get_ledger)):
        led.organizer_balances(organizer)
        return led.history(organizer)

    @app.get("/v1/organizers/{organizer}/statement")
    def org_statement(organizer: str, limit: int = Query(100, ge=1, le=1000),
                      led: Ledger = Depends(get_ledger)):
        led.organizer_balances(organizer)
        return led.organizer_statement(organizer, limit)

    @app.get("/v1/invariants")
    def check(led: Ledger = Depends(get_ledger)):
        return invariants.check_all(led.conn, ts(clock())).as_dict()

    # --- ops -----------------------------------------------------------------

    @app.get("/healthz")
    def healthz(led: Ledger = Depends(get_ledger)):
        led.conn.execute("SELECT 1").fetchone()
        return {"status": "ok"}

    @app.get("/metrics", response_class=PlainTextResponse)
    def prom(led: Ledger = Depends(get_ledger)):
        c = led.conn
        gauges: list[tuple[str, str, dict[str, str], float]] = []
        for r in c.execute("SELECT template, COUNT(*) FROM journal_entries GROUP BY 1 ORDER BY 1"):
            gauges.append(("ledgercore_journal_entries", "Journal entries stored, by template.",
                           {"template": r[0]}, r[1]))
        for r in c.execute("SELECT kind, COUNT(*) FROM holds WHERE status = 'active' GROUP BY 1"):
            gauges.append(("ledgercore_active_holds", "Active holds by kind.", {"kind": r[0]},
                           r[1]))
        rep = invariants.check_all(c, ts(clock()))
        for name, violations in rep.checks.items():
            gauges.append(("ledgercore_invariant_ok", "1 if the invariant holds.",
                           {"check": name}, 0 if violations else 1))
        return metrics.render(gauges)

    @app.get("/", response_class=HTMLResponse)
    def index():
        return resources.files("ledgercore").joinpath("static/index.html").read_text()

    return app
