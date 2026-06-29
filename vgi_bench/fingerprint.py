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


def _haybarn_duckdb_version() -> str:
    """Engine version reported by the released haybarn the harness actually runs.

    The harness executes in-process via haybarn (db_session.py) and loads VGI with
    ``INSTALL vgi FROM community; LOAD vgi;`` — so the engine of record is haybarn's
    DuckDB, not any locally built binary. Report both the DuckDB version() and the
    haybarn package version for full provenance.
    """
    try:
        import haybarn

        con = haybarn.connect()
        try:
            row = con.execute("SELECT version()").fetchone()
            ddb = row[0] if row else None
        finally:
            con.close()
        hv = getattr(haybarn, "__version__", None)
        if ddb and hv:
            return f"{ddb} (haybarn {hv})"
        return ddb or (f"haybarn {hv}" if hv else "unknown")
    except Exception:
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
        if platform.system() == "Windows":
            try:
                import winreg

                with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
                ) as k:
                    name, _ = winreg.QueryValueEx(k, "ProcessorNameString")
                    if name:
                        return str(name).strip()
            except Exception:
                pass
            return os.environ.get("PROCESSOR_IDENTIFIER") or platform.processor() or "unknown"
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
        if platform.system() == "Windows":
            import ctypes

            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = _MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return int(stat.ullTotalPhys) // (1024 * 1024)
            return None
        if hasattr(os, "sysconf"):
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            return (pages * page_size) // (1024 * 1024)
    except Exception:
        return None
    return None


def build_fingerprint(
    *,
    duckdb_binary: str | None = None,
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
        loadavg = list(os.getloadavg())  # POSIX only
    except (OSError, AttributeError):
        loadavg = [0.0, 0.0, 0.0]
    # Engine of record is the released haybarn the harness runs in-process; only
    # honour an explicit --duckdb binary if one was passed (back-compat).
    if duckdb_binary and Path(duckdb_binary).exists():
        duckdb_version = _duckdb_version(duckdb_binary)
        duckdb_path = duckdb_binary
    else:
        duckdb_version = _haybarn_duckdb_version()
        duckdb_path = "haybarn (in-process, INSTALL vgi FROM community)"
    return EnvFingerprint(
        run_id=run_id,
        timestamp_utc=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        harness_version=HARNESS_VERSION,
        vgi_git_sha=vgi_sha,
        vgi_git_dirty=vgi_dirty,
        vgi_python_git_sha=vp_sha,
        vgi_python_git_dirty=vp_dirty,
        duckdb_version=duckdb_version,
        duckdb_binary_path=duckdb_path,
        os=f"{platform.system()} {platform.release()}",
        arch=platform.machine(),
        cpu=_cpu_brand(),
        cpu_count=os.cpu_count() or 1,
        mem_total_mb=_mem_total_mb(),
        hostname=socket.gethostname(),
        loadavg=loadavg,
        note=note,
    )
