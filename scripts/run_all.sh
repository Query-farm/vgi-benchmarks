#!/usr/bin/env bash
# Run the full matrix for every runnable adapter across subprocess+http+unix transports.
# Output lands in ./results/<run_id>/ which is meant to be committed to git.

set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/.."

uv run vgi-bench validate
uv run vgi-bench run \
    --transports subprocess,http,unix \
    --languages python \
    --note "${VGI_BENCH_NOTE:-full run via scripts/run_all.sh}"
