"""Render the SQL file for one matrix cell."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from vgi_bench.models import Case

_PARAM_RX = re.compile(r"\$\{param:([a-zA-Z_][a-zA-Z0-9_]*)\}")
_SIMPLE_RX = re.compile(r"\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _substitute(template: str, *, alias: str, params: dict[str, Any], extras: dict[str, str]) -> str:
    def param_sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in params:
            raise KeyError(f"unknown param placeholder ${{param:{name}}}; have {sorted(params)}")
        return str(params[name])

    out = _PARAM_RX.sub(param_sub, template)

    def simple_sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if name == "alias":
            return alias
        if name in extras:
            return extras[name]
        # leave non-matching tokens alone (e.g. SQL ${} that we don't manage)
        return m.group(0)

    return _SIMPLE_RX.sub(simple_sub, out)


def derive_extra_placeholders(case: Case, params: dict[str, Any]) -> dict[str, str]:
    """Compute any derived placeholders (e.g. ${payload_expr})."""
    extras: dict[str, str] = {}
    payload_bytes = params.get("payload_bytes")
    if payload_bytes is not None:
        if int(payload_bytes) <= 8:
            extras["payload_expr"] = "i::BIGINT AS v"
        else:
            extras["payload_expr"] = f"repeat('x', {int(payload_bytes)}) AS v"
    return extras


def render_sql(
    case: Case,
    params: dict[str, Any],
    *,
    threads: int,
    transport: str,
    profile_json_path: Path,
    profiling_enabled: bool = True,
    worker_concurrency_probe: bool = False,
) -> str:
    alias = case.alias
    extras = derive_extra_placeholders(case, params)

    lines: list[str] = []
    if profiling_enabled:
        lines.append("PRAGMA enable_profiling='json';")
        lines.append(f"PRAGMA profiling_output='{profile_json_path}';")

    if case.requires_attach:
        lines.append(f"ATTACH '{case.attach_name}' AS {alias} (TYPE vgi, LOCATION getenv('VGI_BENCH_WORKER'));")

    lines.append(f"SET threads={threads};")
    if transport == "subprocess":
        # vgi_worker_pool_max is the per-path subprocess pool cap (0 = pool disabled).
        lines.append(f"SET vgi_worker_pool_max={max(threads, 1)};")

    for stmt in case.setup_sql:
        lines.append(_substitute(stmt, alias=alias, params=params, extras=extras))

    lines.append(_substitute(case.query_sql, alias=alias, params=params, extras=extras))

    if worker_concurrency_probe and transport == "subprocess":
        # Disable profiling first so the probe query does NOT overwrite the
        # main query's profile file (DuckDB rewrites profiling_output per query).
        lines.append("PRAGMA disable_profiling;")
        lines.append("-- worker concurrency probe (subprocess pool observability)")
        lines.append(
            "CREATE OR REPLACE TEMP TABLE __vgi_bench_pool_snapshot AS SELECT * FROM vgi_worker_pool();"
        )
        lines.append("SELECT 'pool_count' AS k, COUNT(*) AS v FROM __vgi_bench_pool_snapshot;")

    for stmt in case.teardown_sql:
        lines.append(_substitute(stmt, alias=alias, params=params, extras=extras))

    return "\n".join(lines) + "\n"


def write_sql(path: Path, contents: str) -> None:
    path.write_text(contents)
