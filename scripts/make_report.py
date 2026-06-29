#!/usr/bin/env python3
"""Render a Typst PDF report from a committed benchmark run.

Usage: make_report.py [<run_dir>]  (defaults to the newest run under results/)
"""
from __future__ import annotations

import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# DuckDB ships rows to a vectorized scalar/aggregate function in chunks of
# STANDARD_VECTOR_SIZE (compiled default = 2048). Each chunk is one VGI RPC
# round-trip (input batch in, output batch out). Reporting "RPCs/sec" makes
# the framework dispatch cost legible — rows/sec is misleading because the
# per-row work is just an arithmetic loop inside one RPC.
ROWS_PER_RPC = 2048


def fmt_int(n: float | int | None) -> str:
    if n is None:
        return "—"
    return f"{n:,.0f}"


def fmt_mrows(rows_per_sec: float | None) -> str:
    if rows_per_sec is None or rows_per_sec <= 0:
        return "—"
    return f"{rows_per_sec / 1_000_000:.1f}"


def fmt_krpc(rows_per_sec: float | None) -> str:
    if rows_per_sec is None or rows_per_sec <= 0:
        return "—"
    return f"{(rows_per_sec / ROWS_PER_RPC) / 1_000:.1f}"


def fmt_mbps(bps: float | None) -> str:
    if bps is None or bps <= 0:
        return "—"
    return f"{bps / 1_000_000:.0f}"


def pick_run_dir(argv: list[str]) -> Path:
    if len(argv) > 1:
        return Path(argv[1]).resolve()
    candidates = sorted(p for p in (ROOT / "results").glob("*-*") if p.is_dir() and not p.name.startswith("_"))
    if not candidates:
        sys.exit("no run dirs under results/")
    return candidates[-1]


def rps_of(r: dict) -> float | None:
    return r["metrics"]["throughput_rows"].get("median_rows_per_sec")


def combined_bps_of(r: dict) -> float:
    bt = r["metrics"]["byte_throughput"]
    return (bt.get("input_bytes_per_sec_median") or 0) + (bt.get("output_bytes_per_sec_median") or 0)


