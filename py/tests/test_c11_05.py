"""Candidate 11 ticket C11-05 (issue #32): nesting matrix for the four
phase-1 temporal nodes (until, edge, every, after).

Mirror of ts/test_c11_05.ts: each program is built once with the new
node emission, and the old document is derived from the new
document's JSON by a recursive transform that replaces every new node
with its old DSL expansion, recursing into bodies at every depth.
The lowered LIR of both must be equal.

The same two invariants as the TS file keep the transform exact:
no bare act precedes an `every` node at the same level (except the
`after`/`every` fire-body `set` act, which is a bare act, not a
pre-packed Trigger, when the Every arm runs, so the C11-04
merge-backward rule does not engage and plain bin-packing agrees on
both sides); node bodies are copied verbatim from the new document.
The four every-outer pair cases therefore have no top-level act run
before the outer `every`.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from py import (
    Var,
    after,
    build_module,
    every,
    on_falling,
    on_rising,
    once,
    until,
    when,
    while_loop,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EUDKIT_COMPILE = REPO_ROOT / "target" / "debug" / "eudkit-compile"


def run_lower(hir_json: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="eudkit-py-c1105-") as tmpdir:
        hir_path = Path(tmpdir) / "prog.hir.json"
        hir_path.write_text(hir_json, encoding="utf-8")
        out = subprocess.check_output(
            [str(EUDKIT_COMPILE), "lower", str(hir_path)],
            text=True,
        ).strip()
        return json.loads(out)


# --- Old DSL desugar shapes, written literally (not via the DSL) --------
# Comparison codes: 0 = AtLeast, 10 = Exactly. Modifier codes:
# 7 = SetTo, 8 = Add, 9 = Subtract. These match the per-ticket builders.


def var_deaths(v, comparison, amount):
    return {
        "locid": 0,
        "player": 0,
        "amount": amount,
        "unit": 0,
        "comparison": comparison,
        "condtype": 15,
        "restype": 0,
        "flags": 0,
        "eudx": 0,
        "relocs": [{"field_offset": 4, "target_var": v, "target_offset": 20, "mode": 1}],
    }


def set_var(v, modifier, value):
    return {
        "mask": 4294967295,
        "strid": 0,
        "wavid": 0,
        "time": 0,
        "dest": 0,
        "value": value,
        "unit": 0,
        "acttype": 45,
        "modifier": modifier,
        "flags": 20,
        "padding": 0,
        "eudx": 17235,
        "relocs": [{"field_offset": 16, "target_var": v, "target_offset": 20, "mode": 1}],
    }


def old_until(latch, conds, body):
    return [
        {
            "kind": "when",
            "conds": [var_deaths(latch, 10, 1)],
            "body": [
                {
                    "kind": "when",
                    "conds": conds,
                    "body": [{"kind": "act", **set_var(latch, 7, 0)}],
                }
            ],
        },
        {
            "kind": "when",
            "conds": [var_deaths(latch, 10, 1)],
            "body": body,
        },
    ]


def old_edge(direction, conds, body, curr, prev):
    fire_curr = 1 if direction == "rising" else 0
    fire_prev = 0 if direction == "rising" else 1
    return [
        {"kind": "act", **set_var(curr, 7, 0)},
        {
            "kind": "when",
            "conds": conds,
            "body": [{"kind": "act", **set_var(curr, 7, 1)}],
        },
        {
            "kind": "when",
            "conds": [var_deaths(curr, 10, fire_curr)],
            "body": [
                {
                    "kind": "when",
                    "conds": [var_deaths(prev, 10, fire_prev)],
                    "body": body,
                }
            ],
        },
        {
            "kind": "when",
            "conds": [var_deaths(curr, 10, 1)],
            "body": [{"kind": "act", **set_var(prev, 7, 1)}],
        },
        {
            "kind": "when",
            "conds": [var_deaths(curr, 10, 0)],
            "body": [{"kind": "act", **set_var(prev, 7, 0)}],
        },
    ]


def old_every(n, body, cnt):
    return [
        {"kind": "act", **set_var(cnt, 8, 1)},
        {
            "kind": "when",
            "conds": [var_deaths(cnt, 0, n)],
            "body": [{"kind": "act", **set_var(cnt, 7, 0)}, *body],
        },
    ]


def old_after(n, body, cnt):
    return [
        {
            "kind": "when",
            "conds": [var_deaths(cnt, 10, 1)],
            "body": [{"kind": "act", **set_var(cnt, 7, 0)}, *body],
        },
        {
            "kind": "when",
            "conds": [var_deaths(cnt, 0, 2)],
            "body": [{"kind": "act", **set_var(cnt, 9, 1)}],
        },
        {
            "kind": "when",
            "conds": [var_deaths(cnt, 10, 0)],
            "body": [{"kind": "act", **set_var(cnt, 7, n)}],
        },
    ]


# Recursive transform: replace every new node with its old expansion,
# recursing into bodies at every depth (including the bodies of the
# produced expansions, which are the transformed original bodies).
def desugar_stmts(stmts):
    out = []
    for s in stmts:
        kind = s["kind"]
        if kind == "until":
            out.extend(old_until(s["latch_var"], s["conds"], desugar_stmts(s["body"])))
        elif kind == "edge":
            out.extend(
                old_edge(
                    s["direction"],
                    s["conds"],
                    desugar_stmts(s["body"]),
                    s["curr_var"],
                    s["prev_var"],
                )
            )
        elif kind == "every":
            out.extend(old_every(s["n"], desugar_stmts(s["body"]), s["counter_var"]))
        elif kind == "after":
            out.extend(old_after(s["n"], desugar_stmts(s["body"]), s["counter_var"]))
        elif kind == "when":
            new = dict(s)
            new["body"] = desugar_stmts(s["body"])
            if "else_body" in s:
                new["else_body"] = desugar_stmts(s["else_body"])
            out.append(new)
        elif kind in ("once", "while"):
            new = dict(s)
            new["body"] = desugar_stmts(s["body"])
            out.append(new)
        else:
            out.append(s)
    return out


def hir_doc(mod):
    return json.loads(mod.to_json())


def old_doc(new_doc):
    module = dict(new_doc["module"])
    module["stmts"] = desugar_stmts(new_doc["module"]["stmts"])
    return {"version": new_doc["version"], "module": module}


class TestC1105NestingMatrix(unittest.TestCase):
    def assert_byte_identical(self, mod):
        old = old_doc(hir_doc(mod))
        self.assertEqual(run_lower(mod.to_json()), run_lower(json.dumps(old)))

    # --- Nesting inside when / once / while_loop (4 nodes x 3 contexts) --

    def test_until_inside_when(self):
        def make():
            frame = Var(0)
            count = Var(0)
            frame.iadd(1)
            with when(frame == 1):
                with until(frame == 9):
                    count.iadd(1)

        self.assert_byte_identical(build_module(make))

    def test_until_inside_once(self):
        def make():
            frame = Var(0)
            count = Var(0)
            frame.iadd(1)
            with once(frame == 1):
                with until(frame == 9):
                    count.iadd(1)

        self.assert_byte_identical(build_module(make))

    def test_until_inside_while_loop(self):
        def make():
            frame = Var(0)
            count = Var(0)
            frame.iadd(1)
            with while_loop(frame == 1):
                with until(frame == 9):
                    count.iadd(1)

        self.assert_byte_identical(build_module(make))

    def test_edge_rising_inside_when(self):
        def make():
            frame = Var(0)
            count = Var(0)
            frame.iadd(1)
            with when(frame == 1):
                with on_rising(frame == 2):
                    count.iadd(1)

        self.assert_byte_identical(build_module(make))

    def test_edge_rising_inside_once(self):
        def make():
            frame = Var(0)
            count = Var(0)
            frame.iadd(1)
            with once(frame == 1):
                with on_rising(frame == 2):
                    count.iadd(1)

        self.assert_byte_identical(build_module(make))

    def test_edge_falling_inside_while_loop(self):
        def make():
            frame = Var(0)
            count = Var(0)
            frame.iadd(1)
            with while_loop(frame == 1):
                with on_falling(frame == 2):
                    count.iadd(1)

        self.assert_byte_identical(build_module(make))

    def test_every_inside_when(self):
        def make():
            frame = Var(0)
            count = Var(0)
            frame.iadd(1)
            with when(frame == 1):
                with every(3):
                    count.iadd(1)

        self.assert_byte_identical(build_module(make))

    def test_every_inside_once(self):
        def make():
            frame = Var(0)
            count = Var(0)
            frame.iadd(1)
            with once(frame == 1):
                with every(3):
                    count.iadd(1)

        self.assert_byte_identical(build_module(make))

    def test_every_inside_while_loop(self):
        def make():
            frame = Var(0)
            count = Var(0)
            frame.iadd(1)
            with while_loop(frame == 1):
                with every(3):
                    count.iadd(1)

        self.assert_byte_identical(build_module(make))

    def test_after_inside_when(self):
        def make():
            frame = Var(0)
            count = Var(0)
            frame.iadd(1)
            with when(frame == 1):
                with after(2):
                    count.iadd(1)

        self.assert_byte_identical(build_module(make))

    def test_after_inside_once(self):
        def make():
            frame = Var(0)
            count = Var(0)
            frame.iadd(1)
            with once(frame == 1):
                with after(2):
                    count.iadd(1)

        self.assert_byte_identical(build_module(make))

    def test_after_inside_while_loop(self):
        def make():
            frame = Var(0)
            count = Var(0)
            frame.iadd(1)
            with while_loop(frame == 1):
                with after(2):
                    count.iadd(1)

        self.assert_byte_identical(build_module(make))

    # --- Node inside node (full 4x4 matrix) ------------------------------
    # Outer conds: until frame == 9, edge frame == 2, every 3, after 2.
    # Inner conds differ (until 8, edge 3, every 2, after 3) so inner and
    # outer state is distinguishable in the output. The four every-outer
    # cases have no top-level act run before the outer `every` (see the
    # header note on the merge-backward rule); all other cases do.

    def test_until_inside_until(self):
        def make():
            frame = Var(0)
            count = Var(0)
            frame.iadd(1)
            with until(frame == 9):
                with until(frame == 8):
                    count.iadd(1)

        self.assert_byte_identical(build_module(make))

    def test_until_inside_edge_rising(self):
        def make():
            frame = Var(0)
            count = Var(0)
            frame.iadd(1)
            with on_rising(frame == 2):
                with until(frame == 8):
                    count.iadd(1)

        self.assert_byte_identical(build_module(make))

    def test_until_inside_every(self):
        def make():
            frame = Var(0)
            count = Var(0)
            with every(3):
                with until(frame == 8):
                    count.iadd(1)

        self.assert_byte_identical(build_module(make))

    def test_until_inside_after(self):
        def make():
            frame = Var(0)
            count = Var(0)
            frame.iadd(1)
            with after(2):
                with until(frame == 8):
                    count.iadd(1)

        self.assert_byte_identical(build_module(make))

    def test_edge_rising_inside_until(self):
        def make():
            frame = Var(0)
            count = Var(0)
            frame.iadd(1)
            with until(frame == 9):
                with on_rising(frame == 3):
                    count.iadd(1)

        self.assert_byte_identical(build_module(make))

    def test_edge_rising_inside_edge_falling(self):
        def make():
            frame = Var(0)
            count = Var(0)
            frame.iadd(1)
            with on_falling(frame == 2):
                with on_rising(frame == 3):
                    count.iadd(1)

        self.assert_byte_identical(build_module(make))

    def test_edge_rising_inside_every(self):
        def make():
            frame = Var(0)
            count = Var(0)
            with every(3):
                with on_rising(frame == 3):
                    count.iadd(1)

        self.assert_byte_identical(build_module(make))

    def test_edge_rising_inside_after(self):
        def make():
            frame = Var(0)
            count = Var(0)
            frame.iadd(1)
            with after(2):
                with on_rising(frame == 3):
                    count.iadd(1)

        self.assert_byte_identical(build_module(make))

    def test_every_inside_until(self):
        def make():
            frame = Var(0)
            count = Var(0)
            frame.iadd(1)
            with until(frame == 9):
                with every(2):
                    count.iadd(1)

        self.assert_byte_identical(build_module(make))

    def test_every_inside_edge_rising(self):
        def make():
            frame = Var(0)
            count = Var(0)
            frame.iadd(1)
            with on_rising(frame == 2):
                with every(2):
                    count.iadd(1)

        self.assert_byte_identical(build_module(make))

    def test_every_inside_every(self):
        def make():
            frame = Var(0)
            count = Var(0)
            with every(3):
                with every(2):
                    count.iadd(1)

        self.assert_byte_identical(build_module(make))

    def test_every_inside_after(self):
        def make():
            frame = Var(0)
            count = Var(0)
            frame.iadd(1)
            with after(2):
                with every(2):
                    count.iadd(1)

        self.assert_byte_identical(build_module(make))

    def test_after_inside_until(self):
        def make():
            frame = Var(0)
            count = Var(0)
            frame.iadd(1)
            with until(frame == 9):
                with after(3):
                    count.iadd(1)

        self.assert_byte_identical(build_module(make))

    def test_after_inside_edge_rising(self):
        def make():
            frame = Var(0)
            count = Var(0)
            frame.iadd(1)
            with on_rising(frame == 2):
                with after(3):
                    count.iadd(1)

        self.assert_byte_identical(build_module(make))

    def test_after_inside_every(self):
        def make():
            frame = Var(0)
            count = Var(0)
            with every(3):
                with after(2):
                    count.iadd(1)

        self.assert_byte_identical(build_module(make))

    def test_after_inside_after(self):
        def make():
            frame = Var(0)
            count = Var(0)
            frame.iadd(1)
            with after(2):
                with after(3):
                    count.iadd(1)

        self.assert_byte_identical(build_module(make))

    # --- 3-deep nesting + recursion proof --------------------------------

    def test_every_inside_when_inside_until_3_deep(self):
        def make():
            frame = Var(0)
            count = Var(0)
            frame.iadd(1)
            with until(frame == 9):
                with when(frame == 1):
                    with every(2):
                        count.iadd(1)

        self.assert_byte_identical(build_module(make))

    def test_recursion_proof_nested_whens_inside_until_body_flatten(self):
        # If desugar did not recurse, the Until arm would push the raw
        # expansion without optimizing it, so the nested whens below
        # would survive unflattened and lower to a different trigger
        # structure than the old program (whose plain whens opt
        # flattens). Byte equality therefore proves the desugar
        # recursed into the body.
        def make():
            frame = Var(0)
            count = Var(0)
            frame.iadd(1)
            with until(frame == 9):
                with when(frame == 1):
                    with when(frame == 2):
                        count.iadd(1)

        self.assert_byte_identical(build_module(make))


if __name__ == "__main__":
    unittest.main()
