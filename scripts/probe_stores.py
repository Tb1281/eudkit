"""Probe: measure trigger counts for VarStore runs (issue #96)."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from py import (
    AddrReloc,
    AtLeast,
    Deaths,
    SetMemory,
    SetTo,
    Var,
    build_module,
    emitVarStore,
    when,
)

EUDKIT = REPO_ROOT / "target" / "debug" / "eudkit"


def run(hir_json: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "prog.hir.json"
        p.write_text(hir_json)
        out = subprocess.check_output(
            [str(EUDKIT), "dump-ir", str(p), "--json", "--no-exec"], text=True
        )
    return json.loads(out)


def stores_program(*, in_when: bool) -> str:
    def prog() -> None:
        a = Var(0)
        b = Var(0)
        c = Var(0)
        d = Var(0)
        a.set(1)
        b.set(2)
        c.set(3)
        d.set(4)

        def writes() -> None:
            emitVarStore(a.id, AddrReloc(0x640B58), SetTo)
            emitVarStore(b.id, AddrReloc(0x640B5C), SetTo)
            emitVarStore(c.id, AddrReloc(0x640B60), SetTo)
            emitVarStore(d.id, AddrReloc(0x640B64), SetTo)

        if in_when:
            when([Deaths(0, AtLeast, 1)], writes)
        else:
            writes()

    return build_module(prog).to_json()


def transfers_program(*, in_when: bool) -> str:
    def prog() -> None:
        x = Var(1)
        y = Var(1)
        w = Var(1)
        z = Var(1)

        def both() -> None:
            x.set(y)
            x.iadd(1)
            z.set(w)
            z.iadd(1)

        if in_when:
            when([Deaths(0, AtLeast, 1)], both)
        else:
            both()

    return build_module(prog).to_json()


def summarize(name: str, hir_json: str, show_hir: bool = False) -> dict:
    report = run(hir_json)
    print(f"--- {name} ---")
    print("static:", json.dumps(report.get("static_cost"), sort_keys=True))
    if show_hir:
        stmts = json.loads(hir_json)["module"]["stmts"]
        print("HIR:", json.dumps(stmts)[:3000])
    return report


if __name__ == "__main__":
    show = "--hir" in __import__("sys").argv
    summarize("4 stores at root", stores_program(in_when=False), show)
    summarize("4 stores inside when", stores_program(in_when=True), show)
    summarize("x=y+1; z=w+1 in when", transfers_program(in_when=True), show)
    summarize("x=y+1; z=w+1 at root", transfers_program(in_when=False), show)
