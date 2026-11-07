"""Concurrency stress test. Size comes from LEDGERCORE_STRESS_THREADS and _OPS."""

import os

import pytest

from ledgercore.stress import run_stress


@pytest.mark.stress
def test_concurrent_money_movement_keeps_invariants(tmp_path):
    threads = int(os.environ.get("LEDGERCORE_STRESS_THREADS", "8"))
    ops = int(os.environ.get("LEDGERCORE_STRESS_OPS", "40"))
    res = run_stress(tmp_path / "stress.db", threads=threads, ops=ops, seed=11)
    assert res.invariants["ok"], res.invariants
    assert res.purchases_ok == res.purchase_entries  # no retry or resend posted twice
    assert sum(res.outcomes.values()) == threads * ops
    assert res.duplicate_sends > 0
