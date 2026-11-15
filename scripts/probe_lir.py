"""Probe: dump LIR trigger shapes for a HIR program."""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPILE = REPO_ROOT / "target" / "debug" / "eudkit-compile"


def lir(hir_json: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "prog.hir.json"
        p.write_text(hir_json)
        out = subprocess.check_output([str(COMPILE), "lower", str(p)], text=True)
    return json.loads(out)


def show(name: str, hir_json: str) -> None:
    doc = lir(hir_json)
    items = doc["module"]["items"]
    print(f"--- {name}: {len(items)} items ---")
    pages = 0
    for i, item in enumerate(items):
        if item.get("kind") != "trigger":
            print(f"{i:3} {item.get('kind')} size={item.get('size')}")
            continue
        prev = item.get("prev")
        nxt = item.get("next")
        print(
            f"{i:3} T conds={len(item['conds'])} acts={len(item['acts'])} "
            f"pres={item['preserved']} flag={item.get('flag')} prev={prev} next={nxt}"
        )
