"""Shared test harness helpers: simulate HIR through the native binary and
decode the synthetic STR buffer through EPD reads.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EUDKIT_COMPILE = REPO_ROOT / "target" / "debug" / "eudkit-compile"
EUDKIT = REPO_ROOT / "target" / "debug" / "eudkit"

EPD_BASE = 0x58A364
SYNTHETIC_STRING_BASE = 0x20000000
DWORD_BYTES = 4


def run_sim(
    hir_json: str, loops: int = 1, prefix: str = "eudkit-py-sim-"
) -> dict[str, Any]:
    """Run the native ``sim`` command against HIR JSON and return its report."""
    with tempfile.TemporaryDirectory(prefix=prefix) as tmpdir:
        hir_path = Path(tmpdir) / "prog.hir.json"
        hir_path.write_text(hir_json, encoding="utf-8")
        out = subprocess.check_output(
            [str(EUDKIT_COMPILE), "sim", str(hir_path), str(loops)],
            text=True,
        ).strip()
        return json.loads(out)


def run_static(hir_json: str, prefix: str = "eudkit-py-static-") -> dict[str, Any]:
    """Static cost only, with no simulation: ``dump-ir --json --no-exec``."""
    with tempfile.TemporaryDirectory(prefix=prefix) as tmpdir:
        hir_path = Path(tmpdir) / "prog.hir.json"
        hir_path.write_text(hir_json, encoding="utf-8")
        out = subprocess.check_output(
            [str(EUDKIT), "dump-ir", str(hir_path), "--json", "--no-exec"],
            text=True,
        ).strip()
    return json.loads(out)


def string_epd(index: int) -> int:
    """EPD index of synthetic string ``index``."""
    return (SYNTHETIC_STRING_BASE + index * 0x10000 - EPD_BASE) // 4


def read_dword(report: dict[str, Any], epd: int) -> int:
    return report["external_epds"].get(str(epd), 0)


def read_string_bytes(
    report: dict[str, Any], index: int, limit: int = 256
) -> list[int]:
    """Raw bytes of synthetic string ``index``, stopping at the first NUL."""
    start = string_epd(index)
    out: list[int] = []
    for i in range(limit):
        dword = read_dword(report, start + (i >> 2))
        byte = (dword >> (8 * (i & 3))) & 0xFF
        if byte == 0:
            break
        out.append(byte)
    return out


def read_text(report: dict[str, Any], index: int) -> str:
    """Render a synthetic string, dropping the documented 0x1A alignment pads."""
    return bytes(
        byte for byte in read_string_bytes(report, index) if byte != 0x1A
    ).decode("ascii")


def raw_string_bytes(report: dict[str, Any], count: int) -> list[int]:
    """Raw byte lanes from synthetic string 0, including NUL/past-NUL bytes.

    Unlike :func:`read_string_bytes` this has no string index and does not stop
    at the terminator; use it when the test asserts the whole lane window.
    """
    epd_base = (SYNTHETIC_STRING_BASE - EPD_BASE) // 4
    return [
        (read_dword(report, epd_base + (i >> 2)) >> (8 * (i & 3))) & 0xFF
        for i in range(count)
    ]


def read_dwords(report: dict[str, Any], count: int) -> list[int]:
    """Whole dwords of synthetic string 0, for multi-dword text assertions."""
    epd_base = (SYNTHETIC_STRING_BASE - EPD_BASE) // 4
    return [read_dword(report, epd_base + dword) for dword in range(count)]


def string_dword_bytes(report: dict[str, Any], count: int) -> bytes:
    """Little-endian bytes of the first ``count`` dwords of synthetic string 0."""
    return b"".join(value.to_bytes(DWORD_BYTES, "little") for value in read_dwords(report, count))


def flatten_stmts(stmts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Recursively flatten statements, including temporal bodies and else arms."""
    out: list[dict[str, Any]] = []
    for stmt in stmts:
        out.append(stmt)
        out.extend(flatten_stmts(stmt.get("body") or []))
        out.extend(flatten_stmts(stmt.get("else_body") or []))
    return out


def flatten_acts(stmts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Actions only.

    Unlike :func:`flatten_stmts` this does not recurse into else arms and only
    descends through ``body``, preserving the original action-order contract of
    the ticket #9 assertions.
    """
    out: list[dict[str, Any]] = []
    for stmt in stmts:
        out.extend(stmt.get("acts", []))
        out.extend(flatten_acts(stmt.get("body", [])))
    return out


def touched_dword_offsets(report: dict[str, Any], index: int) -> list[int]:
    """Dword offsets inside the synthetic STR buffer that were written."""
    base = string_epd(index)
    return sorted(
        (int(epd) - base) * 4
        for epd in report["external_epds"]
        if base <= int(epd) < base + 0x4000
    )
