"""Worker lifecycle: subprocess location strings, HTTP servers, unix-socket workers."""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import tempfile
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vgi_bench.models import TransportSpec


@dataclass
class WorkerHandle:
    """Result of starting a worker (or building a subprocess location)."""

    location: str  # the VGI LOCATION string to ATTACH against
    transport: str  # subprocess | http | unix | launch
    process: subprocess.Popen[bytes] | None  # None for auto-spawn subprocess transport
    log_path: Path | None  # tee log for server/unix workers
    socket_path: Path | None
    extra: dict[str, Any]


class WorkerError(RuntimeError):
    pass


def _trim_log(path: Path, *, max_bytes: int) -> None:
    """Keep startup head + shutdown tail; drop the per-request body that grows unbounded.

    HTTP fixture servers can emit megabytes of per-request structured logs which
    are not useful for benchmark provenance. The startup banner (port-line + first
    requests) and any shutdown error are.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size <= max_bytes:
        return
    half = max_bytes // 2
    with contextlib.suppress(OSError):
        with path.open("rb") as fh:
            head = fh.read(half)
            fh.seek(max(0, size - half))
            tail = fh.read()
        path.write_bytes(
            head + b"\n... [trimmed %d bytes] ...\n" % (size - len(head) - len(tail)) + tail
        )


def _substitute(template: str, mapping: dict[str, str]) -> str:
    out = template
    for k, v in mapping.items():
        out = out.replace("{" + k + "}", v)
    return out


def _render_command(command: list[str], mapping: dict[str, str]) -> list[str]:
    out: list[str] = []
    for c in command:
        c = os.path.expanduser(_substitute(c, mapping))
        # Adapters carry POSIX-style worker paths (e.g. .../vgi-example-worker-go);
        # on Windows the built binary is `<path>.exe`. Append it when the bare path
        # has no extension and the .exe exists, so one adapter works on both OSes.
        if os.name == "nt" and not os.path.splitext(c)[1] and os.path.exists(c + ".exe"):
            c += ".exe"
        out.append(c)
    return out


def _join_command(cmd: list[str]) -> str:
    # Quote any token containing whitespace so DuckDB's LOCATION parses it as one argv.
    out: list[str] = []
    for c in cmd:
        if any(ch.isspace() for ch in c):
            out.append('"' + c.replace('"', r"\"") + '"')
        else:
            out.append(c)
    return " ".join(out)


def _maybe_pooled(location: str, pooled: bool, spec: TransportSpec) -> str:
    """No-op for subprocess: pool capacity is controlled by `SET vgi_worker_pool_max=N`,
    which the SQL template emits. The ``pooled`` flag is still carried in result records
    for provenance, but it doesn't decorate the LOCATION string."""
    return location


