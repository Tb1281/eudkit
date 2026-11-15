"""Ticket #122: dynamic ``div`` / ``mod`` / ``divmod`` are re-entrant.

``_divmod_ladder`` allocates ``rem`` and ``quot`` as ``Var(0)``. ``Var(0)`` is
a module initial value written once by lowering, not a per-invocation reset,
so a second trigger-loop pass starts from pass one's accumulators. The ladder
must zero both accumulators on every call. The general constant-divisor
``divmod`` ladder has the same defect for ``quot``.

The pins below are the ticket's measured values and costs on baseline
``4525769``. The red rows must use a ``Var`` divisor; ``d.div(7)`` takes the
already-resetting constant fast path and is a green control.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
import unittest

from py import Var, build_module, divmod, while_loop
from py.tests.helpers import REPO_ROOT, run_sim

FIXTURES = (
    "dynamic_div",
    "dynamic_mod",
    "dynamic_divmod",
    "const_divmod",
    "while_dynamic_div",
    "while_expression_divisor",
    "mul_control",
    "literal_divisor_control",
    "divisor_zero_control",
)


def _build(fixture: str):
    """Build one ticket fixture and record the ids of the vars under test."""
    captured: dict[str, tuple[int, ...]] = {}

    def prog() -> None:
        if fixture == "dynamic_div":
            d = Var(1000000)
            e = Var(7)
            f = Var(0)
            f.set(d.div(e))
            captured["ids"] = (f.id,)
        elif fixture == "dynamic_mod":
            d = Var(1000000)
            e = Var(7)
            f = Var(0)
            f.set(d.mod(e))
            captured["ids"] = (f.id,)
        elif fixture == "dynamic_divmod":
            d = Var(1000000)
            e = Var(7)
            q, r = divmod(d, e)
            q_copy = Var(0)
            r_copy = Var(0)
            q_copy.set(q)
            r_copy.set(r)
            captured["ids"] = (q.id, r.id, q_copy.id, r_copy.id)
        elif fixture == "const_divmod":
            d = Var(1000000)
            q, r = divmod(d, 7)
            q_copy = Var(0)
            r_copy = Var(0)
            q_copy.set(q)
            r_copy.set(r)
            captured["ids"] = (q.id, r.id, q_copy.id, r_copy.id)
        elif fixture == "while_dynamic_div":
            d = Var(1000000)
            e = Var(7)
            n = Var(0)
            f = Var(0)
            acc = Var(0)
            while_loop(
                n.lt(5),
                lambda: (f.set(d.div(e)), acc.iadd(f), n.iadd(1)),
            )
            captured["ids"] = (f.id, acc.id)
        elif fixture == "while_expression_divisor":
            d = Var(1000000)
            e = Var(7)
            n = Var(0)
            f = Var(0)
            acc = Var(0)
            while_loop(
                n.lt(5),
                lambda: (f.set((d + n).div(e)), acc.iadd(f), n.iadd(1)),
            )
            captured["ids"] = (f.id, acc.id)
        elif fixture == "mul_control":
            d = Var(1000000)
            e = Var(7)
            f = Var(0)
            f.set(d.mul(e))
            captured["ids"] = (f.id,)
        elif fixture == "literal_divisor_control":
            d = Var(1000000)
            f = Var(0)
            f.set(d.div(7))
            captured["ids"] = (f.id,)
        elif fixture == "divisor_zero_control":
            d = Var(1000000)
            e = Var(0)
            f = Var(0)
            g = Var(0)
            f.set(d.div(e))
            g.set(d.mod(e))
            captured["ids"] = (f.id, g.id)
        else:  # pragma: no cover - guards the fixture table
            raise AssertionError(f"unknown fixture {fixture!r}")

    return build_module(prog), captured["ids"]


def _values_at_loops(fixture: str, loops=(1, 2, 5)) -> dict[int, list[int]]:
    mod, ids = _build(fixture)
    hir = mod.to_json()
    out: dict[int, list[int]] = {}
    for n in loops:
        var_values = run_sim(hir, n)["var_values"]
        out[n] = [var_values[var_id] for var_id in ids]
    return out


def _cost(fixture: str) -> tuple[tuple[int, int, int, int], tuple[int, int, int]]:
    mod, _ = _build(fixture)
    report = run_sim(mod.to_json(), 1)
    static = report["static_cost"]
    executed = report["executed_cost"]
    return (
        (
            static["triggers"],
            static["conditions"],
            static["actions"],
            static["total_bytes"],
        ),
        (
            executed["triggers"],
            executed["conditions"],
            executed["actions"],
        ),
    )


COST_PINS = {
    # One dynamic `div` site: the reset adds two acts but no bytes.
    "dynamic_div": ((259, 96, 741, 623684), (280, 71, 5010)),
    "dynamic_mod": ((259, 96, 741, 623688), (280, 71, 5010)),
    "dynamic_divmod": ((260, 96, 752, 626096), (282, 71, 5085)),
    "const_divmod": ((36, 30, 144, 86696), (38, 30, 226)),
    "while_dynamic_div": ((261, 97, 749, 628504), (1416, 361, 25410)),
    "while_expression_divisor": ((261, 97, 758, 628500), (1416, 361, 25460)),
}


class DynamicDivisionReentrancy(unittest.TestCase):
    """Dynamic ladders produce the same pair on every trigger-loop pass."""

    def test_dynamic_div_is_reentrant(self):
        for loops, got in _values_at_loops("dynamic_div").items():
            self.assertEqual(got, [142857], f"loops={loops}")

    def test_dynamic_mod_is_reentrant(self):
        for loops, got in _values_at_loops("dynamic_mod").items():
            self.assertEqual(got, [1], f"loops={loops}")

    def test_dynamic_divmod_is_reentrant(self):
        for loops, got in _values_at_loops("dynamic_divmod").items():
            # q, r, q_copy, r_copy: both the ladder results and their copies.
            self.assertEqual(got, [142857, 1, 142857, 1], f"loops={loops}")

    def test_const_divmod_is_reentrant(self):
        for loops, got in _values_at_loops("const_divmod").items():
            # q, r, q_copy, r_copy.
            self.assertEqual(got, [142857, 1, 142857, 1], f"loops={loops}")

    def test_while_loop_dynamic_div_is_reentrant(self):
        got = _values_at_loops("while_dynamic_div", loops=(1,))[1]
        self.assertEqual(got, [142857, 714285])

    def test_while_loop_expression_divisor_is_reentrant(self):
        got = _values_at_loops("while_expression_divisor", loops=(1,))[1]
        self.assertEqual(got, [142857, 714285])


class CostPins(unittest.TestCase):
    """The reset is emitted as acts, so pin costs as well as values."""

    def test_static_and_executed_cost(self):
        for fixture, (static, executed) in COST_PINS.items():
            with self.subTest(fixture=fixture):
                self.assertEqual(_cost(fixture), (static, executed))


class UnchangedControls(unittest.TestCase):
    """Paths that were already re-entrant before #122 stay pinned."""

    def test_mul_control_is_unchanged(self):
        for loops, got in _values_at_loops("mul_control").items():
            self.assertEqual(got, [7000000], f"loops={loops}")

    def test_literal_divisor_control_is_already_green(self):
        for loops, got in _values_at_loops("literal_divisor_control").items():
            self.assertEqual(got, [142857], f"loops={loops}")

    def test_divisor_zero_contract_is_unchanged(self):
        for loops, got in _values_at_loops("divisor_zero_control").items():
            self.assertEqual(got, [0, 1000000], f"loops={loops}")


