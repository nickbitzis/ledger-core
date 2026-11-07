"""A small Prometheus text-format registry (counters and histograms), thread safe.

The API is one process, so in-memory counters are enough. Gauges that
describe ledger state (entry count, active holds, invariant status) are read
from the database at scrape time.
"""

from __future__ import annotations

import threading
from collections import defaultdict

BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0)

HELP = {
    "ledgercore_entries_posted_total": ("counter", "Journal entries committed, by template."),
    "ledgercore_occ_conflicts_total": ("counter", "Optimistic concurrency conflicts detected."),
    "ledgercore_occ_retries_total": ("counter", "Internal retries after a conflict."),
    "ledgercore_idempotent_replays_total": ("counter", "Requests answered from a stored key."),
    "ledgercore_rejections_total": ("counter", "Operations rejected, by error code."),
    "ledgercore_http_requests_total": ("counter", "HTTP requests by route and status."),
    "ledgercore_http_request_seconds": ("histogram", "HTTP latency by route."),
}

_lock = threading.Lock()
_counters: dict[str, dict[tuple, float]] = defaultdict(lambda: defaultdict(float))
_hist: dict[str, dict[tuple, list[float]]] = defaultdict(dict)


def inc(name: str, value: float = 1.0, **labels: str) -> None:
    key = tuple(sorted(labels.items()))
    with _lock:
        _counters[name][key] += value


def observe(name: str, seconds: float, **labels: str) -> None:
    key = tuple(sorted(labels.items()))
    with _lock:
        h = _hist[name].setdefault(key, [0.0] * (len(BUCKETS) + 2))
        for i, b in enumerate(BUCKETS):
            if seconds <= b:
                h[i] += 1
        h[-2] += seconds
        h[-1] += 1


def value(name: str, **labels: str) -> float:
    with _lock:
        return _counters[name].get(tuple(sorted(labels.items())), 0.0)


def total(name: str) -> float:
    with _lock:
        return sum(_counters[name].values())


def _fmt(name: str, labels: tuple, v: float) -> str:
    if labels:
        inner = ",".join(f'{k}="{val}"' for k, val in labels)
        return f"{name}{{{inner}}} {v:g}"
    return f"{name} {v:g}"


def render(gauges: list[tuple[str, str, dict[str, str], float]]) -> str:
    """Counters and histograms from memory, plus gauges passed in (name, help, labels, value)."""
    lines: list[str] = []
    with _lock:
        for name, (typ, help_) in HELP.items():
            lines += [f"# HELP {name} {help_}", f"# TYPE {name} {typ}"]
            if typ == "counter":
                for labels, v in sorted(_counters.get(name, {}).items()):
                    lines.append(_fmt(name, labels, v))
            else:
                for labels, h in sorted(_hist.get(name, {}).items()):
                    for i, b in enumerate(BUCKETS):
                        lines.append(_fmt(f"{name}_bucket", (*labels, ("le", str(b))), h[i]))
                    lines.append(_fmt(f"{name}_bucket", (*labels, ("le", "+Inf")), h[-1]))
                    lines.append(_fmt(f"{name}_sum", labels, h[-2]))
                    lines.append(_fmt(f"{name}_count", labels, h[-1]))
    seen: set[str] = set()
    for name, help_, labels, v in gauges:
        if name not in seen:
            lines += [f"# HELP {name} {help_}", f"# TYPE {name} gauge"]
            seen.add(name)
        lines.append(_fmt(name, tuple(sorted(labels.items())), v))
    return "\n".join(lines) + "\n"
