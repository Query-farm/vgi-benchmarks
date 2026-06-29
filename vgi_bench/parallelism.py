"""Parallelism helpers: pool-cap SQL and worker-concurrency probing."""

from __future__ import annotations

import re
from pathlib import Path

_POOL_PROBE_RX = re.compile(r"pool_count[^\n]*?(\d+)", re.IGNORECASE)


def parse_pool_probe(stdout: str) -> int | None:
    """Pick the largest ``pool_count <N>`` value seen in stdout.

    DuckDB's CLI prints query results as tables; ``SELECT 'pool_count' AS k,
    COUNT(*) AS v FROM …`` emits a row whose digits we capture here.
    """
    matches = _POOL_PROBE_RX.findall(stdout or "")
    if not matches:
        return None
    try:
        return max(int(m) for m in matches)
    except ValueError:
        return None


def empty_probe_path(p: Path) -> Path:
    """Helper so callers can opt out of the worker-concurrency probe trivially."""
    return p
