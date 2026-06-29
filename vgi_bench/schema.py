"""Embedded JSON schemas and a stdlib-only structural validator.

The validator is deliberately small — it enforces required keys, types, enums,
nested object structure, and per-item recursion. It is not a full Draft 2020-12
implementation; for that, install the optional ``jsonschema`` extra and call
``validate_with_jsonschema`` below.
"""

from __future__ import annotations

from typing import Any

CASE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "schema_version",
        "id",
        "function_type",
        "vgi_function",
        "query_sql",
        "params",
        "threads",
        "iterations",
        "applies_to",
    ],
    "properties": {
        "schema_version": {"type": "integer"},
        "id": {"type": "string"},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "function_type": {"enum": ["scalar", "table", "table_in_out", "aggregate"]},
        "vgi_function": {"type": "string"},
        "requires_attach": {"type": "boolean"},
        "call_qualified": {"type": "boolean"},
        "alias": {"type": "string"},
        "attach_name": {"type": "string"},
        "setup_sql": {"type": "array", "items": {"type": "string"}},
        "query_sql": {"type": "string"},
        "teardown_sql": {"type": "array", "items": {"type": "string"}},
        "params": {"type": "object"},
        "param_defaults": {"type": "object"},
        "threads": {"type": "array", "items": {"type": "integer"}},
        "parallelizable": {"type": "boolean"},
        "iterations": {
            "type": "object",
            "required": ["warmup", "measured"],
            "properties": {
                "warmup": {"type": "integer"},
                "measured": {"type": "integer"},
            },
        },
        "applies_to": {
            "type": "object",
            "properties": {
                "transports": {"oneOf": [{"const": "all"}, {"type": "array"}]},
                "languages": {"oneOf": [{"const": "all"}, {"type": "array"}]},
            },
        },
        "metric_tags": {"type": "array", "items": {"type": "string"}},
        "externalization": {"oneOf": [{"type": "null"}, {"type": "object"}]},
        "notes": {"type": "string"},
        "payload": {
            "type": "object",
            "properties": {
                "input_bytes_per_row": {"type": "string"},
                "output_bytes_per_row": {"type": "string"},
                "schema_in": {"type": "string"},
                "schema_out": {"type": "string"},
                "notes": {"type": "string"},
            },
        },
    },
}

ADAPTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["schema_version", "language", "runnable", "transports"],
    "properties": {
        "schema_version": {"type": "integer"},
        "language": {"type": "string"},
        "display_name": {"type": "string"},
        "runnable": {"type": "boolean"},
        "repo_path": {"type": "string"},
        "build": {"oneOf": [{"type": "null"}, {"type": "object"}]},
        "supported_function_types": {"type": "array", "items": {"type": "string"}},
        "version_command": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
        "transports": {
            "type": "object",
            "properties": {
                "subprocess": {"type": "object"},
                "http": {"type": "object"},
                "unix": {"type": "object"},
                "launch": {"type": "object"},
            },
        },
    },
}

RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "schema_version",
        "run_id",
        "case_id",
        "function_type",
        "transport",
        "language",
        "threads",
        "status",
    ],
    "properties": {
        "schema_version": {"type": "integer"},
        "run_id": {"type": "string"},
        "case_id": {"type": "string"},
        "function_type": {"type": "string"},
        "vgi_function": {"type": "string"},
        "transport": {"type": "string"},
        "language": {"type": "string"},
        "threads": {"type": "integer"},
        "status": {"enum": ["ok", "error", "skipped"]},
    },
}


class SchemaError(ValueError):
    pass


def _path(parts: list[str]) -> str:
    return "/" + "/".join(parts) if parts else "/"


def _validate(node: Any, schema: dict[str, Any], path: list[str]) -> None:
    if "oneOf" in schema:
        errors = []
        for sub in schema["oneOf"]:
            try:
                _validate(node, sub, path)
                return
            except SchemaError as e:
                errors.append(str(e))
        raise SchemaError(f"{_path(path)}: matched none of oneOf — {errors}")
    if "const" in schema:
        if node != schema["const"]:
            raise SchemaError(f"{_path(path)}: expected const {schema['const']!r}, got {node!r}")
        return
    if "enum" in schema:
        if node not in schema["enum"]:
            raise SchemaError(f"{_path(path)}: expected one of {schema['enum']!r}, got {node!r}")
        return
    t = schema.get("type")
    if t == "object":
        if not isinstance(node, dict):
            raise SchemaError(f"{_path(path)}: expected object, got {type(node).__name__}")
        for required_key in schema.get("required", []):
            if required_key not in node:
                raise SchemaError(f"{_path(path)}: missing required key {required_key!r}")
        props = schema.get("properties", {})
        for k, v in node.items():
            if k in props:
                _validate(v, props[k], [*path, k])
        return
    if t == "array":
        if not isinstance(node, list):
            raise SchemaError(f"{_path(path)}: expected array, got {type(node).__name__}")
        items_schema = schema.get("items")
        if items_schema is not None:
            for i, item in enumerate(node):
                _validate(item, items_schema, [*path, str(i)])
        return
    if t == "string":
        if not isinstance(node, str):
            raise SchemaError(f"{_path(path)}: expected string, got {type(node).__name__}")
        return
    if t == "integer":
        if isinstance(node, bool) or not isinstance(node, int):
            raise SchemaError(f"{_path(path)}: expected integer, got {type(node).__name__}")
        return
    if t == "boolean":
        if not isinstance(node, bool):
            raise SchemaError(f"{_path(path)}: expected boolean, got {type(node).__name__}")
        return
    if t == "null":
        if node is not None:
            raise SchemaError(f"{_path(path)}: expected null, got {type(node).__name__}")
        return


def validate_case(doc: dict[str, Any]) -> None:
    _validate(doc, CASE_SCHEMA, [])


def validate_adapter(doc: dict[str, Any]) -> None:
    _validate(doc, ADAPTER_SCHEMA, [])


def validate_result(doc: dict[str, Any]) -> None:
    _validate(doc, RESULT_SCHEMA, [])
