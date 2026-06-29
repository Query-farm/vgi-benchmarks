# VGI Benchmark Suite

A reproducible, traceable performance harness for VGI (Vector Gateway Interface).
Drives real DuckDB queries against external worker processes via VGI, captures
per-cell timing through DuckDB JSON profiling, and stamps every run with a full
environment fingerprint so numbers are comparable over time.

## What it measures

A **cell** is one point in the test matrix:

```
case × params × transport × language × threads
```

Each cell runs N iterations of one SQL query against one worker. The harness
records throughput in three forms — rows/s, RPC/s (rows/s ÷ DuckDB's
STANDARD_VECTOR_SIZE), and MB/s — plus parallel-scaling efficiency and DuckDB's
own profile fields.

| Metric | Units | When it's the right number to look at |
|--------|-------|---------------------------------------|
| `throughput_rows.median_rows_per_sec` | rows/s | Bulk row counts, single-case scaling curves |
| `throughput_rows.median_rows_per_sec / 2048` | RPC/s | Cross-case framework overhead comparison |
| `byte_throughput.{input,output}_bytes_per_sec_median` | MB/s in/out | Cross-payload bandwidth claims |
| `parallel_scaling.speedup_vs_1t` | × multiplier | "Is adding threads helping?" |
| `worker_concurrency.observed_peak` | int | "Did we actually fan out N ways?" |

## Quickstart

```bash
cd ~/Development/vgi-benchmarks
uv sync                                          # install harness deps
uv run vgi-bench validate                        # schema-check all cases + language configs
uv run vgi-bench run --dry-run                   # expand matrix, show rendered SQL, don't execute
./scripts/smoke.sh                               # ~2 min run that exercises the path
uv run vgi-bench run --cases scalar_multiply \
    --languages python,java,go \
    --transports subprocess,launch,launch-shm \
    --threads 1,4,8 \
    --iterations-override warmup=2,measured=10 \
    --out results/_quick                         # custom quick run
uv run scripts/make_report.py results/_quick/<run_id>   # render PDF
```

## Architecture

The harness is stdlib-only Python at runtime (no `jsonschema`, no `requests`,
no third-party deps) — one process drives DuckDB in-process via the `haybarn`
binding, dispatches worker lifecycle, and writes a single JSONL of results.

```
                         ┌─────────────────────────────────────┐
                         │    vgi-bench run [matrix flags]     │
                         └─────────────────────────────────────┘
                                          │
                            cases/*.json  │   languages/*.json
                                          ▼
                         ┌─────────────────────────────────────┐
                         │  cli.expand_matrix → list[Cell]     │
                         │  (case × params × transport × lang  │
                         │   × threads)                        │
                         └─────────────────────────────────────┘
                                          │
                                          ▼  for each Cell:
                         ┌─────────────────────────────────────┐
                         │  adapter.start_worker(spec)         │
                         │   ── auto-spawn │ HTTP popen        │
                         │   ── unix socket │ launcher rendezvous
                         └─────────────────────────────────────┘
                                          │
                                          ▼
                         ┌─────────────────────────────────────┐
                         │  runner.run_cell(...)               │
                         │   templating.render(...)            │
                         │   db_session.execute(...) ×N        │
                         │   profile_parse + rpc_timing parse  │
                         └─────────────────────────────────────┘
                                          │
                                          ▼
                         ┌─────────────────────────────────────┐
                         │  aggregate.build_record(...)        │
                         │   median, throughput, byte rates    │
                         └─────────────────────────────────────┘
                                          │
                                          ▼
                         ┌─────────────────────────────────────┐
                         │  writers append → results.jsonl     │
                         └─────────────────────────────────────┘
                                  │                     │
                            after matrix:               │
                                  ▼                     ▼
                  parallel_scaling post-pass       env.json + manifest.json
```

### Module map (`vgi_bench/`)

