"""C11-03: edge node emission + opt desugar byte-identity tests (Python mirror).

Mirrors ts/test_edge_desugar.ts: each program is built twice, once with
the on_rising / on_falling node emission and once with the old desugar
written out by hand (Var + when calls, same allocation order, same
statement order). The lowered LIR output of both must be byte-identical,
and the sim traces over a toggling input must match. LIR JSON carries no
HIR trigger ids (lowering uses positional indices), so LIR equality is
compiled-output equality.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from py import (
    Var,
    build_module,
    on_falling,
    on_rising,
    when,
)

from py.tests.helpers import run_sim
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EUDKIT_COMPILE = REPO_ROOT / "target" / "debug" / "eudkit-compile"


def run_tool(cmd, hir_json):
    # eudkit-compile takes the hir path right after the subcommand:
    # `lower <hir.json>`, `sim <hir.json> [loops]`.
    with tempfile.TemporaryDirectory(prefix="eudkit-py-edge-") as tmpdir:
        hir_path = Path(tmpdir) / "prog.hir.json"
        hir_path.write_text(hir_json, encoding="utf-8")
        out = subprocess.check_output(
            [str(EUDKIT_COMPILE), cmd[0], str(hir_path)] + cmd[1:],
            text=True,
        ).strip()
        return out


def run_lower(hir_json):
    return run_tool(["lower"], hir_json)
def build_node_program():
    def make():
        frame = Var(0)
        input_v = Var(0)
        rise_count = Var(0)
        fall_count = Var(0)

        frame.iadd(1)
        when(frame == 1, lambda: input_v.set(1))
        when(frame == 2, lambda: input_v.set(0))
        when(frame == 3, lambda: input_v.set(1))

        with on_rising(input_v == 1):
            rise_count.iadd(1)
        with on_falling(input_v == 1):
            fall_count.iadd(1)

        after = Var(0)
        after.set(7)

    return build_module(make)


def build_baseline_program():
    def make():
        frame = Var(0)
        input_v = Var(0)
        rise_count = Var(0)
        fall_count = Var(0)

        frame.iadd(1)
        when(frame == 1, lambda: input_v.set(1))
        when(frame == 2, lambda: input_v.set(0))
        when(frame == 3, lambda: input_v.set(1))

        # Rising desugar, written out by hand (today's DSL shape).
        curr_r = Var(0)
        prev_r = Var(0)
        curr_r.set(0)
        when(input_v == 1, lambda: curr_r.set(1))
        when(curr_r == 1, lambda: when(prev_r == 0, lambda: rise_count.iadd(1)))
        when(curr_r == 1, lambda: prev_r.set(1))
        when(curr_r == 0, lambda: prev_r.set(0))

        # Falling desugar: same gates, fire polarities mirrored.
        curr_f = Var(0)
        prev_f = Var(0)
        curr_f.set(0)
        when(input_v == 1, lambda: curr_f.set(1))
        when(curr_f == 0, lambda: when(prev_f == 1, lambda: fall_count.iadd(1)))
        when(curr_f == 1, lambda: prev_f.set(1))
        when(curr_f == 0, lambda: prev_f.set(0))

        after = Var(0)
        after.set(7)

    return build_module(make)


class EdgeDesugarTests(unittest.TestCase):
    def test_edge_nodes_are_emitted_not_desugared(self):
        doc = json.loads(build_node_program().to_json())
        edges = [s for s in doc["module"]["stmts"] if s["kind"] == "edge"]
        self.assertEqual(len(edges), 2)
        self.assertEqual(
            [e["direction"] for e in edges], ["rising", "falling"]
        )

    def test_edge_node_emission_lowers_byte_identically(self):
        node_json = build_node_program().to_json()
        baseline_json = build_baseline_program().to_json()
        self.assertEqual(run_lower(node_json), run_lower(baseline_json))

    def test_edge_node_emission_simulates_identically(self):
        node_json = build_node_program().to_json()
        baseline_json = build_baseline_program().to_json()
        # Loops: 1: input 0->1 (rising fires); 2: 1->0 (falling fires);
        # 3: 0->1 (rising fires); 4: input stays 1 (no edge).
        for loops in [1, 2, 3, 4]:
            self.assertEqual(
                run_sim(node_json, loops)["var_values"],
                run_sim(baseline_json, loops)["var_values"],
                f"sim traces diverge at {loops} loops",
            )
        rep4 = run_sim(node_json, 4)
        # Vars: frame, input, rise_count, fall_count, curr_r, prev_r,
        # curr_f, prev_f, after.
        self.assertEqual(rep4["var_values"][:4], [4, 1, 2, 1])
        self.assertEqual(rep4["var_values"][8], 7)

    def test_edge_inside_when_and_edge_inside_edge(self):
        def make_node():
            frame = Var(0)
            input_v = Var(0)
            count = Var(0)
            frame.iadd(1)
            when(frame == 1, lambda: input_v.set(1))
            when(frame == 2, lambda: input_v.set(0))
            with when(frame >= 1):
                with on_falling(input_v == 1):
                    with on_rising(input_v == 1):
                        count.iadd(1)

        def make_baseline():
            frame = Var(0)
            input_v = Var(0)
            count = Var(0)
            frame.iadd(1)
            when(frame == 1, lambda: input_v.set(1))
            when(frame == 2, lambda: input_v.set(0))

            def outer_body():
                curr_o = Var(0)
                prev_o = Var(0)
                curr_o.set(0)
                when(input_v == 1, lambda: curr_o.set(1))

                def fire_o():
                    # Falling fire gate: curr_o == 0 AND prev_o == 1.
                    def fire_body():
                        curr_i = Var(0)
                        prev_i = Var(0)
                        curr_i.set(0)
                        when(input_v == 1, lambda: curr_i.set(1))
                        when(
                            curr_i == 1,
                            lambda: when(prev_i == 0, lambda: count.iadd(1)),
                        )
                        when(curr_i == 1, lambda: prev_i.set(1))
                        when(curr_i == 0, lambda: prev_i.set(0))

                    when(prev_o == 1, fire_body)

                when(curr_o == 0, fire_o)
                when(curr_o == 1, lambda: prev_o.set(1))
                when(curr_o == 0, lambda: prev_o.set(0))

            when(frame >= 1, outer_body)

        node_json = build_module(make_node).to_json()
        baseline_json = build_module(make_baseline).to_json()
        self.assertEqual(run_lower(node_json), run_lower(baseline_json))
        for loops in [1, 2, 3]:
            self.assertEqual(
                run_sim(node_json, loops)["var_values"],
                run_sim(baseline_json, loops)["var_values"],
                f"nested sim traces diverge at {loops} loops",
            )


if __name__ == "__main__":
    unittest.main()
