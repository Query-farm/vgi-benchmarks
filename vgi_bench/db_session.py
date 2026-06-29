"""In-process DuckDB sessions via ``haybarn`` (DuckDB Python module).

Replaces shelling out to ``duckdb -unsigned -f file.sql`` per iteration. The
single-process model is critical for correctness:

  - Workers spawned during ATTACH live for the lifetime of the connection.
  - Warmup queries actually warm the pool that measured queries reuse.
  - Per-query latency comes from ``time.perf_counter()`` and is in-process —
    no CLI startup overhead in the denominator.

Cold-connection samples still create *fresh* connections so they capture the
real "first-call cost" (worker spawn + bind + init).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import haybarn

from vgi_bench.models import Case
from vgi_bench.profile_parse import ProfileSummary, parse_profile
from vgi_bench.templating import (
    _substitute,  # noqa: PLC2701 — internal helper, deliberately reused here
    derive_extra_placeholders,
)

log = logging.getLogger(__name__)


@dataclass
class WarmSessionResult:
    measured_walltimes_s: list[float] = field(default_factory=list)
    measured_latencies_s: list[float] = field(default_factory=list)
    profile_summaries: list[ProfileSummary] = field(default_factory=list)
    profile_paths: list[str] = field(default_factory=list)
    observed_peak_workers: int | None = None
    rendered_sql_first: str = ""
    error: str | None = None


_FORCE_INSTALLED = False


def _ensure_extension_loaded(con: haybarn.DuckDBPyConnection) -> None:
    """Force-install the latest VGI community build once per process, then LOAD it.

    ``FORCE INSTALL`` re-downloads the community extension so a stale cached copy
    can't pin an out-of-date wire schema (which would mismatch a newer worker).
    We do it once per process — the install is global to the DuckDB extension
    directory, so later connections only need ``LOAD``.
    """
    global _FORCE_INSTALLED
    if not _FORCE_INSTALLED:
        try:
            con.execute("FORCE INSTALL vgi FROM community;")
        except Exception as e:
            # Windows: the extension dir lives outside common AV exclusions, so a
            # freshly downloaded .duckdb_extension can be briefly locked by an
            # on-access scan and the atomic move fails ("Access is denied"). The
            # previously installed copy is still valid — fall back to it.
            log.warning("FORCE INSTALL vgi failed (using already-installed copy): %s", e)
        _FORCE_INSTALLED = True
    con.execute("LOAD vgi;")


def _new_connection() -> haybarn.DuckDBPyConnection:
    return haybarn.connect(config={"allow_unsigned_extensions": "true"})


def _render_query(case: Case, params: dict[str, Any]) -> tuple[list[str], str, list[str]]:
    """Return (setup_sql_list, query_sql, teardown_sql_list) with placeholders resolved."""
    alias = case.alias
    extras = derive_extra_placeholders(case, params)
    setup = [_substitute(s, alias=alias, params=params, extras=extras) for s in case.setup_sql]
    query = _substitute(case.query_sql, alias=alias, params=params, extras=extras)
    teardown = [_substitute(s, alias=alias, params=params, extras=extras) for s in case.teardown_sql]
    return setup, query, teardown


def _sql_literal(s: str) -> str:
    """Escape a string for use as a SQL string literal."""
    return "'" + s.replace("'", "''") + "'"


def _attach_sql(case: Case, location: str) -> str:
    return f"ATTACH '{case.attach_name}' AS {case.alias} (TYPE vgi, LOCATION {_sql_literal(location)});"


def _detach_sql(case: Case) -> str:
    return f"DETACH {case.alias};"


class GroupSession:
    """Long-lived haybarn connection + single ATTACH shared across cells.

    All cells with the same (language, transport, worker_location) reuse one
    connection and one ATTACH. Per-cell setup/teardown SQL still runs so temp
    tables get rebuilt; SET threads / vgi_worker_pool_max are re-applied per cell.
    The worker pool grows monotonically within the session, so cells should be
    ordered by ascending threads to keep observed_peak meaningful per cell.
    """

    def __init__(self, *, transport: str, worker_location: str, attach_name: str, alias: str) -> None:
        self.transport = transport
        self.worker_location = worker_location
        self.attach_name = attach_name
        self.alias = alias
        self.con: haybarn.DuckDBPyConnection | None = None

    def __enter__(self) -> GroupSession:
        self.con = _new_connection()
        _ensure_extension_loaded(self.con)
        self.con.execute(
            f"ATTACH '{self.attach_name}' AS {self.alias} (TYPE vgi, LOCATION {_sql_literal(self.worker_location)});"
        )
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.con is not None:
            try:
                self.con.execute(f"DETACH {self.alias};")
            except Exception as e:
                log.debug("DETACH failed: %s", e)
            self.con.close()
            self.con = None


def run_cell_warm(
    *,
    case: Case,
    params: dict[str, Any],
    threads: int,
    transport: str,
    worker_location: str,
    warmup_count: int,
    measured_count: int,
    raw_dir: Path,
    worker_concurrency_probe: bool,
    session: GroupSession | None = None,
) -> WarmSessionResult:
    """Run one matrix cell.

    If ``session`` is supplied, reuses its long-lived ATTACH (much faster across
    a group of cells). Otherwise creates an ephemeral connection for this cell.
    """
    setup, query, teardown = _render_query(case, params)
    result = WarmSessionResult()

    if session is not None:
        return _run_in_session(case, params, threads, transport, setup, query, teardown,
                               warmup_count, measured_count, raw_dir,
                               worker_concurrency_probe, session, result)

    # ---- Standalone (legacy) — one connection for setup + warmup + all measured ----
    if False:  # placeholder for legacy ephemeral path (kept for explicit unsessioned use)
        pass
    con = _new_connection()
    try:
        _ensure_extension_loaded(con)
        if case.requires_attach:
            con.execute(_attach_sql(case, worker_location))
        con.execute(f"SET threads={threads};")
        if transport == "subprocess":
            con.execute(f"SET vgi_worker_pool_max={max(threads, 1)};")
        for stmt in setup:
            con.execute(stmt)

        # warmup
        for _ in range(warmup_count):
            con.execute(query).fetchall()

        # measured
        result.rendered_sql_first = query
        for i in range(measured_count):
            profile_path = raw_dir / f"measured-t{threads}-{i:02d}.json"
            con.execute("PRAGMA enable_profiling='json';")
            con.execute(f"PRAGMA profiling_output='{profile_path}';")
            t0 = time.perf_counter()
            con.execute(query).fetchall()
            result.measured_walltimes_s.append(time.perf_counter() - t0)
            try:
                summary = parse_profile(profile_path)
            except Exception as e:
                log.warning("profile parse failed for %s: %s", profile_path, e)
                summary = ProfileSummary(
                    latency_s=None, rows_returned=None,
                    cumulative_rows_scanned=None, cpu_time_s=None,
                    raw_path=str(profile_path),
                )
            result.profile_summaries.append(summary)
            result.profile_paths.append(str(profile_path))
            if summary.latency_s is not None:
                result.measured_latencies_s.append(summary.latency_s)

        # worker-concurrency probe (subprocess only — pool table is subprocess-specific)
        if worker_concurrency_probe and transport == "subprocess":
            con.execute("PRAGMA disable_profiling;")
            try:
                row = con.execute("SELECT count(*) FROM vgi_worker_pool();").fetchone()
                if row is not None:
                    result.observed_peak_workers = int(row[0])
            except Exception as e:
                log.debug("pool probe failed: %s", e)

        # teardown
        for stmt in teardown:
            try:
                con.execute(stmt)
            except Exception as e:
                log.debug("teardown stmt failed: %s", e)
        if case.requires_attach:
            try:
                con.execute(_detach_sql(case))
            except Exception as e:
                log.debug("DETACH failed: %s", e)
    except Exception as e:
        result.error = f"warm session failed: {e}"
    finally:
        con.close()

    return result


def _run_in_session(
    case: Case,
    params: dict[str, Any],
    threads: int,
    transport: str,
    setup: list[str],
    query: str,
    teardown: list[str],
    warmup_count: int,
    measured_count: int,
    raw_dir: Path,
    worker_concurrency_probe: bool,
    session: GroupSession,
    result: WarmSessionResult,
) -> WarmSessionResult:
    assert session.con is not None
    con = session.con
    try:
        con.execute(f"SET threads={threads};")
        if transport == "subprocess":
            con.execute(f"SET vgi_worker_pool_max={max(threads, 1)};")

        for stmt in setup:
            con.execute(stmt)

        for _ in range(warmup_count):
            con.execute(query).fetchall()

        result.rendered_sql_first = query
        for i in range(measured_count):
            profile_path = raw_dir / f"measured-t{threads}-{i:02d}.json"
            con.execute("PRAGMA enable_profiling='json';")
            con.execute(f"PRAGMA profiling_output='{profile_path}';")
            t0 = time.perf_counter()
            con.execute(query).fetchall()
            result.measured_walltimes_s.append(time.perf_counter() - t0)
            try:
                summary = parse_profile(profile_path)
            except Exception as e:
                log.warning("profile parse failed: %s", e)
                summary = ProfileSummary(
                    latency_s=None, rows_returned=None,
                    cumulative_rows_scanned=None, cpu_time_s=None,
                    raw_path=str(profile_path),
                )
            result.profile_summaries.append(summary)
            result.profile_paths.append(str(profile_path))
            if summary.latency_s is not None:
                result.measured_latencies_s.append(summary.latency_s)

        if worker_concurrency_probe and transport == "subprocess":
            con.execute("PRAGMA disable_profiling;")
            try:
                row = con.execute("SELECT count(*) FROM vgi_worker_pool();").fetchone()
                if row is not None:
                    result.observed_peak_workers = int(row[0])
            except Exception as e:
                log.debug("pool probe failed: %s", e)

        # Per-cell teardown only (worker stays alive for the group)
        for stmt in teardown:
            try:
                con.execute(stmt)
            except Exception as e:
                log.debug("teardown stmt failed: %s", e)
    except Exception as e:
        result.error = f"session cell failed: {e}"
    return result
