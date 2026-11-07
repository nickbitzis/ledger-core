"""Daily reconciliation of the ledger against the card processor's settlement file.

The generator builds a synthetic settlement CSV for one day from the ledger's
own captures and refunds, then plants breaks. The processor's daily cutoff is
23:55 UTC, so transactions in the last five minutes of a day settle in the
next day's file. That is the timing difference across midnight.

The reconciliation job matches in two passes:

1. Exact: processor reference and type. Then compare amount and fee.
2. Fuzzy: lines with no usable reference match an unmatched ledger item with
   the same currency, type, and gross within a two-hour window.

Anything left is classified as a break.
"""

from __future__ import annotations

import csv
import io
import json
import random
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from ledgercore.clock import parse, ts
from ledgercore.money import fmt

CUTOFF = time(23, 55)
FUZZY_WINDOW = timedelta(hours=2)
FIELDS = ["line_id", "settlement_date", "processor_ref", "type", "currency",
          "gross_minor", "fee_minor", "net_minor", "txn_at"]

MATCHED = ("matched", "matched_fuzzy")
INFO = ("timing_difference",)
BREAKS = ("missing_settlement", "amount_mismatch", "duplicate", "fee_difference",
          "unexpected_line")


@dataclass(frozen=True)
class LedgerItem:
    entry_id: int
    processor_ref: str
    kind: str  # CAPTURE or REFUND
    currency: str
    gross: int
    expected_fee: int
    at: datetime


@dataclass
class Line:
    line_id: str
    settlement_date: str
    processor_ref: str
    type: str
    currency: str
    gross_minor: int
    fee_minor: int
    net_minor: int
    txn_at: str


@dataclass
class Result:
    status: str
    processor_ref: str
    kind: str
    currency: str
    ledger_gross: int | None = None
    settled_gross: int | None = None
    expected_fee: int | None = None
    settled_fee: int | None = None
    note: str = ""


@dataclass
class ReconReport:
    day: str
    results: list[Result] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        c = Counter(r.status for r in self.results)
        return {k: c.get(k, 0) for k in (*MATCHED, *INFO, *BREAKS)}

    @property
    def breaks(self) -> list[Result]:
        return [r for r in self.results if r.status in BREAKS]

    def as_dict(self) -> dict:
        return {"day": self.day, "counts": self.counts,
                "results": [asdict(r) for r in self.results]}

    def to_markdown(self) -> str:
        c = self.counts
        lines = [
            f"# Reconciliation report for {self.day}",
            "",
            "| Status | Count |",
            "|---|---:|",
            *[f"| {k} | {v} |" for k, v in c.items()],
            "",
            f"Breaks needing action: {len(self.breaks)}. "
            f"Timing differences (clear on their own): {c['timing_difference']}.",
            "",
        ]
        shown = [r for r in self.results if r.status not in ("matched",)]
        if shown:
            lines += [
                "| Status | Processor ref | Type | Ledger | Settled | Fee exp. | Fee settled"
                " | Note |",
                "|---|---|---|---:|---:|---:|---:|---|",
            ]
            def m(v: int | None, ccy: str) -> str:
                return "" if v is None else fmt(v, ccy)

            for r in sorted(shown, key=lambda r: (r.status, r.processor_ref)):
                c = r.currency
                lines.append(
                    f"| {r.status} | {r.processor_ref or '(blank)'} | {r.kind} |"
                    f" {m(r.ledger_gross, c)} | {m(r.settled_gross, c)} |"
                    f" {m(r.expected_fee, c)} | {m(r.settled_fee, c)} | {r.note} |"
                )
        return "\n".join(lines) + "\n"


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0), tzinfo=UTC)
    return start, start + timedelta(days=1)


def ledger_items(conn: sqlite3.Connection, start: datetime, end: datetime) -> list[LedgerItem]:
    """Captures and refunds effective in [start, end) that were not reversed."""
    items = []
    for r in conn.execute(
        "SELECT e.id, e.template, e.effective_at, e.metadata FROM journal_entries e"
        " WHERE e.template IN ('ticket_purchase', 'refund_before_payout', 'refund_after_payout')"
        " AND e.effective_at >= ? AND e.effective_at < ?"
        " AND NOT EXISTS (SELECT 1 FROM journal_entries r WHERE r.reverses_entry_id = e.id)"
        " ORDER BY e.effective_at, e.id",
        (ts(start), ts(end)),
    ):
        m = json.loads(r["metadata"])
        capture = r["template"] == "ticket_purchase"
        items.append(LedgerItem(
            entry_id=r["id"],
            processor_ref=m["processor_ref"],
            kind="CAPTURE" if capture else "REFUND",
            currency=m["currency"],
            gross=m["gross"],
            expected_fee=m["expected_processor_fee"] if capture else 0,
            at=parse(r["effective_at"]),
        ))
    return items


