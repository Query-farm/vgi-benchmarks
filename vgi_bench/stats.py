"""Aggregate raw samples into summary statistics."""

from __future__ import annotations

import statistics
from collections.abc import Iterable


def summarize(samples: Iterable[float]) -> dict[str, float | int | list[float]]:
    xs = sorted(float(x) for x in samples)
    if not xs:
        return {"samples": [], "n": 0}
    return {
        "samples": xs,
        "n": len(xs),
        "min": xs[0],
        "max": xs[-1],
        "median": statistics.median(xs),
        "mean": statistics.fmean(xs),
        "p95": _percentile(xs, 95.0),
        "stdev": statistics.pstdev(xs) if len(xs) > 1 else 0.0,
    }


def _percentile(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    if len(xs) == 1:
        return xs[0]
    pos = (q / 100.0) * (len(xs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    frac = pos - lo
    return xs[lo] * (1.0 - frac) + xs[hi] * frac


def trim_max_outlier(xs: list[float]) -> tuple[list[float], int]:
    if len(xs) < 5:
        return list(xs), 0
    s = sorted(xs)
    med = statistics.median(s)
    q1 = _percentile(s, 25.0)
    q3 = _percentile(s, 75.0)
    iqr = q3 - q1
    cutoff = med + 3.0 * iqr if iqr > 0 else float("inf")
    out = [x for x in s if x <= cutoff]
    if len(out) == len(s):
        return list(xs), 0
    # drop only the largest outlier(s); keep order otherwise (but we already sorted)
    return out, len(s) - len(out)
