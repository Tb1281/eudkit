"""Ticket #102: shared ``mul_shift_add`` and the single-diff gated div ladder.

Follow-up to spike #82 (``docs/spikes/c5-arith-share`` on branch
``spike/c5-arith-share``). Two changes are pinned here:

* Var x Var ``mul`` now routes through ``py.var.mul_shift_add`` (the kernel the
  camera helper always had), deleting the 528-trigger bit-pair matrix.
* Var / Var ``div`` / ``mod`` keeps the restoring ladder but replaces the
  per-bit ``diff`` + ``sub_amt`` pair with one hoisted ``diff`` and a gated
  ``when([diff.eq(0), den.ge(1)])``, accumulating into a fresh ``quot`` and
  copying back once. The ``den >= 1`` term preserves divide-by-zero == 0.

Measured on one site (``o.set(x.mul(y))`` / ``o.set(x.div(y))``):

===========  ======  ========  =======  =======  =========
case         vars    static T  C        A        bytes
===========  ======  ========  =======  =======  =========
mul before   3       529       1056     529      1,273,844
mul after    5       165       33       392      397,328
div before   69      354       128      834      852,568
div after    6       291       96       771      700,740
===========  ======  ========  =======  =======  =========

Cycle 2 ticket 02 splits the 32 multiplier gates into chunks of 16 and gates
the high chunk on ``b & 0xFFFF0000``. A sparse multiplier skips that chunk
(``0x5``: E 77/17/1,351, previously 137/32/2,356); a dense one pays the one
extra gate (``0xDEADBEEF``: E 205/33/3,920, previously 203/32/3,918).

Cycle 4 hoists ``diff.set(den)`` above the numerator-bit trigger, so transfer
coalescing folds the independent transfers into one page firing per bit. The
current pins are: ``div`` / ``mod`` static 259/96/741, 623,684 B and executed
271/68/4,787; paired ``divmod`` static 259/96/743, 623,688 B and executed
271/68/4,789.

The correctness lattices (99 mul / 180 div cells in py; 49 / 108 in ts) are
the spike's; each batch is built into one module so the whole lattice costs a
handful of simulator runs instead of one per cell.
"""

from __future__ import annotations

import unittest

from py import Var, build_module
from py.tests.helpers import run_sim

MUL_A = (
    0,
    1,
    2,
    255,
    256,
    65535,
    65536,
    0x7FFFFFFF,
    0x80000000,
    0xFFFFFFFF,
    0x12345678,
)
MUL_B = (0, 1, 3, 255, 4096, 65535, 65536, 0x80000000, 0xFFFFFFFF)

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

# One dynamic-multiply site: `o.set(x * y)`.
MUL_SITE_STATIC = {
    "triggers": 165,
    "conditions": 33,
    "actions": 392,
    "trigger_bytes": 397320,
    "blob_bytes": 8,
    "total_bytes": 397328,
}
# One dynamic-division site: `o.set(x // y)`.
DIV_SITE_STATIC = {
    "triggers": 259,
    "conditions": 96,
    "actions": 741,
    "trigger_bytes": 623672,
    "blob_bytes": 12,
    "total_bytes": 623684,
}

BATCH = 30


def _run_batched(cells, emit) -> list[tuple[tuple[int, ...], int]]:
    """Build `cells` in batches and return (cell, output value) pairs."""
    out: list[tuple[tuple[int, ...], int]] = []
    for start in range(0, len(cells), BATCH):
        batch = cells[start : start + BATCH]
        slots: list[int] = []

        def build(batch=batch):
            for cell in batch:
                slots.append(emit(*cell))

        values = run_sim(build_module(build).to_json(), 1)["var_values"]
        out.extend((cell, values[slot]) for cell, slot in zip(batch, slots))
    return out


