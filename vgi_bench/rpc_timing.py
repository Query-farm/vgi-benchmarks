"""Parse the ``[vgi-client] timeline:`` line emitted on stderr when
``VGI_RPC_CLIENT_TIMING`` is set."""

from __future__ import annotations

import re
from dataclasses import dataclass

_LINE_RX = re.compile(r"\[vgi-client\][^\n]*timeline:[^\n]*")
_FIELD_RX = re.compile(r"(batches|total|convert_in|write|read[^=]*|schema|convert_out)\s*=\s*([0-9.]+)")


@dataclass
class RpcBreakdown:
    available: bool
    batches: int | None = None
    total_ms: float | None = None
    convert_in_ms: float | None = None
    write_ms: float | None = None
    read_ms: float | None = None
    schema_ms: float | None = None
    convert_out_ms: float | None = None
    raw_line: str = ""


def parse_rpc_timing(stderr: str) -> RpcBreakdown:
    m = _LINE_RX.search(stderr or "")
    if not m:
        return RpcBreakdown(available=False)
    line = m.group(0)
    fields: dict[str, float] = {}
    for fm in _FIELD_RX.finditer(line):
        key = fm.group(1)
        # collapse "read(worker+transport)" to "read"
        if key.startswith("read"):
            key = "read"
        try:
            fields[key] = float(fm.group(2))
        except ValueError:
            continue
    out = RpcBreakdown(available=True, raw_line=line)
    if "batches" in fields:
        out.batches = int(fields["batches"])
    out.total_ms = fields.get("total")
    out.convert_in_ms = fields.get("convert_in")
    out.write_ms = fields.get("write")
    out.read_ms = fields.get("read")
    out.schema_ms = fields.get("schema")
    out.convert_out_ms = fields.get("convert_out")
    return out