| Module | Responsibility |
|--------|----------------|
| `cli.py` | argparse, matrix expansion, per-cell driver, manifest |
| `config.py` | Load `cases/*.json` + `languages/*.json`, expand `"all"`, cartesian-expand `params` |
| `schema.py` | Embedded JSON schemas + a stdlib structural validator (no jsonschema dep) |
| `models.py` | Frozen dataclasses: `Case`, `Adapter`, `TransportSpec`, `Cell` |
| `fingerprint.py` | Git SHA/dirty for vgi + vgi-python, DuckDB version, OS/arch/CPU/RAM. Builds `run_id = <date>-<vgi-short-sha>[-dirty]` |
| `adapter.py` | Worker lifecycle: build location string, start server modes, ready-check, teardown |
| `templating.py` | Resolve `${alias}` `${location}` `${param:*}` `${attach}` `${profiling_output}` tokens; assemble per-iteration SQL |
| `db_session.py` | In-process DuckDB via `haybarn` (avoids `duckdb -f` spawn overhead) |
| `duckdb_runner.py` | Fallback path: invoke statically-linked `duckdb` CLI |
| `runner.py` | Drives one cell: ATTACH + setup + measured iterations + teardown |
| `profile_parse.py` | DuckDB `PRAGMA enable_profiling='json'` → `latency`, `rows_returned`, `cumulative_rows_scanned`, operator timings |
| `rpc_timing.py` | Parse `VGI_RPC_CLIENT_TIMING=1` stderr; per-pass ns + batches |
| `payload.py` | Resolve `payload.input_bytes_per_row` formulas (literals, sums, products of params) into per-cell ints |
| `parallelism.py` | Build the parallelism setup SQL (`SET threads=N`, `SET vgi_worker_pool_max=N`) and probe `vgi_worker_pool()` for observed concurrency |
| `stats.py` | min/median/mean/p95/stdev (`statistics`), optional IQR outlier trim |
| `aggregate.py` | Combine raw samples → one `CellResult`; post-pass joins t>1 cells to their t=1 sibling for scaling speedup |
| `writers.py` | `results/<run_id>/{env.json, manifest.json, results.jsonl, raw/}` |

### Scripts

| Script | Purpose |
|--------|---------|
| `scripts/smoke.sh` | ~2-min sanity run on python+subprocess. Run after any harness change. |
| `scripts/run_all.sh` | Full matrix template; pass through `--note` to stamp the run |
| `scripts/make_report.py` | Read one `results/<run_id>/` and render a Typst → PDF report |

## Directory layout

```
cases/                  Active benchmark definitions — one JSON per case
cases-extended/         Definitions kept around but not in the active set
languages/              Language-adapter configs — one JSON per language
vgi_bench/              Python harness package (stdlib-only at runtime)
scripts/                smoke.sh, run_all.sh, make_report.py
results/<run_id>/       Committed outputs:
   env.json              host + git SHAs + DuckDB version (one per run)
   manifest.json         cli_args + matrix counts + wall time
   results.jsonl         one line per cell (the canonical data file)
   raw/                  per-cell DuckDB profile JSONs + RPC timing blocks
   report.typ / .pdf     rendered report (regenerable via make_report.py)
pyproject.toml          uv project; console script `vgi-bench`; runtime deps = stdlib only
```

A `run_id` is `<UTC-date>-<vgi-short-sha>` with a `-dirty` suffix when either
the `vgi` or `vgi-python` tree has uncommitted changes. Each run dir is
self-describing — re-executing the same `manifest.json.cli_args` against the
recorded SHAs reproduces the measurement.

## Adding a new case

A case is a declarative JSON file under `cases/`. Minimum required fields:

```json
{
  "schema_version": 1,
  "id": "my_case",
  "function_type": "scalar",                // scalar | table | table_in_out | aggregate
  "vgi_function": "my_function_name",
  "requires_attach": true,
  "call_qualified": true,                    // ${alias}.my_function_name vs my_function_name
  "alias": "example",
  "attach_name": "example",
  "setup_sql": [
    "CREATE OR REPLACE TEMP TABLE bench_input AS SELECT i::BIGINT AS n FROM range(${param:rows}) t(i);"
  ],
  "query_sql": "SELECT sum(${alias}.my_function_name(n)) FROM bench_input;",
  "teardown_sql": ["DROP TABLE IF EXISTS bench_input;"],
  "params": { "rows": [10000000] },          // cartesian-expanded
  "param_defaults": { "rows": 10000000 },    // used when the user doesn't pin params
  "threads": [1, 4, 8],                      // default sweep (overridable via --threads)
  "parallelizable": true,                    // when false, t>1 cells are skipped
  "payload": {
    "input_bytes_per_row": "8",              // literal int OR an arithmetic formula
    "output_bytes_per_row": "8",             //   referencing ${param:foo} keys
    "schema_in":  "BIGINT n",
    "schema_out": "BIGINT result",
    "notes": "what an input/output row looks like on the wire"
  },
  "iterations": { "warmup": 2, "measured": 10 },
  "applies_to": {                            // gates: harness skips cells outside these
    "transports": "all",                     // or a list
    "languages": ["python", "java", "go"]
  },
  "metric_tags": ["throughput_rows", "throughput_bytes"],
  "notes": "Why this case exists; what to be careful about."
}
```

Tokens available in `setup_sql`, `query_sql`, `teardown_sql`:

| Token | Resolves to |
|-------|-------------|
| `${alias}` | `case.alias` (defaults to `example`) |
| `${attach}` | the ATTACH statement the runner emits |
| `${location}` | the transport-resolved LOCATION (worker command / http URL / unix path) |
| `${param:rows}` | the cell's `params.rows` value (and same for any other params key) |
| `${profiling_output}` | the per-iteration profile JSON path |

