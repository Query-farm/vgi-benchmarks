"""Frozen dataclass models used by the harness."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Iterations:
    warmup: int
    measured: int


@dataclass(frozen=True)
class AppliesTo:
    transports: list[str] | str  # "all" or list
    languages: list[str] | str


@dataclass(frozen=True)
class Case:
    id: str
    schema_version: int
    function_type: str  # scalar | table | table_in_out | aggregate
    vgi_function: str
    title: str
    description: str
    requires_attach: bool
    call_qualified: bool
    alias: str
    attach_name: str
    setup_sql: list[str]
    query_sql: str
    teardown_sql: list[str]
    params: dict[str, list[Any]]
    param_defaults: dict[str, Any]
    threads: list[int]
    parallelizable: bool
    payload: dict[str, Any]  # input_bytes_per_row, output_bytes_per_row, schema_in, schema_out, notes
    iterations: Iterations
    applies_to: AppliesTo
    metric_tags: list[str]
    externalization: dict[str, Any] | None
    notes: str


@dataclass(frozen=True)
class TransportSpec:
    kind: str  # command_location | server | socket | launch
    command: list[str]
    cwd: str | None
    env: dict[str, str]
    location_template: str
    ready_method: str  # none | log_regex | socket_exists | tcp_connect
    ready_log_regex: str | None
    ready_log_stream: str  # stdout | stderr | merged
    ready_timeout_s: float
    pooling_supported: bool
    pooling_query_param: str | None
    teardown: str  # none | signal_term
    notes: str
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Adapter:
    language: str
    display_name: str
    runnable: bool
    repo_path: str | None
    build: dict[str, Any] | None
    supported_function_types: list[str]
    version_command: list[str]
    transports: dict[str, TransportSpec]
    notes: str


@dataclass(frozen=True)
class Cell:
    case: Case
    params: dict[str, Any]
    transport: str  # "subprocess" | "launch" | "subprocess-shm" | "launch-shm" | "http" | "unix"
    language: str
    threads: int


@dataclass
class EnvFingerprint:
    run_id: str
    timestamp_utc: str
    harness_version: str
    vgi_git_sha: str
    vgi_git_dirty: bool
    vgi_python_git_sha: str
    vgi_python_git_dirty: bool
    duckdb_version: str
    duckdb_binary_path: str
    os: str
    arch: str
    cpu: str
    cpu_count: int
    mem_total_mb: int | None
    hostname: str
    loadavg: list[float]
    note: str
