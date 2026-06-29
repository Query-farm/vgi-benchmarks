"""Parse DuckDB's ``PRAGMA enable_profiling='json'`` output."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ProfileSummary:
    latency_s: float | None
    rows_returned: int | None
    cumulative_rows_scanned: int | None
    cpu_time_s: float | None
    operator_timing_ms: dict[str, float] = field(default_factory=dict)
    raw_path: str = ""


def _walk(node: dict[str, Any], acc: dict[str, float]) -> None:
    op = node.get("operator_type") or node.get("name")
    t = node.get("operator_timing")
    if op and isinstance(t, (int, float)):
        acc[op] = acc.get(op, 0.0) + float(t) * 1000.0
    children = node.get("children", []) or []
    for c in children:
        if isinstance(c, dict):
            _walk(c, acc)


def parse_profile(path: Path) -> ProfileSummary:
    text = path.read_text(errors="replace")
    if not text.strip():
        return ProfileSummary(
            latency_s=None,
            rows_returned=None,
            cumulative_rows_scanned=None,
            cpu_time_s=None,
            raw_path=str(path),
        )
    # DuckDB emits one JSON document per query; for multi-query files it emits
    # multiple top-level objects concatenated. Find the largest one — typically
    # the final SELECT after PRAGMA/SET/ATTACH/setup.
    decoder = json.JSONDecoder()
    docs: list[dict[str, Any]] = []
    idx = 0
    while idx < len(text):
        # skip whitespace
        while idx < len(text) and text[idx].isspace():
            idx += 1
        if idx >= len(text):
            break
        try:
            obj, end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            break
        if isinstance(obj, dict):
            docs.append(obj)
        idx = end

    if not docs:
        return ProfileSummary(
            latency_s=None,
            rows_returned=None,
            cumulative_rows_scanned=None,
            cpu_time_s=None,
            raw_path=str(path),
        )

    def measured_score(d: dict[str, Any]) -> tuple[float, int]:
        return (float(d.get("latency") or 0.0), int(d.get("cumulative_rows_scanned") or 0))

    chosen = max(docs, key=measured_score)
    op_acc: dict[str, float] = {}
    _walk(chosen, op_acc)
    return ProfileSummary(
        latency_s=float(chosen.get("latency")) if chosen.get("latency") is not None else None,
        rows_returned=int(chosen["rows_returned"]) if chosen.get("rows_returned") is not None else None,
        cumulative_rows_scanned=int(chosen["cumulative_rows_scanned"])
        if chosen.get("cumulative_rows_scanned") is not None
        else None,
        cpu_time_s=float(chosen["cpu_time"]) if chosen.get("cpu_time") is not None else None,
        operator_timing_ms=op_acc,
        raw_path=str(path),
    )