def main() -> None:
    run_dir = pick_run_dir(sys.argv)
    env = json.loads((run_dir / "env.json").read_text())
    mani = json.loads((run_dir / "manifest.json").read_text())
    records = [json.loads(line) for line in (run_dir / "results.jsonl").open()]

    ok_records = [r for r in records if r["status"] == "ok"]

    # ----- compose .typ -----
    typ_path = run_dir / "report.typ"
    pdf_path = run_dir / "report.pdf"
    L: list[str] = []

    L.append('#set document(title: "VGI Benchmark Report", author: "vgi-bench")')
    L.append(
        '#set page(paper: "us-letter", margin: 0.85in, header: align(right, '
        'text(size: 8pt, fill: gray)[VGI bench · ' + env["run_id"] + ']))'
    )
    L.append('#set text(font: "New Computer Modern", size: 10pt)')
    L.append('#set heading(numbering: "1.1")')
    L.append('#show heading.where(level: 1): it => block(above: 1.4em, below: 0.6em, text(size: 16pt, weight: "bold", it))')
    L.append('#show heading.where(level: 2): it => block(above: 1.0em, below: 0.4em, text(size: 12pt, weight: "bold", it))')
    L.append('#show heading.where(level: 3): it => block(above: 0.8em, below: 0.3em, text(size: 11pt, weight: "bold", it))')
    L.append('#show raw: it => box(fill: rgb("#f4f4f4"), inset: (x: 3pt, y: 1pt), radius: 2pt, text(size: 8.5pt, font: "Menlo", it))')
    L.append("")

    # Title
    L.append('#align(center)[#text(size: 22pt, weight: "bold")[VGI Performance Benchmark]]')
    L.append('#align(center)[#text(size: 11pt, fill: gray)[Run `' + env["run_id"] + '` · ' + env["timestamp_utc"] + ']]')
    L.append("#v(1em)")

    # ---- Environment ----
    L.append("== Environment")
    L.append("#table(columns: (auto, 1fr), stroke: 0.4pt + gray, inset: (x: 6pt, y: 4pt),")
    L.append(
        f'  [*Host*], [{env["hostname"]} · {env["cpu"]} ({env["cpu_count"]} cores) · '
        f'{env["os"]} {env["arch"]} · {env["mem_total_mb"]} MB RAM],'
    )
    L.append(f'  [*DuckDB*], [`{env["duckdb_version"]}` at `{env["duckdb_binary_path"]}`],')
    L.append(f'  [*vgi*], [`{env["vgi_git_sha"]}`' + (" (dirty)" if env["vgi_git_dirty"] else "") + "],")
    L.append(f'  [*vgi-python*], [`{env["vgi_python_git_sha"]}`' + (" (dirty)" if env["vgi_python_git_dirty"] else "") + "],")
    L.append(f'  [*Harness*], [`vgi-bench v{env["harness_version"]}`],')
    L.append(
        f'  [*Cells*], [{mani["matrix_size"]} '
        f'(ok={mani["counts"].get("ok", 0)}, error={mani["counts"].get("error", 0)}, skipped={mani["counts"].get("skipped", 0)})],'
    )
    L.append(f'  [*Wall*], [{mani["wall_seconds"]:.0f} s ({mani["wall_seconds"] / 60:.1f} min)],')
    if env.get("note"):
        L.append(f'  [*Note*], [{env["note"]}],')
    L.append(")")
    L.append("")

    # ---- Reading-this-report intro ----
    L.append("== Reading this report")
    L.append(
        f"DuckDB hands a vectorized scalar function ~{ROWS_PER_RPC:,} rows at a time. Each handoff is one "
        "VGI RPC round-trip: a request batch out to the worker, a response batch back. We report "
        "throughput two ways:"
    )
    L.append("")
    L.append("- *M rows/s* — useful for sizing actual data flow. Scales with row width, so it is not directly comparable across cases with different payload sizes.")
    L.append(f"- *K RPC/s* — same number divided by {ROWS_PER_RPC:,}. This is the dispatch rate the framework is actually sustaining; it is what's bounded by transport overhead, allocator pressure, and the worker's per-batch fixed cost. Compare *this* across cases to see how the framework holds up under different shapes of payload.")
    L.append("")
    L.append("Where a case declares a per-row Arrow payload size we also report *MB/s in/out* (Arrow payload × rows ÷ latency). That's the cleanest absolute bandwidth number for that case.")
    L.append("")

    # ---- Steady-state throughput by case ----
    L.append("== Steady-state throughput")
    L.append(
        "Each case shows every (language, transport) combination at its largest configured row count. "
        "Sorted by RPC/s within each thread group so the framework-overhead picture is visible directly."
    )
    L.append("")

    # group records by case
    by_case: dict[str, list[dict]] = defaultdict(list)
    for r in ok_records:
        by_case[r["case_id"]].append(r)

    for case_id in sorted(by_case):
        rs = by_case[case_id]
        largest_rows = max(r["params"].get("rows", 0) for r in rs)
        rs_largest = [r for r in rs if r["params"].get("rows") == largest_rows]
        threads_in_set = sorted({r["threads"] for r in rs_largest})

        L.append(f"=== `{case_id}`")
        # one-line description if present
        param_str = "; ".join(f"`{k}={v}`" for k, v in (rs_largest[0].get("params", {}).items()))
        L.append(f"At {param_str}.")
        L.append("")

        # One table per thread count — keeps the per-thread comparison legible
        for t in threads_in_set:
            rs_t = sorted(
                (r for r in rs_largest if r["threads"] == t),
                key=lambda r: -(rps_of(r) or 0),
            )
            if not rs_t:
                continue
            L.append(f"==== threads = {t}")
            L.append("#table(columns: (auto, auto, auto, auto, auto, auto),")
            L.append("  stroke: 0.4pt + gray, inset: (x: 5pt, y: 3pt),")
            L.append("  align: (left, left, right, right, right, right),")
            L.append("  [*Lang*], [*Transport*], [*M rows/s*], [*K RPC/s*], [*MB/s in*], [*MB/s out*],")
            for r in rs_t:
                rps = rps_of(r)
                bt = r["metrics"]["byte_throughput"]
                L.append(
                    f'  [{r["language"]}], [{r["transport"]}], '
                    f'[{fmt_mrows(rps)}], [{fmt_krpc(rps)}], '
                    f'[{fmt_mbps(bt.get("input_bytes_per_sec_median"))}], '
                    f'[{fmt_mbps(bt.get("output_bytes_per_sec_median"))}],'
                )
            L.append(")")
            L.append("")

    # ---- Parallel scaling — broken up per (language, transport) ----
    L.append("== Parallel scaling")
    L.append(
        "How well does each (language, transport) combination convert added DuckDB worker threads into "
        "throughput? *speedup* is rows/sec at N threads ÷ rows/sec at threads=1 for the same case. *efficiency* is speedup ÷ threads "
        "(100% = linear scaling). Numbers below ~50% efficiency mean the added thread is more contention than useful work."
    )
    L.append("")
    L.append(
        "This section *intentionally splits* by (language, transport): different runtimes hit different "
        "scaling ceilings (allocator contention, GC, scheduler overhead, OS thread vs virtual thread), and "
        "averaging them hides the interesting differences."
    )
    L.append("")

    # build (lang, transport, case) → {threads: rows/s}
    triple: dict[tuple[str, str, str], dict[int, float]] = defaultdict(dict)
    for r in ok_records:
        rps = rps_of(r)
        if rps is None or rps <= 0:
            continue
        triple[(r["language"], r["transport"], r["case_id"])][r["threads"]] = rps

    # group by (lang, transport)
    lang_transports = sorted({(k[0], k[1]) for k in triple})

    for (lang, transport) in lang_transports:
        L.append(f"=== {lang} + {transport}")

        # for each case the lang+transport ran, build the scaling row
        cases_in_lt = sorted({k[2] for k in triple if k[0] == lang and k[1] == transport})
        if not cases_in_lt:
            continue

        # collect threads union across cases for column headers
        all_threads: set[int] = set()
        for c in cases_in_lt:
            all_threads.update(triple[(lang, transport, c)].keys())
        threads_sorted = sorted(all_threads)
        if 1 not in threads_sorted:
            L.append("(no threads=1 baseline available, skipping)")
            L.append("")
            continue

        # columns: case + per-thread (M rows/s, speedup, efficiency)
        ncols = 1 + 3 * len(threads_sorted)
        col_spec = "auto, " + ", ".join(["auto"] * (3 * len(threads_sorted)))
        align_spec = "left, " + ", ".join(["right"] * (3 * len(threads_sorted)))
        L.append(f"#table(columns: ({col_spec}),")
        L.append("  stroke: 0.4pt + gray, inset: (x: 4pt, y: 3pt),")
        L.append(f"  align: ({align_spec}),")
        # header
        header_cells = ["[*Case*]"]
        for t in threads_sorted:
            header_cells.append(f"[*{t}t M r/s*]")
            header_cells.append(f"[*{t}t speedup*]")
            header_cells.append(f"[*{t}t eff%*]")
        L.append("  " + ", ".join(header_cells) + ",")
        # one row per case
        for case_id in cases_in_lt:
            row_data = triple[(lang, transport, case_id)]
            base = row_data.get(1)
            cells = [f"[`{case_id}`]"]
            for t in threads_sorted:
                rps = row_data.get(t)
                if rps is None:
                    cells.extend(["[—]", "[—]", "[—]"])
                    continue
                cells.append(f"[{fmt_mrows(rps)}]")
                if base:
                    sp = rps / base
                    eff = (sp / t) * 100
                    # color efficiency: green if >75%, amber 50-75%, red <50%
                    color = "#2a8" if eff >= 75 else ("#c80" if eff >= 50 else "#c33")
                    cells.append(f"[{sp:.2f}×]")
                    cells.append(f'[#text(fill: rgb("{color}"))[{eff:.0f}%]]')
                else:
                    cells.extend(["[—]", "[—]"])
            L.append("  " + ", ".join(cells) + ",")
        L.append(")")
        L.append("")

    # ---- SHM impact (shm transport vs its non-shm base) ----
    L.append("== SHM impact")
    L.append(
        "Pair each `*-shm` transport with its non-shm base at the same (language, case, threads). "
        "Positive delta = SHM is faster; negative = SHM's negotiation + per-batch shm-mailbox cost "
        "exceeds whatever syscall savings it would have unlocked."
    )
    L.append("")

    # collect pairs
    pairs: dict[tuple[str, str, str, int], dict[str, float]] = defaultdict(dict)
    for r in ok_records:
        rps = rps_of(r)
        if rps is None or rps <= 0:
            continue
        tr = r["transport"]
        base = tr[: -len("-shm")] if tr.endswith("-shm") else tr
        kind = "shm" if tr.endswith("-shm") else "base"
        pairs[(r["language"], base, r["case_id"], r["threads"])][kind] = rps

    impact_rows: list[tuple[str, str, str, int, float, float]] = []
    for key, kinds in pairs.items():
        if "base" in kinds and "shm" in kinds:
            lang, base, case_id, threads = key
            impact_rows.append((lang, base, case_id, threads, kinds["base"], kinds["shm"]))

    if impact_rows:
        L.append("#table(columns: (auto, auto, auto, auto, auto, auto, auto),")
        L.append("  stroke: 0.4pt + gray, inset: (x: 5pt, y: 3pt),")
        L.append("  align: (left, left, left, right, right, right, right),")
        L.append("  [*Lang*], [*Base transport*], [*Case*], [*Threads*], [*Base M r/s*], [*SHM M r/s*], [*SHM impact*],")
        for row in sorted(impact_rows, key=lambda x: (x[0], x[1], x[2], x[3])):
            lang, base, case_id, threads, b_rps, s_rps = row
            delta = (s_rps / b_rps) - 1.0
            color = "#2a8" if delta > 0.05 else ("#c33" if delta < -0.05 else "gray")
            L.append(
                f'  [{lang}], [{base}], [`{case_id}`], [{threads}], '
                f'[{fmt_mrows(b_rps)}], [{fmt_mrows(s_rps)}], '
                f'[#text(fill: rgb("{color}"))[{delta * 100:+.0f}%]],'
            )
        L.append(")")
    else:
        L.append("(no matched non-shm/shm pairs in this run)")
    L.append("")

    # ---- Payload schemas (reference) ----
    L.append("== Payload schemas")
    L.append("Per-row Arrow payload sizes declared in each case's `payload` block, used to derive the MB/s numbers above.")
    L.append("")
    L.append("#table(columns: (auto, auto, auto, 1.4fr, 1.4fr),")
    L.append("  stroke: 0.4pt + gray, inset: (x: 5pt, y: 3pt), align: (left, right, right, left, left),")
    L.append("  [*Case*], [*in B/row*], [*out B/row*], [*Schema in*], [*Schema out*],")
    seen_cases: set[str] = set()
    for r in sorted(ok_records, key=lambda r: r["case_id"]):
        if r["case_id"] in seen_cases:
            continue
        seen_cases.add(r["case_id"])
        bt = r["metrics"]["byte_throughput"]
        L.append(
            f'  [`{r["case_id"]}`], '
            f'[{fmt_int(bt.get("input_bytes_per_row"))}], '
            f'[{fmt_int(bt.get("output_bytes_per_row"))}], '
            f'[{bt.get("schema_in", "")[:60]}], '
            f'[{bt.get("schema_out", "")[:60]}],'
        )
    L.append(")")
    L.append("")

    # ---- Findings ----
    L.append("== Findings")

    # Peak rows/sec
    top = max(ok_records, key=lambda r: rps_of(r) or 0, default=None)
    if top:
        rps = rps_of(top)
        krpc = (rps or 0) / ROWS_PER_RPC / 1_000
        L.append(
            f'- *Peak throughput in this run: {fmt_mrows(rps)} M rows/s ({krpc:.1f} K RPC/s)* — '
            f'`{top["case_id"]}` · {top["language"]} · {top["transport"]} · threads={top["threads"]}.'
        )

    # Peak combined bytes/sec
    top_bps = max(((combined_bps_of(r), r) for r in ok_records), default=(0, None))
    if top_bps[1] and top_bps[0] > 0:
        L.append(
            f'- *Peak combined data throughput: \\~{top_bps[0] / 1_000_000:.0f} MB/s* '
            f'({top_bps[1]["case_id"]} · {top_bps[1]["transport"]} · '
            f'threads={top_bps[1]["threads"]}). Actual Arrow payload bytes shipped through the wire.'
        )

    # Best scaling row
    best_scaling: tuple[float, str, str, str, int] | None = None
    for (lang, transport, case_id), thread_map in triple.items():
        base = thread_map.get(1)
        if not base:
            continue
        for t, rps in thread_map.items():
            if t == 1:
                continue
            sp = rps / base
            eff = sp / t
            if best_scaling is None or eff > best_scaling[0]:
                best_scaling = (eff, lang, transport, case_id, t)
    if best_scaling:
        eff, lang, transport, case_id, t = best_scaling
        L.append(
            f'- *Best parallel efficiency: {eff * 100:.0f}% at threads={t}* — '
            f'{lang} + {transport} on `{case_id}`. Combinations significantly below this are hitting '
            f'their runtime\'s contention floor (allocator, GC, scheduler).'
        )

    L.append(
        f'- *RPC framing dominates at small payloads.* DuckDB ships ~{ROWS_PER_RPC:,} rows per RPC; '
        'a scalar reporting 10 M rows/s is doing only \\~5 K RPC/s. Differences between transports '
        'at small payloads are first-order driven by per-RPC overhead, not by bulk-byte cost.'
    )
    L.append(
        '- *Naive `count(*) FROM (SELECT scalar(x) FROM ...)` does not call the scalar.* The optimizer '
        'drops the projection because `count(*)` does not reference it. Cases use `sum(scalar(x))` or '
        '`count(scalar(x))` to force evaluation.'
    )
    L.append("")

    # ---- Cross-language status ----
    langs_present = sorted({r["language"] for r in ok_records})
    L.append("== Cross-language status")
    L.append(
        f"Adapters in this run: {', '.join('`' + lng + '`' for lng in langs_present)}. "
        "Other language SDKs (`typescript`, `rust`, `kotlin`, `cpp`) are committed as `runnable: false` "
        "stubs — enabling them is a config flip once the worker's readiness-line format is captured."
    )
    L.append("")

    # ---- Methodology ----
    L.append("== Methodology")
    L.append("Each cell = `case × params × transport × language × threads`. Per cell:")
    L.append("")
    L.append("+ *Warmup* — N iterations discarded; primes the OS page cache, the worker JIT (for Java), and the launcher worker pool.")
    L.append("+ *Measured* — N iterations through DuckDB with `PRAGMA enable_profiling='json'`. `duckdb_latency_s` comes from the profile's `latency` field; `rows_processed` from `cumulative_rows_scanned`.")
    L.append("+ *M rows/s* = `rows_processed / median(duckdb_latency_s)`.")
    L.append(f"+ *K RPC/s* = same number ÷ {ROWS_PER_RPC:,} (the compiled DuckDB STANDARD_VECTOR_SIZE — one RPC per output vector for scalar/aggregate; table/table_in_out reuses the same number as a proxy).")
    L.append("+ *MB/s in/out* = declared `bytes_per_row × rows_processed / median_latency` per direction.")
    L.append("+ *Worker concurrency probe* — first measured iteration appends a pool-stats query so `observed_peak` workers is recorded; meaningful primarily for subprocess transport.")
    L.append("")

    # ---- Caveats ----
    L.append("== Caveats")
    L.append(
        f'- *Laptop benchmark.* macOS P/E-core asymmetry, thermal throttling; loadavg at start was '
        f'{env["loadavg"][0]:.1f}. Compare runs within the same host/SHA.'
    )
    L.append(
        f'- *DuckDB chunk size is compiled-in.* The {ROWS_PER_RPC}-rows-per-RPC assumption holds only against '
        'a stock DuckDB build with the default `STANDARD_VECTOR_SIZE`. The RPC/s numbers shift if that changes.'
    )
    L.append(
        '- *Variance is real at high thread counts.* On Apple Silicon (4 P + 4 E cores) the t=4..8 numbers '
        'see ±5–15% run-to-run variance from scheduler placement; medians of 25–40 iterations are needed for stable readings.'
    )
    L.append(
        '- *Bytes/row counts Arrow payload bytes, not wire bytes.* No compression, IPC framing overhead, or shared-memory transport tax is in the denominator. The MB/s number is a clean lower bound on data shipped, not an exact wire measurement.'
    )
    L.append("")

    L.append("== Reproducibility")
    L.append("```bash")
    L.append("cd ~/Development/vgi-benchmarks")
    L.append("git -C ../.. checkout " + env["vgi_git_sha"] + "   # vgi")
    L.append("# (and the matching vgi-python SHA " + env["vgi_python_git_sha"] + ")")
    L.append("./scripts/run_all.sh")
    L.append("```")
    L.append("")
    L.append(
        "Per-cell raw SQL + DuckDB profile JSON live under `" + str(run_dir.relative_to(ROOT))
        + "/raw/`. Re-rendering the same SQL against the same DuckDB binary at the same SHA reproduces the measurement."
    )
    L.append("")

    typ_path.write_text("\n".join(L))
    print(f"wrote {typ_path}")
    subprocess.run(["typst", "compile", str(typ_path), str(pdf_path)], check=True)
    print(f"wrote {pdf_path}")
    subprocess.run(["open", str(pdf_path)], check=True)
    print(f"opened {pdf_path}")


if __name__ == "__main__":
    main()