class MulShiftAddLattice(unittest.TestCase):
    """Var x Var `mul` through the shared kernel, py 99-cell lattice."""

    def test_ninety_nine_cell_lattice(self):
        def emit(a: int, b: int) -> int:
            x = Var(a)
            y = Var(b)
            o = Var(0)
            o.set(x.mul(y))
            return o.id

        cells = [(a, b) for a in MUL_A for b in MUL_B]
        self.assertEqual(len(cells), 99)
        for (a, b), got in _run_batched(cells, emit):
            self.assertEqual(got, (a * b) & 0xFFFFFFFF, f"{a:#x} * {b:#x}")

    def test_dst_aliasing_both_operands(self):
        """The kernel returns a fresh Var, so the copy-back is alias-safe."""
        captured: dict[str, int] = {}

        def build():
            x = Var(6)
            y = Var(7)
            z = Var(3)
            x.set(x.mul(y))  # dst == a
            y.set(z.mul(y))  # dst == b
            captured["x"], captured["y"] = x.id, y.id

        values = run_sim(build_module(build).to_json(), 1)["var_values"]
        self.assertEqual(values[captured["x"]], 42)
        self.assertEqual(values[captured["y"]], 21)


class MulSiteCostPin(unittest.TestCase):
    """Static and executed cost of the one-site multiply probe."""

    def test_static_and_executed_cost(self):
        def build():
            a = Var(0xDEADBEEF)
            b = Var(0xFFFF)
            o = Var(0)
            o.set(a.mul(b))

        report = run_sim(build_module(build).to_json(), 1)
        self.assertEqual(report["static_cost"], MUL_SITE_STATIC)
        # `0xFFFF` is all-zero in its high half, so it takes the chunk-16
        # early exit. The add fires on every set low bit, and the gate adds
        # one visited trigger/condition; `0xDEADBEEF` / `0xFFFFFFFF` are the
        # dense cases.
        self.assertEqual(
            {
                "triggers": report["executed_cost"]["triggers"],
                "conditions": report["executed_cost"]["conditions"],
                "actions": report["executed_cost"]["actions"],
            },
            {"triggers": 119, "conditions": 17, "actions": 2345},
        )


class MulChunkEarlyExit(unittest.TestCase):
    """Chunk-16 gate: sparse multipliers skip the high chunk, dense pays one gate."""

    @staticmethod
    def _report(a: int, b: int):
        def build():
            x = Var(a)
            y = Var(b)
            o = Var(0)
            o.set(x.mul(y))

        return run_sim(build_module(build).to_json(), 1)

    @staticmethod
    def _executed(report) -> dict[str, int]:
        return {
            "triggers": report["executed_cost"]["triggers"],
            "conditions": report["executed_cost"]["conditions"],
            "actions": report["executed_cost"]["actions"],
        }

    def test_chunked_multipliers_match_native_products(self):
        cases = (
            (0xDEADBEEF, 0x5),
            (0xDEADBEEF, 0xFFFF),
            (0xDEADBEEF, 0xDEADBEEF),
            (0xDEADBEEF, 0),
            (0xDEADBEEF, 1),
            (0xDEADBEEF, 0xFFFFFFFF),
        )
        for a, b in cases:
            with self.subTest(a=hex(a), b=hex(b)):
                slot: list[int] = []

                def build(a=a, b=b):
                    o = Var(0)
                    o.set(Var(a).mul(Var(b)))
                    slot.append(o.id)

                values = run_sim(build_module(build).to_json(), 1)["var_values"]
                self.assertEqual(values[slot[0]], (a * b) & 0xFFFFFFFF)

    def test_small_multiplier_skips_the_high_chunk(self):
        report = self._report(0xDEADBEEF, 0x5)
        self.assertEqual(report["static_cost"], MUL_SITE_STATIC)
        executed = self._executed(report)
        self.assertLessEqual(executed["triggers"], 77)
        self.assertLessEqual(executed["conditions"], 17)
        self.assertLessEqual(executed["actions"], 1351)

    def test_dense_multiplier_pays_at_most_one_gate(self):
        # Baseline 203/32/3,918: the gate may add at most 2T/2C/4A.
        report = self._report(0xDEADBEEF, 0xDEADBEEF)
        self.assertEqual(report["static_cost"], MUL_SITE_STATIC)
        executed = self._executed(report)
        self.assertLessEqual(executed["triggers"], 205)
        self.assertLessEqual(executed["conditions"], 34)
        self.assertLessEqual(executed["actions"], 3922)


