"""Cycle 2 ticket 03: one-ladder ``divmod``.

The dynamic-divisor restoring ladder already materialises ``quot`` and
``rem``; ``div`` and ``mod`` each pay for the whole ladder and copy one
result. ``divmod`` exposes both from a single ladder:

* ``a.divmod(b)`` and the builtin ``divmod(a, b)`` (via ``__divmod__``) return
  a ``(quot, rem)`` pair of fresh ``Var`` s;
* ``den == 0`` keeps the current contract and yields ``(0, dividend)``;
* constant divisors reuse the constant-divisor ladder and still return both
  values;
* ``div`` and ``mod`` keep their current single-result HIR (the existing
  ``test_ticket55`` / ``test_ticket102`` pins move only by the two #122 reset
  actions).

Cost pins for one paired site over ``a = 1000``, ``b = 7`` (cycle 4 spec):
static 259 T / 96 C / 743 A and 623,688 B total, executed 271 / 68 / 4,789.
The single-result ``div`` site is 259 / 96 / 741 and 271 / 68 / 4,787, so the
pair costs exactly the two extra copy-back actions of the second result.
"""

from __future__ import annotations

import subprocess
import textwrap
import unittest

from py import Var, build_module
from py.tests.helpers import REPO_ROOT, run_sim

# The ticket 102 dynamic-divisor matrix: every divisor is exercised at build
# time as a Var, including zero, so the "divisor zero -> (0, dividend)"
# contract is covered by the same lattice.
DIV_NUM = (
    0,
    1,
    2,
    7,
    255,
    256,
    65535,
    65536,
    0x12345678,
    0x7FFFFFFF,
    0x80000000,
    0xFFFFFFFF,
)
DIV_DEN = (
    0,
    1,
    2,
    3,
    7,
    255,
    256,
    1000,
    65535,
    65536,
    0x7FFFFFFF,
    0x80000000,
    0x80000001,
    0xFFFFFFFE,
    0xFFFFFFFF,
)

CONST_DIVISORS = (1, 2, 3, 7, 16, 0xFFFFFFFF)
CONST_DIVIDENDS = (0, 1, 7, 255, 1000, 0xFFFFFFFF)

# One paired site: `q, r = divmod(a, b)`.
DIVMOD_SITE_STATIC = {
    "triggers": 259,
    "conditions": 96,
    "actions": 743,
    "total_bytes": 623688,
}
DIVMOD_SITE_EXECUTED = {"triggers": 271, "conditions": 68, "actions": 4789}

BATCH = 30


def _run_batched(cells, emit) -> list[tuple[tuple[int, ...], tuple[int, int]]]:
    """Build `cells` in batches and return (cell, (quot, rem)) pairs."""
    out: list[tuple[tuple[int, ...], tuple[int, int]]] = []
    for start in range(0, len(cells), BATCH):
        batch = cells[start : start + BATCH]
        slots: list[tuple[int, int]] = []

        def build(batch=batch):
            for cell in batch:
                slots.append(emit(*cell))

        values = run_sim(build_module(build).to_json(), 1)["var_values"]
        out.extend(
            (cell, (values[q], values[r]))
            for cell, (q, r) in zip(batch, slots)
        )
    return out


def _native(a: int, b: int) -> tuple[int, int]:
    a &= 0xFFFFFFFF
    b &= 0xFFFFFFFF
    if b == 0:
        return 0, a
    return a // b, a % b


class DivmodLattice(unittest.TestCase):
    """Dynamic-divisor `divmod` matches native divmod for all 180 cells."""

    def test_builtin_divmod_180_cell_lattice(self):
        def emit(a: int, b: int) -> tuple[int, int]:
            x = Var(a)
            y = Var(b)
            q, r = divmod(x, y)
            return q.id, r.id

        cells = [(a, b) for a in DIV_NUM for b in DIV_DEN]
        self.assertEqual(len(cells), 180)
        for (a, b), got in _run_batched(cells, emit):
            want = _native(a, b)
            self.assertEqual(got, want, f"divmod({a:#x}, {b:#x})")

    def test_method_divmod_returns_both_from_one_ladder(self):
        def emit(a: int, b: int) -> tuple[int, int]:
            x = Var(a)
            y = Var(b)
            q, r = x.divmod(y)
            return q.id, r.id

        cells = [(a, b) for a in DIV_NUM for b in DIV_DEN]
        for (a, b), got in _run_batched(cells, emit):
            self.assertEqual(got, _native(a, b), f"{a:#x}.divmod({b:#x})")


