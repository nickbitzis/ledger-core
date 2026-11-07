"""Command line: demo data, API server, reconciliation, invariants, stress test."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from ledgercore import invariants, metrics, recon
from ledgercore.clock import ts, utcnow
from ledgercore.db import connect, init_db
from ledgercore.logs import setup_logging

DEFAULT_DB = os.environ.get("LEDGERCORE_DB_PATH", "data/ledgercore.db")


def _quiet() -> None:
    setup_logging("ERROR")


def cmd_demo(a: argparse.Namespace) -> int:
    from ledgercore.simulate import run_demo

    _quiet()
    path = Path(a.db)
    if a.fresh:
        for suffix in ("", "-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)
    res = run_demo(path, seed=a.seed, days=a.days, orders_per_day=a.orders_per_day)
    print(json.dumps({"db": res.db_path, "orders": res.orders, "counts": res.counts,
                      "rejected": res.rejected, "invariants_ok": res.invariants_ok,
                      "simulated_until": res.end}, indent=2))
    return 0 if res.invariants_ok else 1


def cmd_api(a: argparse.Namespace) -> int:
    import uvicorn

    from ledgercore.api import create_app

    uvicorn.run(create_app(a.db), host=a.host, port=a.port, log_level="warning")
    return 0


def _day(s: str | None) -> date:
    if not s or s == "yesterday":
        return utcnow().date() - timedelta(days=1)
    return date.fromisoformat(s)


def cmd_gen(a: argparse.Namespace) -> int:
    conn = connect(a.db)
    lines, manifest = recon.generate_settlement(conn, _day(a.day), seed=a.seed,
                                                plant=not a.clean)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(recon.write_csv(lines))
    Path(a.out).with_suffix(".manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"wrote {len(lines)} lines to {a.out}")
    return 0


def cmd_recon(a: argparse.Namespace) -> int:
    conn = connect(a.db)
    day = _day(a.day)
    if a.settlement:
        lines = recon.read_csv(Path(a.settlement).read_text())
    else:
        lines, _ = recon.generate_settlement(conn, day, seed=a.seed)
    report = recon.reconcile(conn, day, lines)
    text = json.dumps(report.as_dict(), indent=2) if a.json else report.to_markdown()
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(text)
    print(text)
    return 0


def cmd_recon_eval(a: argparse.Namespace) -> int:
    """Plant breaks on every day in the database and score detection."""
    conn = connect(a.db)
    days = sorted({r[0][:10] for r in conn.execute(
        "SELECT effective_at FROM journal_entries WHERE template = 'ticket_purchase'")})
    totals: dict[str, dict[str, int]] = {}
    for d in days:
        day = date.fromisoformat(d)
        lines, manifest = recon.generate_settlement(conn, day, seed=a.seed)
        report = recon.reconcile(conn, day, lines)
        for status, s in recon.score(report, manifest).items():
            t = totals.setdefault(status, {"planted": 0, "detected": 0, "flagged": 0})
            for k in t:
                t[k] += s[k]
    print(f"Days: {len(days)}")
    print("| Class | Planted | Detected | Flagged | Recall | Precision |")
    print("|---|---:|---:|---:|---:|---:|")
    for status, t in totals.items():
        recall = t["detected"] / t["planted"] if t["planted"] else 1.0
        precision = t["detected"] / t["flagged"] if t["flagged"] else 1.0
        print(f"| {status} | {t['planted']} | {t['detected']} | {t['flagged']} |"
              f" {recall:.0%} | {precision:.0%} |")
    return 0


def cmd_invariants(a: argparse.Namespace) -> int:
    conn = connect(a.db)
    init_db(conn)
    rep = invariants.check_all(conn, ts(utcnow()))
    print(json.dumps(rep.as_dict(), indent=2))
    return 0 if rep.ok else 1


def cmd_stress(a: argparse.Namespace) -> int:
    from ledgercore.stress import run_stress

    _quiet()
    path = Path(a.db)
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    res = run_stress(path, threads=a.threads, ops=a.ops, seed=a.seed)
    print(json.dumps({
        "threads": res.threads, "ops_per_thread": res.ops_per_thread,
        "occ_conflicts_detected": int(metrics.total("ledgercore_occ_conflicts_total")),
        "occ_internal_retries": int(metrics.total("ledgercore_occ_retries_total")),
        "conflicts_seen_by_clients": res.conflicts_seen,
        "duplicate_sends": res.duplicate_sends,
        "purchases_acknowledged": res.purchases_ok,
        "purchase_entries_in_ledger": res.purchase_entries,
        "outcomes": dict(sorted(res.outcomes.items())),
        "invariants_ok": res.invariants["ok"],
    }, indent=2))
    return 0 if res.ok else 1


def cmd_healthcheck(a: argparse.Namespace) -> int:
    try:
        with urllib.request.urlopen(a.url, timeout=3) as r:  # noqa: S310
            return 0 if r.status == 200 else 1
    except OSError:
        return 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ledgercore")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("demo", help="simulate six days of marketplace activity")
    s.add_argument("--db", default=DEFAULT_DB)
    s.add_argument("--fresh", action="store_true")
    s.add_argument("--seed", type=int, default=42)
    s.add_argument("--days", type=int, default=6)
    s.add_argument("--orders-per-day", type=int, default=120)
    s.set_defaults(fn=cmd_demo)

    s = sub.add_parser("api", help="run the HTTP API and web UI")
    s.add_argument("--db", default=DEFAULT_DB)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(fn=cmd_api)

    s = sub.add_parser("gen-settlement", help="write a synthetic processor settlement CSV")
    s.add_argument("--db", default=DEFAULT_DB)
    s.add_argument("--day")
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--clean", action="store_true", help="no planted breaks")
    s.add_argument("--out", default="data/settlement.csv")
    s.set_defaults(fn=cmd_gen)

    s = sub.add_parser("recon", help="reconcile one day against a settlement file")
    s.add_argument("--db", default=DEFAULT_DB)
    s.add_argument("--day", help="YYYY-MM-DD or 'yesterday' (default)")
    s.add_argument("--settlement", help="CSV path; generated with planted breaks if omitted")
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--json", action="store_true")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_recon)

    s = sub.add_parser("recon-eval", help="plant breaks on every day and score detection")
    s.add_argument("--db", default=DEFAULT_DB)
    s.add_argument("--seed", type=int, default=7)
    s.set_defaults(fn=cmd_recon_eval)

    s = sub.add_parser("invariants", help="run every invariant check")
    s.add_argument("--db", default=DEFAULT_DB)
    s.set_defaults(fn=cmd_invariants)

    s = sub.add_parser("stress", help="concurrent purchases, refunds, payouts, then invariants")
    s.add_argument("--db", default="data/stress.db")
    s.add_argument("--threads", type=int, default=16)
    s.add_argument("--ops", type=int, default=100)
    s.add_argument("--seed", type=int, default=1)
    s.set_defaults(fn=cmd_stress)

    s = sub.add_parser("healthcheck", help="exit 0 if /healthz answers")
    s.add_argument("--url", default="http://127.0.0.1:8000/healthz")
    s.set_defaults(fn=cmd_healthcheck)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