class DivLadderLattice(unittest.TestCase):
    """Var/Var `div` and `mod`, py 180-cell lattice including `den == 0`."""

    def _div_cells(self, op: str):
        def emit(a: int, b: int) -> int:
            x = Var(a)
            y = Var(b)
            o = Var(0)
            o.set(x.div(y) if op == "div" else x.mod(y))
            return o.id

        cells = [(a, b) for a in DIV_NUM for b in DIV_DEN]
        self.assertEqual(len(cells), 180)
        for (a, b), got in _run_batched(cells, emit):
            want = (a // b if b else 0) if op == "div" else (a % b if b else a)
            self.assertEqual(got & 0xFFFFFFFF, want & 0xFFFFFFFF, f"{a:#x} {op} {b:#x}")

    def test_division_lattice(self):
        self._div_cells("div")

    def test_modulo_lattice(self):
        self._div_cells("mod")

    def test_dst_aliasing_both_operands(self):
        """Fresh `quot`/`rem` and one copy-back keep `dst == num`/`den` safe."""
        captured: dict[str, int] = {}

        def build():
            x = Var(1000)
            y = Var(7)
            x.set(x.div(y))  # dst == num
            q = Var(1000)
            d = Var(7)
            d.set(q.div(d))  # dst == den
            r = Var(1000)
            e = Var(7)
            e.set(r.mod(e))  # dst == den, mod path
            captured["x"], captured["d"], captured["e"] = x.id, d.id, e.id

        values = run_sim(build_module(build).to_json(), 1)["var_values"]
        self.assertEqual(values[captured["x"]], 142)
        self.assertEqual(values[captured["d"]], 142)
        self.assertEqual(values[captured["e"]], 1000 % 7)


class DivSiteCostPin(unittest.TestCase):
    """Static and executed cost of the one-site division probe."""

    def test_static_and_executed_cost(self):
        def build():
            a = Var(1000)
            b = Var(7)
            o = Var(0)
            o.set(a.div(b))

        report = run_sim(build_module(build).to_json(), 1)
        self.assertEqual(report["static_cost"], DIV_SITE_STATIC)
        self.assertEqual(
            {
                "triggers": report["executed_cost"]["triggers"],
                "conditions": report["executed_cost"]["conditions"],
                "actions": report["executed_cost"]["actions"],
            },
            {"triggers": 271, "conditions": 68, "actions": 4787},
        )

    def test_divide_by_zero_executed_cost(self):
        def build():
            a = Var(12345)
            b = Var(0)
            o = Var(0)
            o.set(a.div(b))

        report = run_sim(build_module(build).to_json(), 1)
        self.assertEqual(report["static_cost"], DIV_SITE_STATIC)
        # `den >= 1` fails on every bit, so the gated body never runs.
        self.assertEqual(
            {
                "triggers": report["executed_cost"]["triggers"],
                "conditions": report["executed_cost"]["conditions"],
                "actions": report["executed_cost"]["actions"],
            },
            {"triggers": 259, "conditions": 96, "actions": 4491},
        )


class SharedKernelSeam(unittest.TestCase):
    """`camera` and `Var.mul` use the same kernel object."""

    def test_camera_imports_the_shared_kernel(self):
        from py import camera
        from py.var import mul_shift_add

        self.assertIs(camera._mul_shift_add, mul_shift_add)


if __name__ == "__main__":
    unittest.main()