class DivmodAliasing(unittest.TestCase):
    """Both results are fresh Vars, so `q, r = divmod(q, r)` is safe."""

    def test_rebinding_the_operand_names(self):
        captured: dict[str, int] = {}

        def build():
            q = Var(1000)
            r = Var(7)
            q, r = divmod(q, r)
            captured["q"], captured["r"] = q.id, r.id
            # Both results are usable immediately after the call.
            q.iadd(r)

        values = run_sim(build_module(build).to_json(), 1)["var_values"]
        self.assertEqual(values[captured["q"]], 142 + 6)
        self.assertEqual(values[captured["r"]], 6)

    def test_results_are_distinct_from_the_operands(self):
        captured: dict[str, tuple[int, int, int, int]] = {}

        def build():
            a = Var(1000)
            b = Var(7)
            q, r = a.divmod(b)
            captured["ids"] = (a.id, b.id, q.id, r.id)

        build_module(build)
        a_id, b_id, q_id, r_id = captured["ids"]
        self.assertEqual(len({a_id, b_id, q_id, r_id}), 4)
        self.assertNotIn(q_id, (a_id, b_id))
        self.assertNotIn(r_id, (a_id, b_id))


class DivmodConstantDivisor(unittest.TestCase):
    """Constant-divisor divmod returns the same pair as the dynamic path."""

    def test_constant_matches_dynamic_and_native(self):
        for c in CONST_DIVISORS:
            for a in CONST_DIVIDENDS:
                with self.subTest(a=hex(a), c=hex(c)):
                    captured: dict[str, tuple[int, int, int, int]] = {}

                    def build(a=a, c=c):
                        x = Var(a)
                        qc, rc = divmod(x, c)
                        xd = Var(a)
                        yd = Var(c)
                        qd, rd = divmod(xd, yd)
                        captured["ids"] = (qc.id, rc.id, qd.id, rd.id)

                    mod = build_module(build)
                    qc_id, rc_id, qd_id, rd_id = captured["ids"]

                    values = run_sim(mod.to_json(), 1)["var_values"]
                    want = _native(a, c)
                    self.assertEqual(
                        (values[qc_id], values[rc_id]), want, f"const {a:#x}/{c:#x}"
                    )
                    self.assertEqual(
                        (values[qd_id], values[rd_id]), want, f"dynamic {a:#x}/{c:#x}"
                    )


class DivmodCostPin(unittest.TestCase):
    """One paired site has the static and executed cost from the spec."""

    def test_static_and_executed_cost(self):
        captured: dict[str, int] = {}

        def build():
            a = Var(1000)
            b = Var(7)
            q, r = divmod(a, b)
            captured["q"], captured["r"] = q.id, r.id

        report = run_sim(build_module(build).to_json(), 1)
        self.assertEqual(
            {
                "triggers": report["static_cost"]["triggers"],
                "conditions": report["static_cost"]["conditions"],
                "actions": report["static_cost"]["actions"],
                "total_bytes": report["static_cost"]["total_bytes"],
            },
            DIVMOD_SITE_STATIC,
        )
        self.assertEqual(
            {
                "triggers": report["executed_cost"]["triggers"],
                "conditions": report["executed_cost"]["conditions"],
                "actions": report["executed_cost"]["actions"],
            },
            DIVMOD_SITE_EXECUTED,
        )

    def test_one_ladder_is_cheaper_than_div_plus_mod(self):
        captured: dict[str, int] = {}

        def build():
            a = Var(1000)
            b = Var(7)
            q, r = divmod(a, b)
            captured["q"], captured["r"] = q.id, r.id

        report = run_sim(build_module(build).to_json(), 1)
        self.assertLess(
            report["executed_cost"]["triggers"],
            667,  # the two-ladder div + mod baseline
        )


_TS_PARITY_SCRIPT = textwrap.dedent(
    """
    import { Var, buildModule, divmod } from "./ts/index.ts";

    const mod = buildModule(() => {
      const a = Var(1000);
      const b = Var(7);
      const [q, r] = divmod(a, b);
      q.iadd(r);
      const s = Var(0x12345678);
      const t = Var(1000);
      const [u, v] = s.divmod(t);
      u.iadd(v);
    });

    process.stdout.write(mod.toJson());
    """
)


class DivmodFrontendParity(unittest.TestCase):
    """Python and TypeScript emit byte-identical HIR for the divmod fixture."""

    def test_py_and_ts_hir_are_byte_identical(self):
        def build():
            a = Var(1000)
            b = Var(7)
            q, r = divmod(a, b)
            q.iadd(r)
            s = Var(0x12345678)
            t = Var(1000)
            u, v = s.divmod(t)
            u.iadd(v)

        py_hir = build_module(build).to_json()
        res = subprocess.run(
            ["node", "--input-type=module", "-e", _TS_PARITY_SCRIPT],
            cwd=str(REPO_ROOT),
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(py_hir, res.stdout)


if __name__ == "__main__":
    unittest.main()
