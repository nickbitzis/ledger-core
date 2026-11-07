from datetime import UTC, date, datetime, timedelta

import pytest

from ledgercore import recon
from ledgercore.clock import ManualClock
from ledgercore.ledger import Ledger

DAY = date(2026, 11, 4)


@pytest.fixture
def book(tmp_path):
    """Three days of purchases, a few right before midnight, and one refund."""
    clock = ManualClock(datetime(2026, 11, 3, 6, tzinfo=UTC))
    led = Ledger(tmp_path / "r.db", clock)
    led.add_organizer("northbeam", "Northbeam Live", "USD")
    led.add_event("evt-1", "northbeam", "Show", datetime(2026, 11, 30, tzinfo=UTC))
    n = 0
    for d in (3, 4, 5):
        base = datetime(2026, 11, d, tzinfo=UTC)
        times = [base + timedelta(hours=8, minutes=7 * i) for i in range(30)]
        times += [base + timedelta(hours=23, minutes=57, seconds=i * 10) for i in range(2)]
        for t in times:
            n += 1
            clock.set(t)
            led.execute("ticket_purchase", {"order_id": f"o{n}", "organizer": "northbeam",
                                            "event_id": "evt-1", "gross": 2500 + 100 * (n % 7)})
        if d == 4:
            clock.set(base + timedelta(hours=15))
            led.execute("refund_before_payout", {"order_id": f"o{n - 5}"})
    yield led
    led.close()


def test_every_planted_break_is_detected(book):
    lines, manifest = recon.generate_settlement(book.conn, DAY, seed=3)
    report = recon.reconcile(book.conn, DAY, lines)
    for status, s in recon.score(report, manifest).items():
        assert s["planted"] == s["detected"] == s["flagged"], (status, s)
    assert len(report.breaks) == 5


def test_clean_file_has_no_breaks(book):
    lines, manifest = recon.generate_settlement(book.conn, DAY, plant=False)
    report = recon.reconcile(book.conn, DAY, lines)
    assert report.breaks == []
    assert report.counts["timing_difference"] == 4  # 2 from yesterday, 2 into tomorrow
    assert report.counts["matched"] == 30 + 1  # daytime captures plus the refund


def test_csv_round_trip(book):
    lines, _ = recon.generate_settlement(book.conn, DAY)
    assert recon.read_csv(recon.write_csv(lines)) == lines


def test_refund_lines_are_matched(book):
    lines, _ = recon.generate_settlement(book.conn, DAY, plant=False)
    report = recon.reconcile(book.conn, DAY, lines)
    refunds = [r for r in report.results if r.kind == "REFUND"]
    assert [r.status for r in refunds] == ["matched"]


def test_fuzzy_match_picks_closest_in_time(book):
    lines, _ = recon.generate_settlement(book.conn, DAY, plant=False)
    target = next(ln for ln in lines if ln.type == "CAPTURE" and ln.txn_at.startswith("2026-11-04"))
    target.processor_ref = "garbled-ref"
    report = recon.reconcile(book.conn, DAY, lines)
    fuzzy = [r for r in report.results if r.status == "matched_fuzzy"]
    assert len(fuzzy) == 1 and report.breaks == []


def test_amount_mismatch_reports_difference(book):
    lines, _ = recon.generate_settlement(book.conn, DAY, plant=False)
    ln = next(x for x in lines if x.type == "CAPTURE" and x.txn_at.startswith("2026-11-04"))
    ln.gross_minor += 1
    report = recon.reconcile(book.conn, DAY, lines)
    (brk,) = report.breaks
    assert brk.status == "amount_mismatch" and brk.note == "diff +1"


def test_markdown_report(book):
    lines, _ = recon.generate_settlement(book.conn, DAY)
    md = recon.reconcile(book.conn, DAY, lines).to_markdown()
    assert md.startswith("# Reconciliation report for 2026-11-04")
    assert "| duplicate |" in md and "Breaks needing action: 5" in md