def _settles_on(at: datetime) -> date:
    return at.date() + timedelta(days=1) if at.time() > CUTOFF else at.date()


def generate_settlement(
    conn: sqlite3.Connection, day: date, seed: int = 7, plant: bool = True
) -> tuple[list[Line], dict[str, list[str]]]:
    """Build the processor's file for `day` and plant one break of each kind.

    Returns the lines and a manifest of planted breaks keyed by expected status.
    """
    rng = random.Random(f"{seed}:{day.isoformat()}")
    start, end = _day_bounds(day)
    window = ledger_items(conn, start - timedelta(days=1), end)
    in_file = [i for i in window if _settles_on(i.at) == day]
    lines: list[Line] = []
    for n, i in enumerate(in_file):
        fee = i.expected_fee
        lines.append(Line(
            line_id=f"L{day:%Y%m%d}-{n + 1:05d}",
            settlement_date=day.isoformat(),
            processor_ref=i.processor_ref,
            type=i.kind,
            currency=i.currency,
            gross_minor=i.gross,
            fee_minor=fee,
            net_minor=(i.gross - fee) if i.kind == "CAPTURE" else -i.gross,
            txn_at=ts(i.at + timedelta(seconds=rng.randint(0, 3))),
        ))
    manifest: dict[str, list[str]] = {k: [] for k in (*INFO, *BREAKS, "matched_fuzzy")}
    manifest["timing_difference"] = sorted(
        [i.processor_ref for i in in_file if i.at.date() < day]
        + [i.processor_ref for i in window if i.at.date() == day and _settles_on(i.at) > day]
    )
    if not plant:
        return lines, manifest

    captures = [ln for ln in lines if ln.type == "CAPTURE" and ln.txn_at[:10] == day.isoformat()]
    picks = rng.sample(captures, k=min(5, len(captures)))
    if len(picks) == 5:
        missing, mismatch, dup, feediff, noref = picks
        lines.remove(missing)
        manifest["missing_settlement"].append(missing.processor_ref)
        mismatch.gross_minor -= rng.choice([100, 250, 1000])
        mismatch.net_minor = mismatch.gross_minor - mismatch.fee_minor
        manifest["amount_mismatch"].append(mismatch.processor_ref)
        lines.append(Line(**{**asdict(dup), "line_id": dup.line_id + "-DUP"}))
        manifest["duplicate"].append(dup.processor_ref)
        feediff.fee_minor += rng.randint(7, 25)
        feediff.net_minor = feediff.gross_minor - feediff.fee_minor
        manifest["fee_difference"].append(feediff.processor_ref)
        manifest["matched_fuzzy"].append(noref.processor_ref)
        noref.processor_ref = ""
    stray = Line(
        line_id=f"L{day:%Y%m%d}-99999", settlement_date=day.isoformat(),
        processor_ref=f"psp_unknown_{rng.randint(1000, 9999)}", type="CAPTURE",
        currency="USD", gross_minor=4321, fee_minor=155, net_minor=4166,
        txn_at=ts(start + timedelta(hours=13, minutes=rng.randint(0, 59))),
    )
    lines.append(stray)
    manifest["unexpected_line"].append(stray.processor_ref)
    return lines, manifest


def write_csv(lines: list[Line]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS, lineterminator="\n")
    w.writeheader()
    for ln in lines:
        w.writerow(asdict(ln))
    return buf.getvalue()


def read_csv(text: str) -> list[Line]:
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        out.append(Line(
            line_id=row["line_id"], settlement_date=row["settlement_date"],
            processor_ref=row["processor_ref"].strip(), type=row["type"],
            currency=row["currency"], gross_minor=int(row["gross_minor"]),
            fee_minor=int(row["fee_minor"]), net_minor=int(row["net_minor"]),
            txn_at=row["txn_at"],
        ))
    return out


