"""Per-cell driver: start worker, run cold + warm sessions via haybarn (in-process)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from vgi_bench.adapter import WorkerError, WorkerSession
from vgi_bench.aggregate import build_cell_record
from vgi_bench.db_session import GroupSession, run_cell_warm
from vgi_bench.models import Adapter, Case, Cell, TransportSpec
from vgi_bench.payload import compute_bytes
from vgi_bench.rpc_timing import RpcBreakdown


def _max_workers_for(cell: Cell) -> int:
    return max(cell.threads, 1)


def _expected_rows(case: Case, params: dict[str, Any]) -> int | None:
    return params.get("rows")


def _bytes_basis(
    case: Case, params: dict[str, Any], rows: int | None, median_lat: float | None
) -> dict[str, Any] | None:
    """Compute input/output bytes/sec from the case's declared payload schema."""
    bs = compute_bytes(case.payload, params)
    in_bpr = bs["input_bytes_per_row"]
    out_bpr = bs["output_bytes_per_row"]
    if rows is None or not median_lat or (in_bpr is None and out_bpr is None):
        pb = params.get("payload_bytes")
        if pb is None or rows is None or not median_lat:
            return None
        total = int(pb) * int(rows)
        return {
            "input_bytes_per_row": int(pb),
            "output_bytes_per_row": int(pb),
            "input_bytes_total": total,
            "output_bytes_total": total,
            "input_bytes_per_sec_median": float(total) / float(median_lat),
            "output_bytes_per_sec_median": float(total) / float(median_lat),
            "source": "param.payload_bytes (legacy)",
            "schema_in": case.payload.get("schema_in", ""),
            "schema_out": case.payload.get("schema_out", ""),
        }
    in_total = (in_bpr or 0) * int(rows)
    out_total = (out_bpr or 0) * int(rows)
    return {
        "input_bytes_per_row": in_bpr,
        "output_bytes_per_row": out_bpr,
        "input_bytes_total": in_total,
        "output_bytes_total": out_total,
        "input_bytes_per_sec_median": (float(in_total) / float(median_lat)) if in_bpr is not None else None,
        "output_bytes_per_sec_median": (float(out_total) / float(median_lat)) if out_bpr is not None else None,
        "source": "case.payload formula",
        "schema_in": case.payload.get("schema_in", ""),
        "schema_out": case.payload.get("schema_out", ""),
    }


