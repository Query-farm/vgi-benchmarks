"""Load + validate ``cases/*.json`` and ``languages/*.json`` files."""

from __future__ import annotations

import itertools
import json
import os
from pathlib import Path
from typing import Any

from vgi_bench.models import Adapter, AppliesTo, Case, Iterations, TransportSpec
from vgi_bench.schema import validate_adapter, validate_case


def _expand_user(s: str | None) -> str | None:
    return os.path.expanduser(s) if s else s


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def load_case(path: Path) -> Case:
    doc = _read_json(path)
    validate_case(doc)
    if doc["id"] != path.stem:
        raise ValueError(f"{path}: case id {doc['id']!r} does not match filename stem {path.stem!r}")
    it = doc["iterations"]
    return Case(
        id=doc["id"],
        schema_version=doc["schema_version"],
        function_type=doc["function_type"],
        vgi_function=doc["vgi_function"],
        title=doc.get("title", ""),
        description=doc.get("description", ""),
        requires_attach=doc.get("requires_attach", True),
        call_qualified=doc.get("call_qualified", True),
        alias=doc.get("alias", "example"),
        attach_name=doc.get("attach_name", "example"),
        setup_sql=doc.get("setup_sql", []),
        query_sql=doc["query_sql"],
        teardown_sql=doc.get("teardown_sql", []),
        params=doc.get("params", {}),
        param_defaults=doc.get("param_defaults", {}),
        threads=doc["threads"],
        parallelizable=doc.get("parallelizable", True),
        payload=doc.get("payload", {}),
        iterations=Iterations(warmup=it["warmup"], measured=it["measured"]),
        applies_to=AppliesTo(
            transports=doc["applies_to"].get("transports", "all"),
            languages=doc["applies_to"].get("languages", "all"),
        ),
        metric_tags=doc.get("metric_tags", []),
        externalization=doc.get("externalization"),
        notes=doc.get("notes", ""),
    )


def load_cases(case_dir: Path) -> list[Case]:
    paths = sorted(p for p in case_dir.glob("*.json") if not p.name.startswith("_"))
    return [load_case(p) for p in paths]


def _load_transport(name: str, raw: dict[str, Any]) -> TransportSpec:
    ready = raw.get("ready", {"method": "none"})
    pooling = raw.get("pooling") or {}
    return TransportSpec(
        kind=raw.get("kind", "command_location"),
        command=list(raw.get("command", [])),
        cwd=_expand_user(raw.get("cwd")),
        env=dict(raw.get("env", {})),
        location_template=raw.get("location_template", "{command}"),
        ready_method=ready.get("method", "none"),
        ready_log_regex=ready.get("log_regex"),
        ready_log_stream=ready.get("stream", "stdout"),
        ready_timeout_s=float(ready.get("timeout_s", 30)),
        pooling_supported=bool(pooling.get("supported", False)),
        pooling_query_param=pooling.get("query_param"),
        teardown=raw.get("teardown", "signal_term"),
        notes=raw.get("notes", ""),
        extra={
            k: v
            for k, v in raw.items()
            if k
            not in {
                "kind",
                "command",
                "cwd",
                "env",
                "location_template",
                "ready",
                "pooling",
                "teardown",
                "notes",
            }
        },
    )


def load_adapter(path: Path) -> Adapter:
    doc = _read_json(path)
    validate_adapter(doc)
    if doc["language"] != path.stem:
        raise ValueError(f"{path}: language {doc['language']!r} does not match filename stem {path.stem!r}")
    transports = {name: _load_transport(name, raw) for name, raw in doc.get("transports", {}).items()}
    return Adapter(
        language=doc["language"],
        display_name=doc.get("display_name", doc["language"]),
        runnable=bool(doc["runnable"]),
        repo_path=_expand_user(doc.get("repo_path")),
        build=doc.get("build"),
        supported_function_types=list(doc.get("supported_function_types", [])),
        version_command=list(doc.get("version_command", [])),
        transports=transports,
        notes=doc.get("notes", ""),
    )


def load_adapters(adapter_dir: Path) -> list[Adapter]:
    paths = sorted(p for p in adapter_dir.glob("*.json") if not p.name.startswith("_"))
    return [load_adapter(p) for p in paths]


def param_points(case: Case, override_threads: list[int] | None = None) -> list[dict[str, Any]]:
    """Cartesian-expand ``case.params`` into a list of param dicts (without threads)."""
    sweeps = case.params
    if not sweeps:
        return [dict(case.param_defaults)]
    names = list(sweeps.keys())
    values = [sweeps[n] for n in names]
    out: list[dict[str, Any]] = []
    for combo in itertools.product(*values):
        base = dict(case.param_defaults)
        base.update(dict(zip(names, combo, strict=True)))
        out.append(base)
    return out


def case_threads(case: Case, override_threads: list[int] | None) -> list[int]:
    if override_threads:
        return list(override_threads)
    return list(case.threads)


def applies(value: list[str] | str, candidate: str) -> bool:
    if value == "all":
        return True
    return candidate in value
