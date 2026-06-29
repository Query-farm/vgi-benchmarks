"""Invoke the statically-linked DuckDB CLI for one iteration."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CliResult:
    rc: int
    stdout: str
    stderr: str
    walltime_s: float
    timed_out: bool


def run_duckdb(
    *,
    duckdb_binary: str,
    sql_file: Path,
    env: dict[str, str],
    timeout_s: float = 600.0,
) -> CliResult:
    cmd = [duckdb_binary, "-unsigned", "-f", str(sql_file)]
    full_env = {**os.environ, **env}
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            env=full_env,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        return CliResult(
            rc=-1,
            stdout=(exc.stdout or b"").decode("utf-8", errors="replace"),
            stderr=(exc.stderr or b"").decode("utf-8", errors="replace"),
            walltime_s=time.perf_counter() - t0,
            timed_out=True,
        )
    return CliResult(
        rc=proc.returncode,
        stdout=proc.stdout.decode("utf-8", errors="replace"),
        stderr=proc.stderr.decode("utf-8", errors="replace"),
        walltime_s=time.perf_counter() - t0,
        timed_out=False,
    )