def _build_cell_from_warm_result(
    *, cell: Cell, run_id: str, run_dir: Path, location: str, warm: Any, rows: int | None,
    iterations_cfg: dict[str, int], trim_outliers: bool,
) -> dict[str, Any]:
    """Convert a WarmSessionResult plus cell metadata into a cell record."""
    if warm.error:
        status, error = "error", warm.error
    else:
        status, error = "ok", None
    rpc_breakdowns: list[RpcBreakdown] = [RpcBreakdown(available=False) for _ in warm.measured_walltimes_s]
    raw_profile_paths: list[str] = []
    for p in warm.profile_paths:
        try:
            raw_profile_paths.append(str(Path(p).relative_to(run_dir)))
        except ValueError:
            raw_profile_paths.append(p)
    median_lat = (
        sorted(warm.measured_latencies_s)[len(warm.measured_latencies_s) // 2]
        if warm.measured_latencies_s else None
    )
    return build_cell_record(
        run_id=run_id, case_id=cell.case.id, function_type=cell.case.function_type,
        vgi_function=cell.case.vgi_function, transport=cell.transport, language=cell.language,
        threads=cell.threads, params=cell.params,
        worker_concurrency_cap=_max_workers_for(cell),
        observed_peak_workers=warm.observed_peak_workers,
        rendered_sql=warm.rendered_sql_first, worker_location=location,
        status=status, error=error, iterations_cfg=iterations_cfg,
        measured_walltimes_s=warm.measured_walltimes_s,
        measured_latencies_s=warm.measured_latencies_s,
        rows_processed=rows,
        profile_summaries=warm.profile_summaries, rpc_breakdowns=rpc_breakdowns,
        bytes_basis=_bytes_basis(cell.case, cell.params, rows, median_lat),
        externalization=cell.case.externalization, raw_profile_paths=raw_profile_paths,
        trim_outliers=trim_outliers,
    )


def run_cell_in_group(
    *,
    entries: list[tuple[int, Cell, Adapter, Any]],
    lang: str,
    transport: str,
    run_id: str,
    run_dir: Path,
    trim_outliers: bool,
    fail_fast: bool,
    records_out: list[tuple[int, dict[str, Any]]],
    skipped_factory: Callable[..., dict[str, Any]],
    manifest_counts: dict[str, int],
    log_label: Callable[[int, int, Cell], str],
    total: int,
) -> None:
    """Run all cells in a (language, transport) group sharing one connection + worker."""
    if not entries:
        return
    first_adapter = entries[0][2]
    first_spec = entries[0][3]
    if not first_adapter.runnable:
        for idx, cell, adapter, _spec in entries:
            print(log_label(idx, total, cell), flush=True)
            rec = skipped_factory(cell, adapter, "adapter not runnable")
            manifest_counts[rec["status"]] = manifest_counts.get(rec["status"], 0) + 1
            records_out.append((idx, rec))
        return

    runid = f"{lang}-{transport}-group"
    raw_dir_root = run_dir / "raw" / f"_group_{lang}_{transport}"
    raw_dir_root.mkdir(parents=True, exist_ok=True)

    # Apply the transport spec's env to os.environ for the duration of the group.
    # This is how *-shm transports communicate VGI_RPC_SHM_SIZE_BYTES to the C++
    # extension side: the env var must be set in the haybarn process *before*
    # haybarn.connect() so getenv() in the extension sees it at ATTACH time.
    import os
    saved_env: dict[str, str | None] = {}
    for k, v in first_spec.env.items():
        saved_env[k] = os.environ.get(k)
        os.environ[k] = v

    # Probe by base transport (subprocess / subprocess-shm both subprocess-backed).
    base_transport = transport.removesuffix("-shm")

    try:
        with WorkerSession(
            transport=base_transport, spec=first_spec, runid=runid,
            pooled=False, max_workers=max(c[1].threads for c in entries),
            log_dir=raw_dir_root,
        ) as worker:
            handle = worker.handle
            assert handle is not None
            location = handle.location

            first_case = entries[0][1].case
            with GroupSession(
                transport=base_transport, worker_location=location,
                attach_name=first_case.attach_name, alias=first_case.alias,
            ) as session:
                for idx, cell, adapter, _spec in entries:
                    print(log_label(idx, total, cell), flush=True)
                    raw_dir = run_dir / "raw" / cell.case.id
                    raw_dir.mkdir(parents=True, exist_ok=True)
                    rows = _expected_rows(cell.case, cell.params)
                    iterations_cfg = {
                        "warmup": cell.case.iterations.warmup,
                        "measured": cell.case.iterations.measured,
                    }
                    if cell.threads > 1 and not cell.case.parallelizable:
                        rec = skipped_factory(
                            cell, adapter,
                            f"case.parallelizable=false; threads={cell.threads} skipped",
                            "skipped",
                        )
                    else:
                        warm = run_cell_warm(
                            case=cell.case, params=cell.params,
                            threads=cell.threads, transport=base_transport,
                            worker_location=location,
                            warmup_count=cell.case.iterations.warmup,
                            measured_count=cell.case.iterations.measured,
                            raw_dir=raw_dir,
                            worker_concurrency_probe=(base_transport == "subprocess" and cell.threads > 1),
                            session=session,
                        )
                        rec = _build_cell_from_warm_result(
                            cell=cell, run_id=run_id, run_dir=run_dir,
                            location=location, warm=warm, rows=rows,
                            iterations_cfg=iterations_cfg, trim_outliers=trim_outliers,
                        )
                        # Record the actual transport (with -shm suffix), not the base.
                        rec["transport"] = transport
                    manifest_counts[rec["status"]] = manifest_counts.get(rec["status"], 0) + 1
                    records_out.append((idx, rec))
                    if fail_fast and rec["status"] == "error":
                        print(f"fail-fast: aborting after error in {cell.case.id}", flush=True)
                        return
    except WorkerError as e:
        for idx, cell, adapter, _spec in entries:
            rec = skipped_factory(cell, adapter, f"worker_error: {e}", "error")
            manifest_counts[rec["status"]] = manifest_counts.get(rec["status"], 0) + 1
            records_out.append((idx, rec))
    finally:
        # Restore env
        for k, prev in saved_env.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev


def run_cell(
    cell: Cell,
    *,
    adapter: Adapter,
    transport_spec: TransportSpec,
    run_id: str,
    run_dir: Path,
    duckdb_binary: str,  # accepted for back-compat; haybarn supplies its own engine
    trim_outliers: bool,
) -> dict[str, Any]:
    raw_dir = run_dir / "raw" / cell.case.id
    raw_dir.mkdir(parents=True, exist_ok=True)

    rows = _expected_rows(cell.case, cell.params)
    iterations_cfg = {
        "warmup": cell.case.iterations.warmup,
        "measured": cell.case.iterations.measured,
    }

    if cell.threads > 1 and not cell.case.parallelizable:
        return build_cell_record(
            run_id=run_id,
            case_id=cell.case.id,
            function_type=cell.case.function_type,
            vgi_function=cell.case.vgi_function,
            transport=cell.transport,
            language=cell.language,
            threads=cell.threads,
            params=cell.params,
            worker_concurrency_cap=_max_workers_for(cell),
            observed_peak_workers=None,
            rendered_sql="",
            worker_location="",
            status="skipped",
            error=f"case.parallelizable=false; threads={cell.threads} skipped",
            iterations_cfg=iterations_cfg,
            measured_walltimes_s=[],
            measured_latencies_s=[],
            rows_processed=rows,
            profile_summaries=[],
            rpc_breakdowns=[],
            bytes_basis=None,
            externalization=cell.case.externalization,
            raw_profile_paths=[],
            trim_outliers=False,
        )

    with WorkerSession(
        transport=cell.transport,
        spec=transport_spec,
        runid=f"{cell.case.id}-{cell.threads}",
        pooled=False,
        max_workers=_max_workers_for(cell),
        log_dir=raw_dir,
    ) as session:
        handle = session.handle
        assert handle is not None
        location = handle.location

        warm = run_cell_warm(
            case=cell.case,
            params=cell.params,
            threads=cell.threads,
            transport=cell.transport,
            worker_location=location,
            warmup_count=cell.case.iterations.warmup,
            measured_count=cell.case.iterations.measured,
            raw_dir=raw_dir,
            worker_concurrency_probe=(cell.transport == "subprocess" and cell.threads > 1),
        )

        if warm.error:
            status = "error"
            error: str | None = warm.error
        else:
            status = "ok"
            error = None

        # RPC timing is not captured in-process today (the C++ TimingDumper writes
        # at process exit on the duckdb CLI). Record as unavailable but keep the field
        # so the schema is stable across runs.
        rpc_breakdowns: list[RpcBreakdown] = [
            RpcBreakdown(available=False) for _ in warm.measured_walltimes_s
        ]

        # Relative profile paths so the record is portable
        raw_profile_paths: list[str] = []
        for p in warm.profile_paths:
            try:
                raw_profile_paths.append(str(Path(p).relative_to(run_dir)))
            except ValueError:
                raw_profile_paths.append(p)

        median_lat = (
            sorted(warm.measured_latencies_s)[len(warm.measured_latencies_s) // 2]
            if warm.measured_latencies_s
            else None
        )
        return build_cell_record(
            run_id=run_id,
            case_id=cell.case.id,
            function_type=cell.case.function_type,
            vgi_function=cell.case.vgi_function,
            transport=cell.transport,
            language=cell.language,
            threads=cell.threads,
            params=cell.params,
            worker_concurrency_cap=_max_workers_for(cell),
            observed_peak_workers=warm.observed_peak_workers,
            rendered_sql=warm.rendered_sql_first,
            worker_location=location,
            status=status,
            error=error,
            iterations_cfg=iterations_cfg,
            measured_walltimes_s=warm.measured_walltimes_s,
            measured_latencies_s=warm.measured_latencies_s,
            rows_processed=rows,
            profile_summaries=warm.profile_summaries,
            rpc_breakdowns=rpc_breakdowns,
            bytes_basis=_bytes_basis(cell.case, cell.params, rows, median_lat),
            externalization=cell.case.externalization,
            raw_profile_paths=raw_profile_paths,
            trim_outliers=trim_outliers,
        )