### Important gotchas

- **DuckDB elides scalar projections** that aren't referenced. `count(*) FROM
  (SELECT scalar(x) FROM ...)` doesn't actually call the scalar. Use
  `sum(scalar(x))` or `count(scalar(x))` to force evaluation.
- **`payload.input_bytes_per_row` formulas must use only `${param:foo}` references
  and arithmetic** — see `vgi_bench/payload.py` for the parser. No function
  calls, no string concat. If the formula doesn't resolve to an int the
  byte-throughput metric falls back to `source: "n/a"`.
- **`parallelizable: false` is honoured silently** — t>1 cells just don't run.
  Set it correctly or you'll get a confusing flat-scaling curve.
- **`payload_bytes` in a string-column case** is the user-visible string length;
  the Arrow representation adds a 4-byte offset per row. Account for it in the
  `payload.input_bytes_per_row` formula (e.g. `"4 + ${param:payload_bytes}"`).

## Adding a new language adapter

A language adapter is a JSON file under `languages/`. Marking it `"runnable":
true` enables it; `false` means "config present but the harness skips it." Full
shape:

```json
{
  "schema_version": 1,
  "language": "myimpl",
  "display_name": "MyImpl (vgi-myimpl example worker)",
  "runnable": true,
  "repo_path": "~/Development/vgi-myimpl",
  "build": {
    "commands": [["make", "build"]],
    "cwd": "~/Development/vgi-myimpl",
    "notes": "produces ./vgi-example-worker-myimpl"
  },
  "supported_function_types": ["scalar", "table", "table_in_out", "aggregate"],
  "version_command": ["myimpl", "--version"],
  "transports": {
    "subprocess": {                          // transport key — used in --transports
      "kind": "subprocess",                  // subprocess | server | launch
      "command": ["~/Development/vgi-myimpl/vgi-example-worker-myimpl"],
      "cwd": null,
      "env": {},
      "ready": { "method": "none" },         // none | log_regex | http_poll | socket_file
      "location_template": "~/Development/vgi-myimpl/vgi-example-worker-myimpl",
      "pooling": { "supported": true, "values": [false, true] },
      "teardown": "none"
    },
    "launch-shm": {
      "kind": "launch",
      "command": ["~/Development/vgi-myimpl/vgi-example-worker-myimpl"],
      "env": { "VGI_RPC_SHM_SIZE_BYTES": "67108864" },
      "ready": { "method": "none" },
      "location_template": "launch:~/Development/vgi-myimpl/vgi-example-worker-myimpl",
      "pooling": { "supported": true, "values": [false, true] },
      "teardown": "none",
      "notes": "SHM-enabled variant"
    }
  },
  "notes": "anything reviewers need to know about this language's worker"
}
```

Per-transport `kind` values:

- **`subprocess`** — DuckDB spawns the worker per ATTACH. `location_template` is the bare worker command.
- **`server`** — Harness pre-spawns one long-lived process; readiness via log regex / socket file / http poll. `location_template` references `${port}` or `${socket_path}`.
- **`launch`** — VGI launcher rendezvous (one worker process per `(cmd, env)` tuple, reused across ATTACHes). `location_template` starts with `launch:`.

For `ready.method`:

- **`none`** — start, sleep a bit, assume ready (works for `launch` because the launcher handles rendezvous timing).
- **`log_regex`** — `{ stream: "stderr", pattern: "PORT:(\\d+)" }` — capture a group into `${port}` or `${socket_path}`.
- **`http_poll`** — `{ url_path: "/health", timeout_s: 10 }` — repeatedly GET until 200 or timeout.
- **`socket_file`** — `{ path: "<resolved>", timeout_s: 10 }` — wait for the socket file to appear.

Once written, validate:

```bash
uv run vgi-bench validate
uv run vgi-bench run --dry-run --languages myimpl --transports subprocess
```

A dry run prints the expanded matrix + the rendered SQL the cell would have
sent to DuckDB. Useful for confirming `location_template` resolves correctly
before you commit a build step.

## Iteration tuning

`--iterations-override warmup=N,measured=M` controls iteration counts at runtime
(overrides each case's `iterations` block). Trade-offs:

| Profile | warmup | measured | Wall (per cell) | Suitable for |
|---------|-------:|---------:|-----------------|--------------|
| Quick smoke | 0 | 2 | seconds | "does this run at all?" |
| Routine bench | 2 | 8 | tens of seconds | day-to-day comparisons |
| Steady-state | 5 | 25 | minutes | publishable / cross-host claims |
| High-thread stability | 5 | 40 | minutes | quieting t=4..8 variance on noisy hosts |

JVM-based workers (Java) benefit from `warmup ≥ 3` so the JIT compiles the hot
path before measurement. Go and Python workers don't need it — `warmup=0` is
fine.

## Report rendering

`scripts/make_report.py <run_dir>` reads `results.jsonl` + `env.json` +
`manifest.json` and emits a Typst document next to them, then compiles to PDF
via `typst compile`. Open the PDF with macOS `open` automatically.

The report is composed top-down in `make_report.py`:

1. **Environment** — host + DuckDB + git SHAs + cell counts + wall time
2. **Reading this report** — explains M rows/s vs K RPC/s vs MB/s
3. **Steady-state throughput** — per-case table, one subtable per thread count
4. **Parallel scaling** — per (language, transport) table, rows = cases, columns = (M r/s, speedup, efficiency) per thread count with green/amber/red efficiency colouring
5. **SHM impact** — paired non-shm/shm by (language, base transport, case, threads)
6. **Payload schemas** — declared `payload` blocks per case (reference)
7. **Findings** — auto-extracted peaks
8. **Methodology** + **Caveats** + **Reproducibility**

To customise the report, edit `make_report.py` directly — it's a single file
that builds the Typst source line-by-line. No template engine.

## Result schema (one line per cell in `results.jsonl`)

```jsonc
{
  "schema_version": 1,
  "run_id": "2026-05-30T1234-abc1234-dirty",
  "case_id": "scalar_multiply",
  "function_type": "scalar",
  "vgi_function": "multiply",
  "transport": "launch-shm",
  "language": "java",
  "threads": 4,
  "params": { "rows": 10000000 },
  "worker_concurrency": {
    "cap": 4,
    "observed_peak": 4,
    "source": "vgi_worker_pool" | "configured"
  },
  "status": "ok" | "error" | "skipped",
  "error": null | "string",
  "iterations": {
    "warmup": 2, "measured": 10,
    "measured_walltime_s": [...],
    "measured_duckdb_latency_s": [...],
    "outliers_trimmed_walltime": 0,
    "outliers_trimmed_latency": 0
  },
  "rows_processed": 10000000,
  "metrics": {
    "throughput_rows": { "median_rows_per_sec": ..., "basis": "duckdb_latency_s" },
    "calls": { "calls_per_sec_median": ..., "per_call_latency_us_median": ..., "note": "..." },
    "rpc_breakdown_ms": { "available": bool, "n": int, "median": {...}, "samples": [...] },
    "byte_throughput": {
      "input_bytes_per_row": int, "output_bytes_per_row": int,
      "input_bytes_total": int, "output_bytes_total": int,
      "input_bytes_per_sec_median": float, "output_bytes_per_sec_median": float,
      "source": "case.payload formula",
      "schema_in": "...", "schema_out": "..."
    },
    "parallel_scaling": {
      "speedup_vs_1t": float,           // populated by aggregate post-pass
      "efficiency": float,
      "baseline_run_id": "..."
    }
  },
  "raw": { "profile_paths_relative": [...] }
}
```

Use `results.jsonl` as the canonical source for any custom analysis — the PDF
report is one view, not the source of truth. JSONL is line-delimited so it
streams cleanly through `jq`, pandas, polars, or DuckDB itself.

## Caveats

- **Laptop benchmarks are noisy.** macOS P/E-core asymmetry, thermal
  throttling, background load. Compare runs from the same host (matching
  `env.json`); cross-host comparison is meaningful only as ratios.
- **DuckDB's `STANDARD_VECTOR_SIZE` is compiled-in.** The "RPC/s = rows/s ÷
  2048" relationship holds against a stock DuckDB build. If you've changed it,
  update `ROWS_PER_RPC` in `scripts/make_report.py`.
- **High-thread variance is real.** At t=4..8 on Apple Silicon, run-to-run
  variance can be ±15%. For publishable numbers use ≥ 25 measured iterations and
  run 2-3 times.
- **Byte counts are declared, not measured.** `payload.input_bytes_per_row`
  describes the Arrow payload; it excludes IPC framing overhead and any
  compression. The MB/s number is a clean lower bound, not an exact wire
  measurement.
- **Adding a new function to a worker can break `function_registration.test`** in
  the upstream vgi integration suite (it counts registered functions). That's
  not this suite's concern, but watch for it if you flip a new function on.

## Where to look next

- Plan / design rationale: `~/.claude/plans/right-now-we-have-curious-stonebraker.md`
- VGI extension: `~/Development/vgi/`
- Reference workers: `~/Development/vgi-python/`, `~/vgi-java/`, `~/Development/vgi-go/`
- RPC layer: `~/Development/vgi-rpc-python/`, `~/Development/vgi-rpc-go/`, `~/Development/vgi-rpc-java/`
