#!/usr/bin/env bash
# Smoke-test the VGI benchmark suite end-to-end on the Python subprocess adapter.
# Runs a tiny matrix (scalar_multiply + table_sequence, threads=1,4) to confirm:
#   - ATTACH succeeds against the Python fixture worker
#   - DuckDB JSON profiling parses
#   - VGI_RPC_CLIENT_TIMING stderr line is captured
#   - Worker concurrency probe (subprocess pool) returns observed_peak > 1 at threads=4

set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/.."

OUT_DIR="${OUT_DIR:-results/_smoke}"

echo "==> validate"
uv run vgi-bench validate

echo "==> dry-run (python/subprocess)"
uv run vgi-bench run --dry-run \
    --languages python --transports subprocess \
    --cases scalar_multiply,table_sequence \
    --threads 1,4

echo "==> smoke run"
uv run vgi-bench run \
    --languages python --transports subprocess \
    --cases scalar_multiply,table_sequence \
    --threads 1,4 \
    --iterations-override warmup=1,measured=3,cold_samples=2 \
    --out "$OUT_DIR" \
    --note "smoke run via scripts/smoke.sh"

echo "==> done. Check ${OUT_DIR}/ for env.json, manifest.json, results.jsonl, raw/."