def reconcile(conn: sqlite3.Connection, day: date, lines: list[Line]) -> ReconReport:
    start, end = _day_bounds(day)
    today = ledger_items(conn, start, end)
    near = ledger_items(conn, start - timedelta(days=1), end + timedelta(days=1))
    by_key_today = {(i.processor_ref, i.kind): i for i in today}
    by_key_near = {(i.processor_ref, i.kind): i for i in near}
    report = ReconReport(day=day.isoformat())
    used: set[int] = set()
    seen: set[tuple[str, str]] = set()
    unref: list[Line] = []

    # Pass 1: exact on reference.
    for ln in sorted(lines, key=lambda x: x.line_id):
        key = (ln.processor_ref, ln.type)
        base = dict(processor_ref=ln.processor_ref, kind=ln.type, currency=ln.currency,
                    settled_gross=ln.gross_minor, settled_fee=ln.fee_minor)
        if ln.processor_ref and key in seen:
            report.results.append(Result("duplicate", **base, note=f"repeat line {ln.line_id}"))
            continue
        if ln.processor_ref:
            seen.add(key)
        item = by_key_today.get(key)
        if item is not None:
            used.add(item.entry_id)
            full = {**base, "ledger_gross": item.gross, "expected_fee": item.expected_fee}
            if ln.currency != item.currency or ln.gross_minor != item.gross:
                diff = ln.gross_minor - item.gross
                report.results.append(Result("amount_mismatch", **full, note=f"diff {diff:+d}"))
            elif ln.fee_minor != item.expected_fee:
                diff = ln.fee_minor - item.expected_fee
                report.results.append(Result("fee_difference", **full, note=f"fee {diff:+d}"))
            else:
                report.results.append(Result("matched", **full))
            continue
        item = by_key_near.get(key)
        if item is not None:
            report.results.append(Result(
                "timing_difference", **base, ledger_gross=item.gross,
                expected_fee=item.expected_fee,
                note=f"ledger {item.at:%Y-%m-%d %H:%M}, settled {ln.settlement_date}",
            ))
            continue
        unref.append(ln)

    # Pass 2: fuzzy on amount and time window.
    for ln in unref:
        at = parse(ln.txn_at)
        cands = [
            i for i in today
            if i.entry_id not in used and i.currency == ln.currency and i.kind == ln.type
            and i.gross == ln.gross_minor and abs(i.at - at) <= FUZZY_WINDOW
        ]
        base = dict(kind=ln.type, currency=ln.currency, settled_gross=ln.gross_minor,
                    settled_fee=ln.fee_minor)
        if cands:
            best = min(cands, key=lambda i: abs(i.at - at))
            used.add(best.entry_id)
            report.results.append(Result(
                "matched_fuzzy", processor_ref=best.processor_ref, **base,
                ledger_gross=best.gross, expected_fee=best.expected_fee,
                note=f"line {ln.line_id} had ref {ln.processor_ref or '(blank)'};"
                     f" {len(cands)} candidate(s)",
            ))
        else:
            report.results.append(Result(
                "unexpected_line", processor_ref=ln.processor_ref, **base,
                note=f"line {ln.line_id} has no ledger entry",
            ))

    # Ledger items that never showed up.
    for i in today:
        if i.entry_id in used:
            continue
        status = "timing_difference" if _settles_on(i.at) > day else "missing_settlement"
        note = "after 23:55 cutoff, expected in next file" if status != "missing_settlement" \
            else f"ledger entry {i.entry_id} at {i.at:%H:%M}, no settlement line"
        report.results.append(Result(
            status, processor_ref=i.processor_ref, kind=i.kind, currency=i.currency,
            ledger_gross=i.gross, expected_fee=i.expected_fee, note=note,
        ))
    return report


def score(report: ReconReport, manifest: dict[str, list[str]]) -> dict[str, dict[str, int]]:
    """Planted vs detected per class. Detected means same ref with the expected status."""
    got: dict[str, set[str]] = {}
    for r in report.results:
        got.setdefault(r.status, set()).add(r.processor_ref)
    out = {}
    for status, refs in manifest.items():
        found = sum(1 for ref in refs if ref in got.get(status, set()))
        flagged = len(got.get(status, set()))
        out[status] = {"planted": len(refs), "detected": found, "flagged": flagged}
    return out
