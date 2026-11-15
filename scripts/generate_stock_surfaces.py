#!/usr/bin/env python3
"""Render the stock row spec's non-emitter surfaces.

`schema/stock-rows.json` is the single source for the stock and extension
helper rows. This script renders:

- the reference tables in `docs/stock-triggers.md` between explicit
  `<!-- BEGIN GENERATED: ... -->` / `<!-- END GENERATED: ... -->` markers, so
  hand-edited prose outside the markers is never touched;
- `py/stock_rows_generated.py`, the stock/extension export manifest that
  `py/__init__.py` re-exports; and
- `py/schema/stock-rows.json`, the copy of the spec that ships inside the
  `eudkit` wheel as package data. It is written as the canonical file's raw
  bytes, never re-serialized, so the packaged copy is byte-identical.

Run it after editing the spec:

    python3 scripts/generate_stock_surfaces.py

`--check` renders in memory and exits non-zero when any surface drifts
(docs tables, the export manifest, or the packaged spec copy; used by
`py/tests/test_stock_surfaces.py`), without writing anything.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = REPO_ROOT / "schema" / "stock-rows.json"
DOCS_PATH = REPO_ROOT / "docs" / "stock-triggers.md"
MANIFEST_PATH = REPO_ROOT / "py" / "stock_rows_generated.py"
PACKAGED_SPEC_PATH = REPO_ROOT / "py" / "schema" / "stock-rows.json"

HELPER_SECTIONS = ("conditions", "actions", "extensions")

NOSCRIPT = "—"


def load_spec() -> dict[str, Any]:
    return json.loads(SPEC_PATH.read_text())


def rows(spec: dict[str, Any], section: str) -> list[dict[str, Any]]:
    return spec["sections"][section]


def helper_rows(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for section in HELPER_SECTIONS for row in rows(spec, section)]


def param_names(row: dict[str, Any]) -> str:
    return ", ".join(param["name"] for param in row["params"])


def reference_table(spec: dict[str, Any], section: str) -> list[str]:
    out = [
        "| ID | Helper | Parameters (in order) | Default flags | Class |",
        "| --: | --- | --- | --: | --- |",
    ]
    for row in rows(spec, section):
        params = param_names(row) or NOSCRIPT
        out.append(
            f"| {row['chkType']} | `{row['id']}` | `{params}` | "
            f"{row['defaultFlags']} | {row['class']} |"
        )
    return out


def target_form_table(spec: dict[str, Any]) -> list[str]:
    out = [
        "| Helper | Dynamic parameters | Class |",
        "| --- | --- | --- |",
    ]
    for row in helper_rows(spec):
        targets = [param["name"] for param in row["params"] if "targetForm" in param]
        if not targets:
            continue
        out.append(
            f"| `{row['id']}` | `{', '.join(targets)}` | eudkit extension |"
        )
    return out


def string_slot_table(spec: dict[str, Any]) -> list[str]:
    out = [
        "| Helper | Parameter | HIR field | CHK slot |",
        "| --- | --- | --- | --- |",
    ]
    for row in helper_rows(spec):
        for param in row["params"]:
            if param.get("encoding") != "string":
                continue
            out.append(
                f"| `{row['id']}` | `{param['name']}` | "
                f"`{param['field']}` | `{param['chkField']}` |"
            )
    return out


EXTENSION_WIRE_FORMS = {
    "DeathsX": "condition 15 + `eudx` mask",
    "Memory": "condition 15 at `EPD(addr)`",
    "MemoryEPD": "condition 15 at an EPD",
    "MemoryX": "condition 15 + mask at `EPD(addr)`",
    "MemoryXEPD": "condition 15 + mask at an EPD",
    "SetDeathsX": "action 45 + `eudx` mask",
    "SetMemory": "action 45 with a fixed destination EPD",
    "SetMemoryEPD": "action 45 with an EPD destination",
    "SetMemoryX": "action 45 + mask with a fixed address",
    "SetMemoryXEPD": "action 45 + mask with an EPD destination",
    "SetNextPtr": "action 45 writing a trigger `next` pointer",
    "DisplayTextIndex": "action 9 with a pre-resolved `strid`",
    "SetStringDword": "action 45 into a string-table dword",
}


def extension_helper_table(spec: dict[str, Any]) -> list[str]:
    extension_rows = rows(spec, "extensions")
    ids = [row["id"] for row in extension_rows]
    stale = sorted(set(EXTENSION_WIRE_FORMS) - set(ids))
    if stale:
        raise SystemExit(
            "docs/stock-triggers.md: wire-form overlay names no extension row: "
            + ", ".join(stale)
        )
    missing = [row_id for row_id in ids if row_id not in EXTENSION_WIRE_FORMS]
    if missing:
        raise SystemExit(
            "docs/stock-triggers.md: extension rows need a wire form: "
            + ", ".join(missing)
        )
    out = [
        "| Helper | Wire form | Class |",
        "| --- | --- | --- |",
    ]
    for row in extension_rows:
        out.append(
            f"| `{row['id']}` | {EXTENSION_WIRE_FORMS[row['id']]} | {row['class']} |"
        )
    return out


DOC_REGIONS = {
    "conditions": lambda spec: reference_table(spec, "conditions"),
    "actions": lambda spec: reference_table(spec, "actions"),
    "extension-target-forms": target_form_table,
    "string-parameter-slots": string_slot_table,
    "extension-helpers": extension_helper_table,
}


def render_docs(spec: dict[str, Any], text: str) -> str:
    lines = text.splitlines()
    for name, render in DOC_REGIONS.items():
        begin = f"<!-- BEGIN GENERATED: {name} -->"
        end = f"<!-- END GENERATED: {name} -->"
        try:
            start = lines.index(begin)
            stop = lines.index(end)
        except ValueError as error:
            raise SystemExit(
                f"{DOCS_PATH.relative_to(REPO_ROOT)}: missing generated markers for {name}"
            ) from error
        if stop <= start:
            raise SystemExit(
                f"{DOCS_PATH.relative_to(REPO_ROOT)}: malformed generated markers for {name}"
            )
        lines[start + 1 : stop] = render(spec)
    rendered = "\n".join(lines)
    if text.endswith("\n"):
        rendered += "\n"
    return rendered


def manifest_names(spec: dict[str, Any]) -> list[str]:
    return [row["id"] for row in helper_rows(spec)]


def render_manifest(spec: dict[str, Any]) -> str:
    names = manifest_names(spec)
    imports = "\n".join(f"    {name}," for name in names)
    exports = "\n".join(f'    "{name}",' for name in names)
    return (
        '"""Generated from schema/stock-rows.json; do not edit by hand.\n'
        "\n"
        "Stock and eudkit-extension helper names, in spec order. Regenerate with\n"
        "`python3 scripts/generate_stock_surfaces.py`; `--check` fails on drift.\n"
        '"""\n'
        "\n"
        "from .hir import (\n"
        f"{imports}\n"
        ")\n"
        "\n"
        "STOCK_EXPORTS = (\n"
        f"{exports}\n"
        ")\n"
        "\n"
        "__all__ = list(STOCK_EXPORTS)\n"
    )