class WorkerSession(AbstractContextManager["WorkerSession"]):
    """Context manager owning a worker process (or owning nothing for auto-spawn)."""

    def __init__(
        self,
        *,
        transport: str,
        spec: TransportSpec,
        runid: str,
        pooled: bool,
        max_workers: int,
        log_dir: Path,
    ) -> None:
        self.transport = transport
        self.spec = spec
        self.runid = runid
        self.pooled = pooled
        self.max_workers = max_workers
        self.log_dir = log_dir
        self.handle: WorkerHandle | None = None

    # ---- start ----
    def __enter__(self) -> WorkerSession:
        if self.transport == "subprocess":
            self.handle = self._start_subprocess()
        elif self.transport == "http":
            self.handle = self._start_http()
        elif self.transport == "unix":
            self.handle = self._start_unix()
        elif self.transport == "launch":
            self.handle = self._start_launch()
        else:
            raise WorkerError(f"unknown transport {self.transport!r}")
        return self

    def _start_subprocess(self) -> WorkerHandle:
        mapping = {"runid": self.runid, "max_workers": str(self.max_workers)}
        cmd = _render_command(self.spec.command, mapping)
        joined = _join_command(cmd)
        loc_template = self.spec.location_template or "{command}"
        location = _substitute(loc_template, {"command": joined, **mapping})
        location = _maybe_pooled(location, self.pooled, self.spec)
        return WorkerHandle(
            location=location,
            transport="subprocess",
            process=None,
            log_path=None,
            socket_path=None,
            extra={"command": cmd},
        )

    def _start_launch(self) -> WorkerHandle:
        # Launch transport: LOCATION is `launch:<command>` (shell-tokenised by VGI's
        # launcher). The launcher manages spawn / idle-shutdown / unix-socket plumbing.
        # The harness does NOT spawn the worker itself — DuckDB does on ATTACH.
        mapping = {"runid": self.runid, "max_workers": str(self.max_workers)}
        cmd = _render_command(self.spec.command, mapping)
        cfg = self.log_dir / f"launch-{self.runid}.json"
        import json

        cfg.write_text(json.dumps({"argv": cmd}))
        loc = _substitute(self.spec.location_template or "launch:{launch_cfg}", {"launch_cfg": str(cfg)})
        # Expand ~ so the launcher's shell-tokenisation can exec the binary.
        if loc.startswith("launch:"):
            loc = "launch:" + os.path.expanduser(loc[len("launch:"):])
        loc = _maybe_pooled(loc, self.pooled, self.spec)
        return WorkerHandle(
            location=loc,
            transport="launch",
            process=None,
            log_path=None,
            socket_path=None,
            extra={"command": cmd, "launch_cfg": str(cfg)},
        )

    def _start_http(self) -> WorkerHandle:
        mapping = {
            "runid": self.runid,
            "max_workers": str(self.max_workers),
            "host": "127.0.0.1",
            "port": "0",
        }
        cmd = _render_command(self.spec.command, mapping)
        log_path = self.log_dir / f"worker-http-{self.runid}.log"
        env = {**os.environ, **self.spec.env}
        log_fh = log_path.open("wb")
        proc = subprocess.Popen(
            cmd,
            cwd=self.spec.cwd,
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
        try:
            port = self._wait_for_log_capture(proc, log_path, self.spec.ready_log_regex or r"PORT:(\d+)")
        except Exception:
            self._terminate(proc, log_fh)
            raise
        loc = _substitute(self.spec.location_template or "http://127.0.0.1:{port}", {"port": port})
        loc = _maybe_pooled(loc, self.pooled, self.spec)
        return WorkerHandle(
            location=loc,
            transport="http",
            process=proc,
            log_path=log_path,
            socket_path=None,
            extra={"port": int(port), "log_fh": log_fh, "command": cmd},
        )

    def _start_unix(self) -> WorkerHandle:
        socket_dir = Path(tempfile.mkdtemp(prefix=f"vgi-bench-{self.runid}-"))
        socket_path = socket_dir / "worker.sock"
        mapping = {
            "runid": self.runid,
            "max_workers": str(self.max_workers),
            "tmp_socket": str(socket_path),
            "socket_path": str(socket_path),
        }
        cmd = _render_command(self.spec.command, mapping)
        log_path = self.log_dir / f"worker-unix-{self.runid}.log"
        env = {**os.environ, **self.spec.env}
        log_fh = log_path.open("wb")
        proc = subprocess.Popen(
            cmd,
            cwd=self.spec.cwd,
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
        try:
            captured = self._wait_for_log_capture(
                proc,
                log_path,
                self.spec.ready_log_regex or r"UNIX:(\S+)",
            )
        except Exception:
            self._terminate(proc, log_fh)
            raise
        bound = Path(captured)
        loc = _substitute(self.spec.location_template or "unix://{socket}", {"socket": str(bound)})
        loc = _maybe_pooled(loc, self.pooled, self.spec)
        return WorkerHandle(
            location=loc,
            transport="unix",
            process=proc,
            log_path=log_path,
            socket_path=bound,
            extra={"log_fh": log_fh, "command": cmd, "socket_dir": str(socket_dir)},
        )

    # ---- readiness ----
    def _wait_for_log_capture(self, proc: subprocess.Popen[bytes], log_path: Path, pattern: str) -> str:
        rx = re.compile(pattern)
        deadline = time.monotonic() + self.spec.ready_timeout_s
        seen = ""
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise WorkerError(f"worker exited rc={proc.returncode} before ready (log: {log_path})")
            try:
                seen = log_path.read_text(errors="replace")
            except OSError:
                seen = ""
            m = rx.search(seen)
            if m:
                return m.group(1) if m.groups() else m.group(0)
            time.sleep(0.05)
        raise WorkerError(f"timeout waiting for {pattern!r} in {log_path} (last bytes: {seen[-300:]!r})")

    # ---- teardown ----
    def __exit__(self, *exc: Any) -> None:
        if self.handle is None:
            return
        if self.handle.process is not None:
            self._terminate(self.handle.process, self.handle.extra.get("log_fh"))
        if self.handle.log_path is not None:
            _trim_log(self.handle.log_path, max_bytes=64 * 1024)
        if self.handle.socket_path is not None:
            with contextlib.suppress(OSError):
                self.handle.socket_path.unlink(missing_ok=True)
            sd = self.handle.extra.get("socket_dir")
            if sd:
                with contextlib.suppress(OSError):
                    Path(sd).rmdir()

    @staticmethod
    def _terminate(proc: subprocess.Popen[bytes], log_fh: Any) -> None:
        try:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        except ProcessLookupError:
            pass
        finally:
            if log_fh is not None:
                with contextlib.suppress(OSError):
                    log_fh.close()
