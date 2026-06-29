"""Collect the environment fingerprint stamped onto every run."""

from __future__ import annotations

import datetime as _dt
import os
import platform
import socket
import subprocess
from pathlib import Path

from vgi_bench import HARNESS_VERSION
from vgi_bench.models import EnvFingerprint

DEFAULT_DUCKDB = os.path.expanduser("~/Development/vgi/build/release/duckdb")
DEFAULT_VGI_REPO = os.path.expanduser("~/Development/vgi")
DEFAULT_VGI_PYTHON_REPO = os.path.expanduser("~/Development/vgi-python")


def _git_sha(repo: str) -> tuple[str, bool]:
    if not Path(repo, ".git").exists():
        return ("unknown", False)
    try:
        sha = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return ("unknown", False)
    try:
        dirty = bool(
            subprocess.run(
                ["git", "-C", repo, "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            ).stdout.strip()
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        dirty = False
    return (sha, dirty)


def _duckdb_version(binary: str) -> str:
    try:
        out = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        return out.splitlines()[0] if out else "unknown"
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return "unknown"


def _cpu_brand() -> str:
    try:
        if platform.system() == "Darwin":
            out = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            ).stdout.strip()
            return out or platform.processor() or "unknown"
        return platform.processor() or "unknown"
    except Exception:
        return "unknown"


def _mem_total_mb() -> int | None:
    try:
        if platform.system() == "Darwin":
            out = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            ).stdout.strip()
            return int(out) // (1024 * 1024)
        if hasattr(os, "sysconf"):
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            return (pages * page_size) // (1024 * 1024)
    except Exception:
        return None
    return None


def build_fingerprint(
    *,
    duckdb_binary: str = DEFAULT_DUCKDB,
    vgi_repo: str = DEFAULT_VGI_REPO,
    vgi_python_repo: str = DEFAULT_VGI_PYTHON_REPO,
    note: str = "",
) -> EnvFingerprint:
    vgi_sha, vgi_dirty = _git_sha(vgi_repo)
    vp_sha, vp_dirty = _git_sha(vgi_python_repo)
    now = _dt.datetime.now(_dt.UTC)
    short_date = now.strftime("%Y-%m-%dT%H%M")
    dirty_suffix = "-dirty" if (vgi_dirty or vp_dirty) else ""
    run_id = f"{short_date}-{vgi_sha}{dirty_suffix}"
    try:
        loadavg = list(os.getloadavg())
    except OSError:
        loadavg = [0.0, 0.0, 0.0]
    return EnvFingerprint(
        run_id=run_id,
        timestamp_utc=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        harness_version=HARNESS_VERSION,
        vgi_git_sha=vgi_sha,
        vgi_git_dirty=vgi_dirty,
        vgi_python_git_sha=vp_sha,
        vgi_python_git_dirty=vp_dirty,
        duckdb_version=_duckdb_version(duckdb_binary),
        duckdb_binary_path=duckdb_binary,
        os=f"{platform.system()} {platform.release()}",
        arch=platform.machine(),
        cpu=_cpu_brand(),
        cpu_count=os.cpu_count() or 1,
        mem_total_mb=_mem_total_mb(),
        hostname=socket.gethostname(),
        loadavg=loadavg,
        note=note,
    )