def diff(path: Path, current: str, expected: str) -> str:
    return "".join(
        difflib.unified_diff(
            current.splitlines(keepends=True),
            expected.splitlines(keepends=True),
            fromfile=str(path.relative_to(REPO_ROOT)),
            tofile=f"{path.relative_to(REPO_ROOT)} (generated)",
        )
    )


def packaged_spec_diff(current: bytes, expected: bytes) -> str:
    """Diff the packaged spec copy against the canonical bytes, naming the file."""
    if current == expected:
        return ""
    rendered = diff(
        PACKAGED_SPEC_PATH,
        current.decode("utf-8", errors="replace"),
        expected.decode("utf-8", errors="replace"),
    )
    if rendered:
        return rendered
    # Bytes differ only in ways the text decode could not show (e.g. invalid
    # UTF-8): still report drift so `--check` cannot pass on a corrupt copy.
    name = PACKAGED_SPEC_PATH.relative_to(REPO_ROOT)
    return f"--- {name}\n+++ {name} (generated)\n@@ packaged bytes differ @@\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "verify the docs tables, the Python export manifest, and the "
            "packaged spec copy are in sync without writing"
        ),
    )
    args = parser.parse_args(argv)

    spec = load_spec()
    current_docs = DOCS_PATH.read_text()
    expected_docs = render_docs(spec, current_docs)
    current_manifest = MANIFEST_PATH.read_text() if MANIFEST_PATH.exists() else ""
    expected_manifest = render_manifest(spec)
    expected_spec = SPEC_PATH.read_bytes()
    current_spec = (
        PACKAGED_SPEC_PATH.read_bytes() if PACKAGED_SPEC_PATH.exists() else b""
    )

    if args.check:
        drift = ""
        if current_docs != expected_docs:
            drift += diff(DOCS_PATH, current_docs, expected_docs)
        if current_manifest != expected_manifest:
            drift += diff(MANIFEST_PATH, current_manifest, expected_manifest)
        drift += packaged_spec_diff(current_spec, expected_spec)
        if drift:
            sys.stdout.write(drift)
            return 1
        return 0

    changed = False
    if current_docs != expected_docs:
        DOCS_PATH.write_text(expected_docs)
        print(f"updated {DOCS_PATH.relative_to(REPO_ROOT)}")
        changed = True
    if current_manifest != expected_manifest:
        MANIFEST_PATH.write_text(expected_manifest)
        print(f"updated {MANIFEST_PATH.relative_to(REPO_ROOT)}")
        changed = True
    if current_spec != expected_spec:
        PACKAGED_SPEC_PATH.parent.mkdir(parents=True, exist_ok=True)
        PACKAGED_SPEC_PATH.write_bytes(expected_spec)
        print(f"updated {PACKAGED_SPEC_PATH.relative_to(REPO_ROOT)}")
        changed = True
    if not changed:
        print("stock surfaces already in sync")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
