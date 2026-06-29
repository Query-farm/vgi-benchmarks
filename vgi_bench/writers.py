"""Result writers: run-level env + manifest + per-cell records jsonl."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from vgi_bench.models import EnvFingerprint


def make_run_dir(results_root: Path, run_id: str) -> Path:
    run_dir = results_root / run_id
    (run_dir / "raw").mkdir(parents=True, exist_ok=True)
    return run_dir


def write_env(run_dir: Path, env: EnvFingerprint) -> Path:
    path = run_dir / "env.json"
    _atomic_write(path, asdict(env))
    return path


def write_manifest(run_dir: Path, manifest: dict[str, Any]) -> Path:
    path = run_dir / "manifest.json"
    _atomic_write(path, manifest)
    return path


def append_record(run_dir: Path, record: dict[str, Any]) -> None:
    path = run_dir / "results.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=False) + "\n")


def rewrite_records(run_dir: Path, records: list[dict[str, Any]]) -> None:
    path = run_dir / "results.jsonl"
    tmp = path.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, sort_keys=False) + "\n")
    os.replace(tmp, path)


def _atomic_write(path: Path, data: Any) -> None:
    with tempfile.NamedTemporaryFile(  # noqa: SIM115 — explicit delete=False + os.replace
        mode="w",
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
        encoding="utf-8",
    ) as tmp:
        json.dump(data, tmp, indent=2, sort_keys=False)
        tmp.write("\n")
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_name = tmp.name
    os.replace(tmp_name, path)
