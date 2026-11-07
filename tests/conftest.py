from __future__ import annotations

import logging

import pytest

from ledgercore.clock import ManualClock
from ledgercore.ledger import Ledger
from tests.helpers import T0, seed

logging.getLogger("ledgercore").setLevel(logging.ERROR)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(T0)


@pytest.fixture
def led(tmp_path, clock) -> Ledger:
    ledger = Ledger(tmp_path / "test.db", clock)
    seed(ledger, clock)
    yield ledger
    ledger.close()
