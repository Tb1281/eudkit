"""Candidate 11 ticket C11-04 (issue #31): counted forms (`every` + `after`)
promoted from DSL desugar to HIR nodes with a single opt desugar home.

Mechanism (corrected per the C11-03 finding, superseding design-doc Q6):
the DSL allocates the counter variable at node-emission time, in the
old allocation order, and the node carries it as a required
`counter_var` field; opt desugar uses that id verbatim and never mints
var ids. Byte-identical output is therefore by construction for the
rank order, and the opt desugar merges a top-level `every` increment
into a preceding unconditional trigger (the DSL's own act-run packing)
so the act-run packing is identical too.

TDD red: on the old DSL these tests fail (whens + DSL-side counter
desugar instead of nodes); on C11-01 scaffolding the byte-identity
tests fail (nodes pass through opt unchanged and fail loud in lower).
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from py import Var, after, build_module, every, once, when, while_loop

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EUDKIT_COMPILE = REPO_ROOT / "target" / "debug" / "eudkit-compile"


def run_lower(hir_json: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="eudkit-py-c1104-") as tmpdir:
        hir_path = Path(tmpdir) / "prog.hir.json"
        hir_path.write_text(hir_json, encoding="utf-8")
        out = subprocess.check_output(
            [str(EUDKIT_COMPILE), "lower", str(hir_path)],
            text=True,
        ).strip()
        return json.loads(out)


# --- Old DSL desugar shapes, written literally (not via the DSL) --------
# These are the shapes the old DSL emitted; the differential tests below
# prove the new node path compiles to exactly the same LIR. The counter
# id comes from the node's `counter_var` field: the DSL allocates it in
# the old order, so the old-shape vars list is already correct and the
# old document needs no extra declarations.


def var_deaths(cnt, comparison, amount):
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
        "relocs": [{"field_offset": 4, "target_var": cnt, "target_offset": 20, "mode": 1}],
    }


def set_var(cnt, modifier, value):
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
        "relocs": [{"field_offset": 16, "target_var": cnt, "target_offset": 20, "mode": 1}],
    }


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


def old_doc(new_doc, replace):
    module = dict(new_doc["module"])
    module["stmts"] = replace(new_doc["module"]["stmts"])
    return {"version": new_doc["version"], "module": module}


def hir_doc(mod):
    return json.loads(mod.to_json())


class C11_04EmissionTests(unittest.TestCase):
    def test_every_emits_node_cm_style(self):
        def make():
            hits = Var(0)
            with every(3):
                hits.iadd(1)

        doc = hir_doc(build_module(make))
        self.assertEqual(doc["version"], "3.2")
        # The counter is allocated by the DSL at emission time, in the
        # old order (after the user var), and carried on the node.
        self.assertEqual([v["id"] for v in doc["module"]["vars"]], [0, 1])
        kinds = [s["kind"] for s in doc["module"]["stmts"]]
        self.assertIn("every", kinds)
        node = next(s for s in doc["module"]["stmts"] if s["kind"] == "every")
        self.assertEqual(node["n"], 3)
        self.assertEqual(node["counter_var"], 1)
        self.assertEqual(len(node["body"]), 1)
        self.assertEqual(node["body"][0]["kind"], "act")

    def test_every_emits_node_callback_style(self):
        def make():
            hits = Var(0)
            every(3, lambda: hits.iadd(1))

        doc = hir_doc(build_module(make))
        kinds = [s["kind"] for s in doc["module"]["stmts"]]
        self.assertIn("every", kinds)
        node = next(s for s in doc["module"]["stmts"] if s["kind"] == "every")
        self.assertEqual(node["n"], 3)
        self.assertEqual(node["counter_var"], 1)

    def test_after_emits_node_cm_style(self):
        def make():
            fired = Var(0)
            with after(2):
                fired.iadd(1)

        doc = hir_doc(build_module(make))
        self.assertEqual([v["id"] for v in doc["module"]["vars"]], [0, 1])
        kinds = [s["kind"] for s in doc["module"]["stmts"]]
        self.assertIn("after", kinds)
        node = next(s for s in doc["module"]["stmts"] if s["kind"] == "after")
        self.assertEqual(node["n"], 2)
        self.assertEqual(node["counter_var"], 1)
        self.assertEqual(len(node["body"]), 1)
        self.assertEqual(node["body"][0]["kind"], "act")

    def test_after_emits_node_callback_style(self):
        def make():
            fired = Var(0)
            after(2, lambda: fired.iadd(1))

        doc = hir_doc(build_module(make))
        kinds = [s["kind"] for s in doc["module"]["stmts"]]
        self.assertIn("after", kinds)
        node = next(s for s in doc["module"]["stmts"] if s["kind"] == "after")
        self.assertEqual(node["counter_var"], 1)

    def test_byte_identical_clean(self):
        def make():
            hits = Var(0)
            with every(3):
                hits.iadd(1)
            with after(2):
                hits.iadd(10)

        mod = build_module(make)
        doc = hir_doc(mod)
        every_node = next(s for s in doc["module"]["stmts"] if s["kind"] == "every")
        after_node = next(s for s in doc["module"]["stmts"] if s["kind"] == "after")
        old = old_doc(
            doc,
            lambda _: [
                *old_every(every_node["n"], every_node["body"], every_node["counter_var"]),
                *old_after(after_node["n"], after_node["body"], after_node["counter_var"]),
            ],
        )
        self.assertEqual(run_lower(mod.to_json()), run_lower(json.dumps(old)))

    def test_byte_identical_top_level_act_run_before_every_after(self):
        def make():
            frame = Var(0)
            count = Var(0)
            frame.iadd(1)
            with every(3):
                count.iadd(1)
            with after(2):
                count.iadd(10)

        mod = build_module(make)
        doc = hir_doc(mod)
        every_node = next(s for s in doc["module"]["stmts"] if s["kind"] == "every")
        after_node = next(s for s in doc["module"]["stmts"] if s["kind"] == "after")
        # Old-shape input uses unpacked act+when lists; opt's bin-packing
        # reproduces the DSL trigger packing identically.
        old = old_doc(
            doc,
            lambda _: [
                {"kind": "act", **set_var(0, 8, 1)},
                *old_every(every_node["n"], every_node["body"], every_node["counter_var"]),
                *old_after(after_node["n"], after_node["body"], after_node["counter_var"]),
            ],
        )
        self.assertEqual(run_lower(mod.to_json()), run_lower(json.dumps(old)))

    def test_byte_identical_dword_var_created_after_every(self):
        def make():
            hits = Var(0)
            with every(3):
                hits.iadd(1)
            late = Var(0)
            late.iadd(5)

        mod = build_module(make)
        doc = hir_doc(mod)
        every_node = next(s for s in doc["module"]["stmts"] if s["kind"] == "every")
        old = old_doc(
            doc,
            lambda _: [
                *old_every(every_node["n"], every_node["body"], every_node["counter_var"]),
                {"kind": "act", **set_var(2, 8, 5)},
            ],
        )
        self.assertEqual(run_lower(mod.to_json()), run_lower(json.dumps(old)))

    def test_byte_identical_consecutive_everys_after_one_act_run(self):
        def make():
            frame = Var(0)
            hits = Var(0)
            frame.iadd(1)
            with every(3):
                hits.iadd(1)
            with every(2):
                hits.iadd(10)

        mod = build_module(make)
        doc = hir_doc(mod)
        nodes = [s for s in doc["module"]["stmts"] if s["kind"] == "every"]
        old = old_doc(
            doc,
            lambda _: [
                {"kind": "act", **set_var(0, 8, 1)},
                *old_every(nodes[0]["n"], nodes[0]["body"], nodes[0]["counter_var"]),
                *old_every(nodes[1]["n"], nodes[1]["body"], nodes[1]["counter_var"]),
            ],
        )
        self.assertEqual(run_lower(mod.to_json()), run_lower(json.dumps(old)))

    def test_byte_identical_every_inside_when(self):
        def make():
            frame = Var(0)
            hits = Var(0)
            frame.iadd(1)
            with when(frame == 1):
                with every(2):
                    hits.iadd(1)

        mod = build_module(make)
        doc = hir_doc(mod)
        outer = next(s for s in doc["module"]["stmts"] if s["kind"] == "when")
        every_node = next(s for s in outer["body"] if s["kind"] == "every")

        def replace(stmts):
            return [
                {**s, "body": old_every(every_node["n"], every_node["body"], every_node["counter_var"])}
                if s["kind"] == "when" else s
                for s in stmts
            ]

        old = old_doc(doc, replace)
        self.assertEqual(run_lower(mod.to_json()), run_lower(json.dumps(old)))

    def test_byte_identical_after_inside_once(self):
        def make():
            frame = Var(0)
            fired = Var(0)
            frame.iadd(1)
            with once(frame == 1):
                with after(2):
                    fired.iadd(1)

        mod = build_module(make)
        doc = hir_doc(mod)
        outer = next(s for s in doc["module"]["stmts"] if s["kind"] == "once")
        after_node = next(s for s in outer["body"] if s["kind"] == "after")

        def replace(stmts):
            return [
                {**s, "body": old_after(after_node["n"], after_node["body"], after_node["counter_var"])}
                if s["kind"] == "once" else s
                for s in stmts
            ]

        old = old_doc(doc, replace)
        self.assertEqual(run_lower(mod.to_json()), run_lower(json.dumps(old)))

    def test_byte_identical_every_inside_while(self):
        def make():
            counter = Var(3)
            hits = Var(0)
            with while_loop(counter > 0):
                counter.isub(1)
                with every(2):
                    hits.iadd(1)

        mod = build_module(make)
        doc = hir_doc(mod)
        outer = next(s for s in doc["module"]["stmts"] if s["kind"] == "while")
        every_node = next(s for s in outer["body"] if s["kind"] == "every")

        def replace(stmts):
            return [
                {**s, "body": [s["body"][0], *old_every(every_node["n"], every_node["body"], every_node["counter_var"])]}
                if s["kind"] == "while" else s
                for s in stmts
            ]

        old = old_doc(doc, replace)
        self.assertEqual(run_lower(mod.to_json()), run_lower(json.dumps(old)))

    def test_dsl_n_validation_kept(self):
        with self.assertRaisesRegex(Exception, "positive integer"):
            build_module(lambda: every(0, lambda: None))
        with self.assertRaisesRegex(Exception, "positive integer|build-time integer"):
            build_module(lambda: after(-1, lambda: None))


if __name__ == "__main__":
    unittest.main()
