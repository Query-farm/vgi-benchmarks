"""Build a single result record from raw iteration data, and a post-pass for parallel scaling."""

from __future__ import annotations

from typing import Any

from vgi_bench import RESULT_SCHEMA_VERSION
from vgi_bench.profile_parse import ProfileSummary
from vgi_bench.rpc_timing import RpcBreakdown
from vgi_bench.stats import summarize, trim_max_outlier


def build_cell_record(
    *,
    run_id: str,
    case_id: str,
    function_type: str,
    vgi_function: str,
    transport: str,
    language: str,
    threads: int,
    params: dict[str, Any],
    worker_concurrency_cap: int,
    observed_peak_workers: int | None,
    rendered_sql: str,
    worker_location: str,
    status: str,
    error: str | None,
    iterations_cfg: dict[str, int],
    measured_walltimes_s: list[float],
    measured_latencies_s: list[float],
    rows_processed: int | None,
    profile_summaries: list[ProfileSummary],
    rpc_breakdowns: list[RpcBreakdown],
    bytes_basis: dict[str, Any] | None,
    externalization: dict[str, Any] | None,
    raw_profile_paths: list[str],
    trim_outliers: bool,
) -> dict[str, Any]:
    if trim_outliers:
        wt, dropped_wt = trim_max_outlier(measured_walltimes_s)
        lat, dropped_lat = trim_max_outlier(measured_latencies_s)
    else:
        wt = list(measured_walltimes_s)
        lat = list(measured_latencies_s)
        dropped_wt = 0
        dropped_lat = 0

    wt_summary = summarize(wt)
    lat_summary = summarize(lat) if lat else {"samples": [], "n": 0}

    rows = rows_processed or 0
    median_lat = float(lat_summary.get("median", 0.0) or 0.0) if lat_summary.get("n") else None

    throughput = None
    calls_per_sec = None
    per_call_latency_us = None
    if median_lat and median_lat > 0 and rows:
        throughput = rows / median_lat
        calls_per_sec = throughput  # for scalar/aggregate "one call per row" is the canonical view
        per_call_latency_us = (median_lat / rows) * 1_000_000.0

    # RPC breakdown median across iterations (only available ones)
    available = [b for b in rpc_breakdowns if b.available]
    rpc_block: dict[str, Any]
    if not available:
        rpc_block = {"available": False, "samples": []}
    else:

        def _med(field: str) -> float | None:
            vals = [getattr(b, field) for b in available if getattr(b, field) is not None]
            if not vals:
                return None
            vals = sorted(vals)
            mid = len(vals) // 2
            if len(vals) % 2 == 1:
                return float(vals[mid])
            return float((vals[mid - 1] + vals[mid]) / 2.0)

        rpc_block = {
            "available": True,
            "n": len(available),
            "median": {
                "batches": int(_med("batches")) if _med("batches") is not None else None,
                "total_ms": _med("total_ms"),
                "convert_in_ms": _med("convert_in_ms"),
                "write_ms": _med("write_ms"),
                "read_ms": _med("read_ms"),
                "schema_ms": _med("schema_ms"),
                "convert_out_ms": _med("convert_out_ms"),
            },
            "samples": [
                {
                    "batches": b.batches,
                    "total_ms": b.total_ms,
                    "convert_in_ms": b.convert_in_ms,
                    "write_ms": b.write_ms,
                    "read_ms": b.read_ms,
                    "schema_ms": b.schema_ms,
                    "convert_out_ms": b.convert_out_ms,
                }
                for b in available
            ],
        }

    profile_block: dict[str, Any]
    if profile_summaries:
        lats = [p.latency_s for p in profile_summaries if p.latency_s is not None]
        rows_returned = [p.rows_returned for p in profile_summaries if p.rows_returned is not None]
        cum_scanned = [p.cumulative_rows_scanned for p in profile_summaries if p.cumulative_rows_scanned is not None]
        # union of operator names; median of timings per op
        all_ops: dict[str, list[float]] = {}
        for p in profile_summaries:
            for k, v in p.operator_timing_ms.items():
                all_ops.setdefault(k, []).append(v)
        top_ops = {k: sorted(vs)[len(vs) // 2] for k, vs in all_ops.items()}
        profile_block = {
            "median_latency_s": sorted(lats)[len(lats) // 2] if lats else None,
            "rows_returned": rows_returned[-1] if rows_returned else None,
            "cumulative_rows_scanned": cum_scanned[-1] if cum_scanned else None,
            "top_operators_ms": dict(sorted(top_ops.items(), key=lambda kv: -kv[1])[:10]),
        }
    else:
        profile_block = {"median_latency_s": None, "rows_returned": None, "cumulative_rows_scanned": None}

    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "run_id": run_id,
        "case_id": case_id,
        "function_type": function_type,
        "vgi_function": vgi_function,
        "transport": transport,
        "language": language,
        "threads": threads,
        "params": params,
        "worker_concurrency": {
            "cap": worker_concurrency_cap,
            "observed_peak": observed_peak_workers,
            "source": "vgi_worker_pool" if transport == "subprocess" else "configured",
        },
        "status": status,
        "error": error,
        "iterations": {
            **iterations_cfg,
            "measured_walltime_s": wt,
            "measured_duckdb_latency_s": lat,
            "outliers_trimmed_walltime": dropped_wt,
            "outliers_trimmed_latency": dropped_lat,
        },
        "rows_processed": rows_processed,
        "metrics": {
            "throughput_rows": {
                "rows_processed": rows_processed,
                "median_rows_per_sec": throughput,
                "basis": "duckdb_latency_s",
            },
            "calls": {
                "calls_per_sec_median": calls_per_sec,
                "per_call_latency_us_median": per_call_latency_us,
                "note": "scalar/aggregate => 1 call per row; table/table_in_out reuses rows as a proxy.",
            },
            "rpc_breakdown_ms": rpc_block,
            "byte_throughput": bytes_basis
            or {
                "input_bytes_per_row": None,
                "output_bytes_per_row": None,
                "input_bytes_total": None,
                "output_bytes_total": None,
                "input_bytes_per_sec_median": None,
                "output_bytes_per_sec_median": None,
                "source": "n/a",
                "schema_in": "",
                "schema_out": "",
            },
            "parallel_scaling": {
                "speedup_vs_1t": None,  # filled by post-pass
                "efficiency": None,
                "baseline_threads": 1,
            },
        },
        "stats": {
            "walltime_s": wt_summary,
            "duckdb_latency_s": lat_summary,
        },
        "duckdb_profile_summary": profile_block,
        "externalization": externalization,
        "raw": {
            "profile_paths": raw_profile_paths,
            "rpc_timing_samples": [b.raw_line for b in rpc_breakdowns if b.available],
            "rendered_sql_first": rendered_sql.splitlines()[:80],
            "worker_location": worker_location,
        },
    }


def fill_parallel_scaling(records: list[dict[str, Any]]) -> None:
    """Join each record to its threads=1 sibling and fill ``parallel_scaling``."""

    def key(r: dict[str, Any]) -> tuple[str, str, str, str]:
        return (r["case_id"], r["transport"], r["language"], json.dumps(r["params"], sort_keys=True))

    baselines: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for r in records:
        if r.get("threads") == 1 and r.get("status") == "ok":
            baselines[key(r)] = r
    for r in records:
        if r.get("status") != "ok":
            continue
        b = baselines.get(key(r))
        if not b:
            continue
        baseline_lat = b["stats"]["duckdb_latency_s"].get("median")
        my_lat = r["stats"]["duckdb_latency_s"].get("median")
        if not baseline_lat or not my_lat or my_lat <= 0:
            continue
        speedup = float(baseline_lat) / float(my_lat)
        eff = speedup / max(r["threads"], 1)
        r["metrics"]["parallel_scaling"] = {
            "speedup_vs_1t": speedup,
            "efficiency": eff,
            "baseline_threads": 1,
        }


# late import to avoid a cycle at module load time
import json  # noqa: E402