_TS_PARITY_SCRIPT = textwrap.dedent(
    """
    import { Var, buildModule, divmod, whileLoop } from "./ts/index.ts";

    const names = [
      "dynamic_div",
      "dynamic_mod",
      "dynamic_divmod",
      "const_divmod",
      "while_dynamic_div",
      "while_expression_divisor",
      "mul_control",
      "literal_divisor_control",
      "divisor_zero_control",
    ];

    function fixture(name) {
      return buildModule(() => {
        if (name === "dynamic_div") {
          const d = Var(1000000);
          const e = Var(7);
          const f = Var(0);
          f.set(d.div(e));
        } else if (name === "dynamic_mod") {
          const d = Var(1000000);
          const e = Var(7);
          const f = Var(0);
          f.set(d.mod(e));
        } else if (name === "dynamic_divmod") {
          const d = Var(1000000);
          const e = Var(7);
          const [q, r] = divmod(d, e);
          const qCopy = Var(0);
          const rCopy = Var(0);
          qCopy.set(q);
          rCopy.set(r);
        } else if (name === "const_divmod") {
          const d = Var(1000000);
          const [q, r] = divmod(d, 7);
          const qCopy = Var(0);
          const rCopy = Var(0);
          qCopy.set(q);
          rCopy.set(r);
        } else if (name === "while_dynamic_div") {
          const d = Var(1000000);
          const e = Var(7);
          const n = Var(0);
          const f = Var(0);
          const acc = Var(0);
          whileLoop(n.lt(5), () => {
            f.set(d.div(e));
            acc.iadd(f);
            n.iadd(1);
          });
        } else if (name === "while_expression_divisor") {
          const d = Var(1000000);
          const e = Var(7);
          const n = Var(0);
          const f = Var(0);
          const acc = Var(0);
          whileLoop(n.lt(5), () => {
            f.set(d.add(n).div(e));
            acc.iadd(f);
            n.iadd(1);
          });
        } else if (name === "mul_control") {
          const d = Var(1000000);
          const e = Var(7);
          const f = Var(0);
          f.set(d.mul(e));
        } else if (name === "literal_divisor_control") {
          const d = Var(1000000);
          const f = Var(0);
          f.set(d.div(7));
        } else if (name === "divisor_zero_control") {
          const d = Var(1000000);
          const e = Var(0);
          const f = Var(0);
          const g = Var(0);
          f.set(d.div(e));
          g.set(d.mod(e));
        } else {
          throw new Error(`unknown fixture ${name}`);
        }
      });
    }

    const out = {};
    for (const name of names) out[name] = fixture(name).toJson();
    process.stdout.write(JSON.stringify(out));
    """
)


class FrontendParity(unittest.TestCase):
    """Python and TypeScript emit byte-identical HIR for every fixture."""

    def test_py_and_ts_hir_are_byte_identical(self):
        py_hir = {name: _build(name)[0].to_json() for name in FIXTURES}
        res = subprocess.run(
            ["node", "--input-type=module", "-e", _TS_PARITY_SCRIPT],
            cwd=str(REPO_ROOT),
            check=True,
            capture_output=True,
            text=True,
        )
        ts_hir = json.loads(res.stdout)
        self.assertEqual(set(ts_hir), set(FIXTURES))
        for name in FIXTURES:
            self.assertEqual(py_hir[name], ts_hir[name], name)


if __name__ == "__main__":
    unittest.main()
