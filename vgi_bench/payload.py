"""Resolve per-row byte counts from case payload schema + params.

Case payload formulas are tiny: a literal int (``"8"``), a sum/product of
literals and ``${param:name}`` references (``"4 + ${param:payload_bytes}"``).
Only `+`, `*`, `-` and integer literals + param refs are supported — we evaluate
in a no-builtins ``eval`` after substituting params and validating with a regex.
"""

from __future__ import annotations

import re
from typing import Any

_PARAM_RX = re.compile(r"\$\{param:([a-zA-Z_][a-zA-Z0-9_]*)\}")
_SAFE_RX = re.compile(r"^[\d\s+\-*()]+$")


def evaluate_formula(formula: str | int | None, params: dict[str, Any]) -> int | None:
    """Resolve a payload formula to an integer byte count.

    Returns None when the formula is None / unparseable.
    """
    if formula is None:
        return None
    if isinstance(formula, int):
        return formula
    if not isinstance(formula, str):
        return None
    # Substitute ${param:name} -> str(value)
    def _sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in params:
            raise KeyError(f"payload formula references unknown param {name!r}; have {sorted(params)}")
        return str(int(params[name]))

    expanded = _PARAM_RX.sub(_sub, formula).strip()
    if not _SAFE_RX.match(expanded):
        return None
    try:
        return int(eval(expanded, {"__builtins__": {}}, {}))  # noqa: S307 — regex-restricted
    except (SyntaxError, ValueError, ZeroDivisionError):
        return None


def compute_bytes(case_payload: dict[str, Any], params: dict[str, Any]) -> dict[str, int | None]:
    """Return {input_bytes_per_row, output_bytes_per_row} resolved for this param point."""
    return {
        "input_bytes_per_row": evaluate_formula(case_payload.get("input_bytes_per_row"), params),
        "output_bytes_per_row": evaluate_formula(case_payload.get("output_bytes_per_row"), params),
    }
