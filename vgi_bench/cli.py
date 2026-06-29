"""CLI entry point: matrix expansion, validate, dry-run, full run."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from vgi_bench import HARNESS_VERSION
from vgi_bench.aggregate import fill_parallel_scaling
from vgi_bench.config import applies, case_threads, load_adapters, load_cases, param_points
from vgi_bench.fingerprint import DEFAULT_DUCKDB, build_fingerprint
from vgi_bench.models import Adapter, Case, Cell
from vgi_bench.templating import render_sql, write_sql
from vgi_bench.writers import (
    make_run_dir,
    rewrite_records,
    write_env,
    write_manifest,
)

ROOT = Path(__file__).resolve().parent.parent
CASES_DIR = ROOT / "cases"
ADAPTERS_DIR = ROOT / "languages"
RESULTS_DIR = ROOT / "results"


def _split_list(s: str | None) -> list[str] | None:
    if not s:
        return None
    return [x.strip() for x in s.split(",") if x.strip()]


def _split_ints(s: str | None) -> list[int] | None:
    if not s:
        return None
    return [int(x.strip()) for x in s.split(",") if x.strip()]


def _filter_cases(cases: list[Case], ids: list[str] | None) -> list[Case]:
    if not ids:
        return cases
    by_id = {c.id: c for c in cases}
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise SystemExit(f"unknown case id(s): {missing}")
    return [by_id[i] for i in ids]


def _filter_adapters(adapters: list[Adapter], ids: list[str] | None) -> list[Adapter]:
    if not ids:
        return adapters
    by_id = {a.language: a for a in adapters}
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise SystemExit(f"unknown language(s): {missing}")
    return [by_id[i] for i in ids]


def _apply_overrides(case: Case, overrides: dict[str, int]) -> Case:
    if not overrides:
        return case
    from dataclasses import replace

    from vgi_bench.models import Iterations

    it = case.iterations
    return replace(
        case,
        iterations=Iterations(
            warmup=overrides.get("warmup", it.warmup),
            measured=overrides.get("measured", it.measured),
        ),
    )


def _parse_overrides(s: str | None) -> dict[str, int]:
    if not s:
        return {}
    out: dict[str, int] = {}
    for part in s.split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip()
        try:
            out[k] = int(v.strip())
        except ValueError:
            raise SystemExit(f"--iterations-override: bad int for {k!r}: {v!r}") from None
    return out


def _expand_matrix(
    cases: list[Case],
    adapters: list[Adapter],
    transports: list[str] | None,
    languages: list[str] | None,
    threads_override: list[int] | None,
) -> list[tuple[Cell, Adapter, Any]]:
    cells: list[tuple[Cell, Adapter, Any]] = []
    for case in cases:
        # Default: every transport variant our runnable adapters declare. SHM
        # is just another transport (e.g. "launch-shm" vs "launch") so adapters
        # opt in by declaring it.
        candidate_transports = transports or ["subprocess", "subprocess-shm", "launch", "launch-shm"]
        for transport in candidate_transports:
            if not applies(case.applies_to.transports, transport):
                continue
            for adapter in adapters:
                if languages and adapter.language not in languages:
                    continue
                if not applies(case.applies_to.languages, adapter.language):
                    continue
                spec = adapter.transports.get(transport)
                if spec is None:
                    continue
                if adapter.supported_function_types and case.function_type not in adapter.supported_function_types:
                    continue
                for params in param_points(case):
                    for t in case_threads(case, threads_override):
                        cell = Cell(
                            case=case,
                            params=params,
                            transport=transport,
                            language=adapter.language,
                            threads=t,
                        )
                        cells.append((cell, adapter, spec))
    return cells


def cmd_validate(args: argparse.Namespace) -> int:
    cases = load_cases(CASES_DIR)
    adapters = load_adapters(ADAPTERS_DIR)
    print(f"OK: {len(cases)} cases, {len(adapters)} adapters validated.")
    for c in cases:
        flags = []
        if not c.parallelizable:
            flags.append("not-parallelizable")
        if c.externalization:
            flags.append("externalize")
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        print(f"  case  {c.id:35s} {c.function_type:14s} fn={c.vgi_function}{suffix}")
    for a in adapters:
        marker = "RUN" if a.runnable else "STUB"
        print(f"  lang  {a.language:12s} [{marker}]  transports={sorted(a.transports.keys())}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cases_all = load_cases(CASES_DIR)
    adapters_all = load_adapters(ADAPTERS_DIR)

    overrides = _parse_overrides(args.iterations_override)
    selected_cases = [_apply_overrides(c, overrides) for c in _filter_cases(cases_all, _split_list(args.cases))]
    selected_adapters = _filter_adapters(adapters_all, _split_list(args.languages))

    transports = _split_list(args.transports)
    languages = _split_list(args.languages)
    threads_override = _split_ints(args.threads)

    cells = _expand_matrix(
        selected_cases,
        selected_adapters,
        transports=transports,
        languages=languages,
        threads_override=threads_override,
    )

    if args.dry_run:
        print(f"matrix: {len(cells)} cells")
        for cell, _adapter, _spec in cells[: args.dry_run_max]:
            print(
                f"  [{cell.case.id}] transport={cell.transport} lang={cell.language} "
                f"threads={cell.threads} params={cell.params}"
            )
        if cells:
            sample_cell, _, _ = cells[0]
            sample_path = Path("/tmp/vgi-bench-dryrun.sql")
            sql = render_sql(
                sample_cell.case,
                sample_cell.params,
                threads=sample_cell.threads,
                transport=sample_cell.transport,
                profile_json_path=Path("/tmp/vgi-bench-dryrun.profile.json"),
                profiling_enabled=True,
                worker_concurrency_probe=(sample_cell.transport == "subprocess"),
            )
            write_sql(sample_path, sql)
            print(f"sample SQL for {sample_cell.case.id}:\n----\n{sql}----")
        return 0

    duckdb_binary = args.duckdb or DEFAULT_DUCKDB
    if not Path(duckdb_binary).exists():
        raise SystemExit(f"duckdb binary not found: {duckdb_binary}")

    env = build_fingerprint(duckdb_binary=duckdb_binary, note=args.note or "")
    out_root = Path(args.out) if args.out else RESULTS_DIR
    run_dir = make_run_dir(out_root, env.run_id)
    write_env(run_dir, env)
    started_at = time.time()
    cli_args_serialized = {
        "cases": args.cases,
        "transports": args.transports,
        "languages": args.languages,
        "threads": args.threads,
        "iterations_override": args.iterations_override,
        "note": args.note,
        "duckdb": str(duckdb_binary),
        "trim_outliers": args.trim_outliers,
        "fail_fast": args.fail_fast,
    }
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "run_id": env.run_id,
        "started_utc": env.timestamp_utc,
        "harness_version": HARNESS_VERSION,
        "matrix_size": len(cells),
        "cli_args": cli_args_serialized,
        "counts": {"ok": 0, "error": 0, "skipped": 0},
    }
    write_manifest(run_dir, manifest)

    # Group cells by (language, transport) so the haybarn connection + worker
    # ATTACH can be reused across them. Sort by threads ascending within a group
    # so the observed pool peak reflects each cell's own thread count.
    from collections import defaultdict as _dd

    from vgi_bench.runner import run_cell_in_group

    groups: dict[tuple[str, str], list[tuple[int, Cell, Adapter, Any]]] = _dd(list)
    for idx, (cell, adapter, spec) in enumerate(cells):
        groups[(cell.language, cell.transport)].append((idx, cell, adapter, spec))
    for k in groups:
        groups[k].sort(key=lambda x: (x[1].case.id, x[1].threads, json.dumps(x[1].params, sort_keys=True)))

    records: list[tuple[int, dict[str, Any]]] = []

    for (lang, transport), entries in groups.items():
        run_cell_in_group(
            entries=entries,
            lang=lang,
            transport=transport,
            run_id=env.run_id,
            run_dir=run_dir,
            trim_outliers=args.trim_outliers,
            fail_fast=args.fail_fast,
            records_out=records,
            skipped_factory=lambda c, a, reason, status="skipped": _skipped_record(c, a, env.run_id, reason=reason, status=status),
            manifest_counts=manifest["counts"],
            log_label=lambda idx, total, cell: f"[{idx + 1}/{total}] {cell.case.id} | {cell.transport}/{cell.language} | t={cell.threads} params={cell.params}",
            total=len(cells),
        )

    # Reorder records to match original matrix order
    records.sort(key=lambda kv: kv[0])
    ordered_records: list[dict[str, Any]] = [r for _, r in records]

    fill_parallel_scaling(ordered_records)
    rewrite_records(run_dir, ordered_records)
    manifest["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    manifest["wall_seconds"] = time.time() - started_at
    write_manifest(run_dir, manifest)
    print(
        f"\nrun_id={env.run_id}  dir={run_dir}\n"
        f"counts: ok={manifest['counts'].get('ok', 0)} "
        f"error={manifest['counts'].get('error', 0)} "
        f"skipped={manifest['counts'].get('skipped', 0)}"
    )
    return 0 if manifest["counts"].get("error", 0) == 0 else 1


def _skipped_record(
    cell: Cell, adapter: Adapter, run_id: str, *, reason: str, status: str = "skipped"
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "case_id": cell.case.id,
        "function_type": cell.case.function_type,
        "vgi_function": cell.case.vgi_function,
        "transport": cell.transport,
        "language": cell.language,
        "threads": cell.threads,
        "params": cell.params,
        "worker_concurrency": {"cap": cell.threads, "observed_peak": None, "source": "n/a"},
        "status": status,
        "error": reason,
        "iterations": {
            "warmup": cell.case.iterations.warmup,
            "measured": cell.case.iterations.measured,
            "measured_walltime_s": [],
            "measured_duckdb_latency_s": [],
            "outliers_trimmed_walltime": 0,
            "outliers_trimmed_latency": 0,
        },
        "rows_processed": cell.params.get("rows"),
        "metrics": {
            "throughput_rows": {"rows_processed": None, "median_rows_per_sec": None, "basis": "duckdb_latency_s"},
            "calls": {"calls_per_sec_median": None, "per_call_latency_us_median": None, "note": ""},
            "rpc_breakdown_ms": {"available": False, "samples": []},
            "byte_throughput": {
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
            "parallel_scaling": {"speedup_vs_1t": None, "efficiency": None, "baseline_threads": 1},
        },
        "stats": {
            "walltime_s": {"samples": [], "n": 0},
            "duckdb_latency_s": {"samples": [], "n": 0},
        },
        "duckdb_profile_summary": {"median_latency_s": None, "rows_returned": None, "cumulative_rows_scanned": None},
        "externalization": cell.case.externalization,
        "raw": {"profile_paths": [], "rpc_timing_samples": [], "rendered_sql_first": [], "worker_location": ""},
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vgi-bench", description="VGI benchmark suite harness.")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_validate = sub.add_parser("validate", help="Schema-check cases + adapters.")
    p_validate.set_defaults(func=cmd_validate)

    p_run = sub.add_parser("run", help="Run the matrix.")
    p_run.add_argument("--cases", help="Comma-separated case ids (default: all).")
    p_run.add_argument("--transports", help="Comma-separated transports (default: subprocess,http,unix).")
    p_run.add_argument("--languages", help="Comma-separated language ids (default: all runnable).")
    p_run.add_argument("--threads", help="Override per-case threads sweep (e.g. '1,4').")
    p_run.add_argument("--iterations-override", help="Comma key=val pairs, e.g. 'warmup=1,measured=3'.")
    p_run.add_argument("--out", help="Results root dir (default: ./results).")
    p_run.add_argument("--note", help="Free-text note stamped into env.json.")
    p_run.add_argument("--duckdb", help="Path to the duckdb CLI binary.")
    p_run.add_argument("--trim-outliers", action="store_true", help="Drop IQR-outlier samples.")
    p_run.add_argument("--fail-fast", action="store_true", help="Abort on the first cell error.")
    p_run.add_argument(
        "--dry-run", action="store_true", help="Expand matrix + render sample SQL; do not start workers or duckdb."
    )
    p_run.add_argument("--dry-run-max", type=int, default=20, help="Max cells to print in --dry-run.")
    p_run.set_defaults(func=cmd_run)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
