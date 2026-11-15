"""Cycle 1 runtime pins for bitwise, shift, and constant-multiply lowering.

The optimized paths replace the old per-bit trigger ladders with masked
``SetDeathsX`` kernels:

* ``~x`` / ``-x`` are two/three in-place acts, no temporary;
* ``x & y`` / ``x | y`` / ``x ^ y`` patch one trigger's mask/value fields
  from ``y`` at build time, so the runtime cost is one trigger;
* constant shifts use eudplib's alternating-lane SetTo/Add/Subtract acts,
  which always fit one trigger; dynamic shifts compose those kernels under
  five bit gates plus a ``>= 32`` zero guard;
* constant multiplies lower the single-bit, all-ones and cost-gated k-term
  shift-add chain cases to shifts/adds instead of a 32-trigger scan, falling
  back to that window scan for constants the chain cannot beat.

Everything is simulated through the native HIR runner, so the uint32 results
are checked against Python's own arithmetic rather than against the lowering.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from py import (
    AtLeast,
    Deaths,
    DeathsX,
    SetDeathsX,
    SetTo,
    Trigger,
    TriggerStruct,
    Var,
    build_module,
    byte,
    dword,
    when,
    while_loop,
)
from py.tests.helpers import EUDKIT_COMPILE, REPO_ROOT, run_sim

EDGE_VALUES = [
    0,
    1,
    0xFFFFFFFF,
    0x80000000,
    0x55555555,
    0xAAAAAAAA,
    0xDEADBEEF,
]
# Extra constants exercise both operand positions on the constant fast paths.
EXTRA_CONSTANTS = [0x0F0F0F0F, 0xFFFF0000, 0x12345678, 0x0000FFFF, 0x80000001]
SHIFT_AMOUNTS = tuple(range(34))


def _run_batched(cells, build_cell, batch_size=24):
    """Build ``cells`` in batches and return ``(cell, output value)`` pairs."""
    out = []
    for start in range(0, len(cells), batch_size):
        batch = cells[start : start + batch_size]
        slots: list[int] = []

        def build(batch=batch):
            for cell in batch:
                slots.append(build_cell(*cell))

        values = run_sim(build_module(build).to_json(), 1)["var_values"]
        out.extend((cell, values[slot]) for cell, slot in zip(batch, slots))
    return out


def _run_dump_ir(hir_json: str) -> str:
    with tempfile.TemporaryDirectory(prefix="eudkit-py-perf-dump-") as tmpdir:
        hir_path = Path(tmpdir) / "prog.hir.json"
        hir_path.write_text(hir_json, encoding="utf-8")
        return subprocess.check_output(
            [str(EUDKIT_COMPILE), "dump-ir", str(hir_path)],
            text=True,
        )


def _allocation_lines(dump_ir: str) -> list[str]:
    lines = dump_ir.split("\n")
    start = lines.index("Allocations:")
    out: list[str] = []
    for line in lines[start + 1 :]:
        if not line.startswith("  var["):
            break
        out.append(line)
    return out


#: Constant-multiply cases added by the Cycle 1 review: the top bit, both
#: popcount-2 forms (including ``b0 == 0``), and the zero/one fast paths.
MUL_CONSTANTS = [0, 1, 0x80000000, 3, 5, 0x80000001, 12, 0xFFFFFFFF]

#: Masked-operation fixture: a dirty raw lane is seeded through a same-id
#: full-mask alias, then each kernel must write only inside ``MASK``.
MASK = 0x0F0F0F0F
DIRTY_RAW = 0xA5B6C7D8
MASKED_VALUE = DIRTY_RAW & MASK

#: Adjacent (same-page) TriggerStruct fields with narrow hardware masks.
MaskedFields = TriggerStruct("MaskedFields", {"lo": byte(), "hi": byte(8), "sink": dword()})
LO_MASK = 0xFF
HI_MASK = 0xFF00
LO_RAW = 0xABCDFF12
HI_RAW = 0x12343478


class UnaryBitwiseMatrix(unittest.TestCase):
    """``~a`` and ``-a`` agree with Python on every uint32 edge value."""

    def test_bitnot_and_neg(self):
        cells = [("bitnot", value) for value in EDGE_VALUES]
        cells += [("neg", value) for value in EDGE_VALUES]

        def emit(op, value):
            a = Var(value)
            out = Var(0)
            if op == "bitnot":
                out.set(~a)
            else:
                out.set(-a)
            return out.id

        for (op, value), got in _run_batched(cells, emit, batch_size=14):
            want = (~value) & 0xFFFFFFFF if op == "bitnot" else (-value) & 0xFFFFFFFF
            with self.subTest(op=op, value=hex(value)):
                self.assertEqual(got, want)


class BinaryBitwiseMatrix(unittest.TestCase):
    """``&``, ``|`` and ``^`` agree for constants and Var/Var pairs."""

    def test_constant_operand_matrix(self):
        constants = EDGE_VALUES + EXTRA_CONSTANTS
        cells = [
            (op, value, constant)
            for op in ("bitand", "bitor", "bitxor")
            for value in EDGE_VALUES
            for constant in constants
        ]

        def emit(op, value, constant):
            a = Var(value)
            out = Var(0)
            if op == "bitand":
                out.set(a & constant)
            elif op == "bitor":
                out.set(a | constant)
            else:
                out.set(a ^ constant)
            return out.id

        for (op, value, constant), got in _run_batched(cells, emit, batch_size=30):
            if op == "bitand":
                want = value & constant
            elif op == "bitor":
                want = value | constant
            else:
                want = value ^ constant
            with self.subTest(op=op, value=hex(value), constant=hex(constant)):
                self.assertEqual(got, want & 0xFFFFFFFF)

    def test_reverse_constant_operand_matrix(self):
        cells = [
            (op, constant, value)
            for op in ("bitand", "bitor", "bitxor")
            for constant in EXTRA_CONSTANTS
            for value in EDGE_VALUES
        ]

        def emit(op, constant, value):
            a = Var(value)
            out = Var(0)
            if op == "bitand":
                out.set(constant & a)
            elif op == "bitor":
                out.set(constant | a)
            else:
                out.set(constant ^ a)
            return out.id

        for (op, constant, value), got in _run_batched(cells, emit, batch_size=30):
            if op == "bitand":
                want = constant & value
            elif op == "bitor":
                want = constant | value
            else:
                want = constant ^ value
            with self.subTest(op=op, constant=hex(constant), value=hex(value)):
                self.assertEqual(got, want & 0xFFFFFFFF)

    def test_var_var_matrix(self):
        cells = [
            (op, lhs, rhs)
            for op in ("bitand", "bitor", "bitxor")
            for lhs in EDGE_VALUES
            for rhs in EDGE_VALUES
        ]

        def emit(op, lhs, rhs):
            a = Var(lhs)
            b = Var(rhs)
            out = Var(0)
            if op == "bitand":
                out.set(a & b)
            elif op == "bitor":
                out.set(a | b)
            else:
                out.set(a ^ b)
            return out.id

        for (op, lhs, rhs), got in _run_batched(cells, emit, batch_size=21):
            if op == "bitand":
                want = lhs & rhs
            elif op == "bitor":
                want = lhs | rhs
            else:
                want = lhs ^ rhs
            with self.subTest(op=op, lhs=hex(lhs), rhs=hex(rhs)):
                self.assertEqual(got, want & 0xFFFFFFFF)


class ConstantMultiplyMatrix(unittest.TestCase):
    """Constant ``mul`` fast paths agree for every pinned constant."""

    def test_constant_multiply_matrix(self):
        cells = [
            (value, constant)
            for value in EDGE_VALUES
            for constant in MUL_CONSTANTS
        ]

        def emit(value, constant):
            a = Var(value)
            out = Var(0)
            out.set(a * constant)
            return out.id

        for (value, constant), got in _run_batched(cells, emit, batch_size=30):
            with self.subTest(value=hex(value), constant=hex(constant)):
                self.assertEqual(got, (value * constant) & 0xFFFFFFFF)


class BitCellGuard(unittest.TestCase):
    """The patched trigger's mask=1 placeholder must force a DwordCell."""

    def test_boolean_init_self_store_stays_dword(self):
        captured: dict[str, int] = {}
        expected: dict[str, int] = {}

        def build():
            for op in ("bitand", "bitor", "bitxor"):
                v = Var(0)
                w = Var(1)
                if op == "bitand":
                    v.set(v & w)
                    expected[op] = 0
                elif op == "bitor":
                    v.set(v | w)
                    expected[op] = 1
                else:
                    v.set(v ^ w)
                    expected[op] = 1
                captured[op] = v.id

        mod = build_module(build)
        allocations = _allocation_lines(_run_dump_ir(mod.to_json()))
        by_id = {
            int(line.split("var[", 1)[1].split("]", 1)[0]): line
            for line in allocations
        }
        for op, var_id in captured.items():
            with self.subTest(op=op, guard="allocation"):
                self.assertIn("DwordCell(", by_id[var_id])
                self.assertNotIn("BitCell(", by_id[var_id])

        values = run_sim(mod.to_json(), 1)["var_values"]
        for op, var_id in captured.items():
            with self.subTest(op=op, guard="value"):
                self.assertEqual(values[var_id], expected[op])


class ShiftMatrix(unittest.TestCase):
    """Constant and variable shifts agree for every amount 0..33."""

    def test_constant_shift_matrix(self):
        cells = [
            (op, value, amount)
            for op in ("shl", "shr")
            for value in EDGE_VALUES
            for amount in SHIFT_AMOUNTS
        ]

        def emit(op, value, amount):
            a = Var(value)
            out = Var(0)
            if op == "shl":
                out.set(a << amount)
            else:
                out.set(a >> amount)
            return out.id

        for (op, value, amount), got in _run_batched(cells, emit, batch_size=34):
            if op == "shl":
                want = 0 if amount >= 32 else (value << amount) & 0xFFFFFFFF
            else:
                want = 0 if amount >= 32 else value >> amount
            with self.subTest(op=op, value=hex(value), amount=amount):
                self.assertEqual(got, want)

    def test_dynamic_shift_matrix(self):
        cells = [
            (op, value, amount)
            for op in ("shl", "shr")
            for value in EDGE_VALUES
            for amount in SHIFT_AMOUNTS
        ]

        def emit(op, value, amount):
            a = Var(value)
            shift = Var(amount)
            out = Var(0)
            if op == "shl":
                out.set(a << shift)
            else:
                out.set(a >> shift)
            return out.id

        for (op, value, amount), got in _run_batched(cells, emit, batch_size=14):
            if op == "shl":
                want = 0 if amount >= 32 else (value << amount) & 0xFFFFFFFF
            else:
                want = 0 if amount >= 32 else value >> amount
            with self.subTest(op=op, value=hex(value), amount=amount):
                self.assertEqual(got, want)


class AliasingMatrix(unittest.TestCase):
    """In-place writes stay correct when dst aliases either operand."""

    def test_listed_aliasing_cases(self):
        a = 0xDEADBEEF
        b = 0x0F0F0F0F
        cases = [
            ("a &= a", lambda x, y: x.__iand__(x), (a, b)),
            ("a &= b", lambda x, y: x.__iand__(y), (a & b, b)),
            ("b = a & b", lambda x, y: y.set(x & y), (a, a & b)),
            ("a |= b", lambda x, y: x.__ior__(y), (a | b, b)),
            ("b = a | b", lambda x, y: y.set(x | y), (a, a | b)),
            ("a ^= a", lambda x, y: x.__ixor__(x), (0, b)),
            ("a ^= b", lambda x, y: x.__ixor__(y), (a ^ b, b)),
            ("b = a ^ b", lambda x, y: y.set(x ^ y), (a, a ^ b)),
            ("a <<= a", lambda x, y: x.__ilshift__(x), (0, b)),
            ("a <<= 1", lambda x, y: x.__ilshift__(1), ((a << 1) & 0xFFFFFFFF, b)),
            ("a >>= 1", lambda x, y: x.__irshift__(1), (a >> 1, b)),
            ("a *= 0xFFFFFFFF", lambda x, y: x.__imul__(0xFFFFFFFF), ((-a) & 0xFFFFFFFF, b)),
            ("a *= 8", lambda x, y: x.__imul__(8), ((a * 8) & 0xFFFFFFFF, b)),
            ("a *= 12", lambda x, y: x.__imul__(12), ((a * 12) & 0xFFFFFFFF, b)),
            ("b = a << b", lambda x, y: y.set(x << y), (a, 0)),
            ("b = a >> b", lambda x, y: y.set(x >> y), (a, 0)),
            ("a <<= b", lambda x, y: x.__ilshift__(y), ((a << b) & 0xFFFFFFFF, b)),
            ("a >>= b", lambda x, y: x.__irshift__(y), (a >> b, b)),
        ]
        # ``b = a << b`` / ``b = a >> b`` make rhs the destination too, so the
        # lowering must stage the result in a temporary and copy it back. The
        # large ``b`` value saturates both dynamic shifts to zero.
        captures: list[tuple[str, int, int, int, int]] = []

        def build():
            for label, mutate, (expected_a, expected_b) in cases:
                x = Var(a)
                y = Var(b)
                mutate(x, y)
                captures.append((label, x.id, y.id, expected_a, expected_b))

        values = run_sim(build_module(build).to_json(), 1)["var_values"]
        for label, aid, bid, expected_a, expected_b in captures:
            with self.subTest(case=label):
                self.assertEqual(values[aid], expected_a & 0xFFFFFFFF)
                self.assertEqual(values[bid], expected_b & 0xFFFFFFFF)

    def test_self_shift_with_small_amount_is_nonzero(self):
        # A small value makes ``a <<= a`` a real, non-saturated dynamic shift.
        value = 5
        captured: dict[str, int] = {}

        def build():
            a = Var(value)
            a <<= a
            captured["a"] = a.id

        values = run_sim(build_module(build).to_json(), 1)["var_values"]
        self.assertEqual(values[captured["a"]], (value << value) & 0xFFFFFFFF)


class LoopReexecution(unittest.TestCase):
    """The same lowering works when the loop body runs on later frames."""

    def _run_loop(self, mutate, initial, limit=2, loops=2):
        captured: dict[str, int] = {}

        def build():
            value = Var(initial)
            counter = Var(limit)
            with while_loop(counter > 0):
                mutate(value, counter)
                counter -= 1
            captured["value"] = value.id

        return run_sim(build_module(build).to_json(), loops)["var_values"][
            captured["value"]
        ]

    def test_while_loop_reexecutes_shift_and_bitwise_bodies(self):
        cases = [
            ("shl const", lambda v, c: v.__ilshift__(1), 0xC0, (0xC0 << 1 << 1) & 0xFFFFFFFF),
            ("shr const", lambda v, c: v.__irshift__(1), 0xC0, 0xC0 >> 1 >> 1),
            ("shl dynamic", lambda v, c: v.__ilshift__(c), 0xC0, (0xC0 << 2 << 1) & 0xFFFFFFFF),
            ("shr dynamic", lambda v, c: v.__irshift__(c), 0xC0, 0xC0 >> 2 >> 1),
            ("and", lambda v, c: v.__iand__(c), 0xC0, 0),
            ("or", lambda v, c: v.__ior__(c), 0xC0, 0xC0 | 2 | 1),
            ("xor", lambda v, c: v.__ixor__(c), 0xC0, 0xC0 ^ 2 ^ 1),
            ("mul 8", lambda v, c: v.__imul__(8), 3, 3 * 8 * 8),
            ("mul 12", lambda v, c: v.__imul__(12), 3, (3 * 12 * 12) & 0xFFFFFFFF),
            ("mul -1", lambda v, c: v.__imul__(0xFFFFFFFF), 3, 3),
        ]
        for label, mutate, initial, want in cases:
            with self.subTest(case=label):
                self.assertEqual(
                    self._run_loop(mutate, initial),
                    want & 0xFFFFFFFF,
                )

    def test_while_loop_reexecutes_bitnot_and_neg(self):
        cases = [
            ("bitnot", lambda v, c: v.set(~v), 0xC0, 0xC0),
            ("neg", lambda v, c: v.set(-v), 0xC0, 0xC0),
        ]
        for label, mutate, initial, want in cases:
            with self.subTest(case=label):
                self.assertEqual(
                    self._run_loop(mutate, initial),
                    want & 0xFFFFFFFF,
                )


class ReexecutionAcrossFrames(unittest.TestCase):
    """Dynamic shifts and multi-store bodies see the original input each pass."""

    def test_dynamic_shift_reads_the_amount_when_the_store_was_built(self):
        captured: dict[str, int] = {}

        def build():
            src = Var(0xDEADBEEF)
            amount = Var(1)
            r1 = Var(0)
            r2 = Var(0)
            r3 = Var(0)
            # The amount changes between sites; each shift must keep the
            # amount value that was current when its store was built.
            r1.set(src << amount)
            amount.set(4)
            r2.set(src << amount)
            amount.set(33)
            r3.set(src << amount)
            captured.update(r1=r1.id, r2=r2.id, r3=r3.id)

        values = run_sim(build_module(build).to_json(), 1)["var_values"]
        self.assertEqual(values[captured["r1"]], (0xDEADBEEF << 1) & 0xFFFFFFFF)
        self.assertEqual(values[captured["r2"]], (0xDEADBEEF << 4) & 0xFFFFFFFF)
        self.assertEqual(values[captured["r3"]], 0)

    def test_optimised_stores_are_idempotent_across_frames(self):
        captured: dict[str, int] = {}

        def build():
            frame = Var(0)
            frame.iadd(1)
            captured["frame"] = frame.id

            # Source and amount never change, so each result must be
            # reproduced on every trigger loop rather than accumulate.
            src = Var(0xDEADBEEF)
            amount = Var(3)
            shifted = Var(0)
            shifted.set(src << amount)
            captured["shifted"] = shifted.id
            shifted_right = Var(0)
            shifted_right.set(src >> amount)
            captured["shifted_right"] = shifted_right.id

            growing = Var(0xDEADBEEF)
            growing.set(growing << 2)
            captured["growing"] = growing.id

            times_twelve = Var(5)
            times_twelve.set(times_twelve * 12)
            captured["times_twelve"] = times_twelve.id

        mod = build_module(build)
        loop1 = run_sim(mod.to_json(), 1)
        self.assertEqual(loop1["var_values"][captured["frame"]], 1)
        self.assertEqual(
            loop1["var_values"][captured["shifted"]], (0xDEADBEEF << 3) & 0xFFFFFFFF
        )
        self.assertEqual(loop1["var_values"][captured["shifted_right"]], 0xDEADBEEF >> 3)
        self.assertEqual(
            loop1["var_values"][captured["growing"]], (0xDEADBEEF << 2) & 0xFFFFFFFF
        )
        self.assertEqual(loop1["var_values"][captured["times_twelve"]], 5 * 12)

        loop2 = run_sim(mod.to_json(), 2)
        self.assertEqual(loop2["var_values"][captured["frame"]], 2)
        self.assertEqual(
            loop2["var_values"][captured["shifted"]], (0xDEADBEEF << 3) & 0xFFFFFFFF
        )
        self.assertEqual(loop2["var_values"][captured["shifted_right"]], 0xDEADBEEF >> 3)
        self.assertEqual(
            loop2["var_values"][captured["growing"]], (0xDEADBEEF << 4) & 0xFFFFFFFF
        )
        self.assertEqual(loop2["var_values"][captured["times_twelve"]], 5 * 12 * 12)
        self.assertTrue(loop2["self_targeted_invariant_holds"])


class InPlaceOperatorReturns(unittest.TestCase):
    """``^=`` / ``>>=`` / ``<<=`` return the same Var and re-execute."""

    def test_returns_self_and_works_across_frames(self):
        captured: dict[str, int] = {}

        def build():
            rhs = Var(2)
            x = Var(0xDEADBEEF)
            s = Var(0xDEADBEEF)
            l = Var(0xDEADBEEF)
            self.assertIs(x.__ixor__(rhs), x)
            self.assertIs(s.__irshift__(rhs), s)
            self.assertIs(l.__ilshift__(rhs), l)
            captured.update(x=x.id, s=s.id, l=l.id)

        values = run_sim(build_module(build).to_json(), 2)["var_values"]
        self.assertEqual(values[captured["x"]], 0xDEADBEEF)
        self.assertEqual(values[captured["s"]], 0xDEADBEEF >> 2 >> 2)
        self.assertEqual(
            values[captured["l"]], (0xDEADBEEF << 2 << 2) & 0xFFFFFFFF
        )


def _masked_cases(value: int):
    """Operation matrix parameterised by the value-space input."""
    rhs_and = 0xF0F0F0F0
    rhs_or = 0xFF0000FF
    rhs_xor = 0xA5A5A5A5
    return [
        ("bitnot", lambda v, w: v.set(~v), (~value) & 0xFFFFFFFF, 0),
        ("neg", lambda v, w: v.set(-v), (-value) & 0xFFFFFFFF, 0),
        ("const_and", lambda v, w: v.set(v & 0xF0F0F0F0), value & 0xF0F0F0F0, 0),
        ("const_or", lambda v, w: v.set(v | 0xFF0000FF), value | 0xFF0000FF, 0),
        ("const_xor", lambda v, w: v.set(v ^ 0xA5A5A5A5), value ^ 0xA5A5A5A5, 0),
        ("const_shl", lambda v, w: v.set(v << 4), (value << 4) & 0xFFFFFFFF, 0),
        ("const_shr", lambda v, w: v.set(v >> 4), value >> 4, 0),
        ("var_and", lambda v, w: v.set(v & w), value & rhs_and, rhs_and),
        ("var_or", lambda v, w: v.set(v | w), value | rhs_or, rhs_or),
        ("var_xor", lambda v, w: v.set(v ^ w), value ^ rhs_xor, rhs_xor),
        ("dynamic_shl", lambda v, w: v.set(v << w), (value << 4) & 0xFFFFFFFF, 4),
        ("dynamic_shr", lambda v, w: v.set(v >> w), value >> 4, 4),
    ]


class MaskedVarKernels(unittest.TestCase):
    """Every new kernel honours a masked Var's value space (ADR 0018)."""

    def test_explicit_mask_preserves_dirty_neighbour_bits(self):
        dirty_outside = DIRTY_RAW & ~MASK & 0xFFFFFFFF
        for name, mutate, expected, rhs in _masked_cases(MASKED_VALUE):
            captured: dict[str, int] = {}

            def build(mutate=mutate, rhs=rhs, captured=captured):
                v = Var(0, explicit_mask=MASK)
                alias = Var(0, explicit_id=v.id)
                alias.set(DIRTY_RAW)
                w = Var(rhs)
                mutate(v, w)
                captured["v"] = v.id

            report = run_sim(build_module(build).to_json(), 1)
            want = dirty_outside | (expected & MASK)
            with self.subTest(op=name):
                self.assertTrue(report["self_targeted_invariant_holds"])
                self.assertEqual(report["var_values"][captured["v"]], want)

    def test_adjacent_trigger_struct_fields_preserve_neighbours(self):
        lo_value = LO_RAW & LO_MASK
        lo_outside = LO_RAW & ~LO_MASK & 0xFFFFFFFF
        for name, mutate, expected, rhs in _masked_cases(lo_value):
            captured: dict[str, int] = {}

            def build(mutate=mutate, rhs=rhs, captured=captured):
                fields = MaskedFields(lo=0x12, hi=0x3400, sink=0xDEADBEEF)
                lo_alias = Var(0, explicit_id=fields.lo.id)
                hi_alias = Var(0, explicit_id=fields.hi.id)
                lo_alias.set(LO_RAW)
                hi_alias.set(HI_RAW)
                w = Var(rhs)
                mutate(fields.lo, w)
                captured.update(lo=fields.lo.id, hi=fields.hi.id, sink=fields.sink.id)

            report = run_sim(build_module(build).to_json(), 1)
            values = report["var_values"]
            want_lo = lo_outside | (expected & LO_MASK)
            with self.subTest(op=name):
                self.assertTrue(report["self_targeted_invariant_holds"])
                self.assertEqual(values[captured["lo"]], want_lo)
                self.assertEqual(values[captured["hi"]], HI_RAW)
                self.assertEqual(values[captured["sink"]], 0xDEADBEEF)


class CostPins(unittest.TestCase):
    """Static and executed trigger budgets for the optimized kernels."""

    def _report(self, build):
        return run_sim(build_module(build).to_json(), 1)

    def test_constant_shifts_stay_within_two_triggers(self):
        for op in ("shl", "shr"):
            def build(op=op):
                a = Var(0xDEADBEEF)
                if op == "shl":
                    a <<= 1
                else:
                    a >>= 1

            report = self._report(build)
            with self.subTest(op=op):
                self.assertLessEqual(report["static_cost"]["triggers"], 2)
                self.assertLessEqual(report["executed_cost"]["triggers"], 2)

    def test_dynamic_shifts_stay_within_ten_triggers(self):
        # ``b = 15`` sets four of the five shift gates. The five gate triggers
        # are condition scaffolding shared by the whole dynamic-shift block;
        # the shift kernel adds at most one trigger per bit (five) plus the
        # 1T ``>= 32`` guard, so the kernel delta stays within the 10T budget.
        def scaffold():
            a = Var(0xDEADBEEF)
            b = Var(15)
            for k in range(5):
                # One live action keeps each calibration gate: a 0-act `when`
                # is now a dead gate the optimizer drops.
                when(
                    DeathsX(b.value, AtLeast, 1, 0, 1 << k),
                    lambda: a.iadd(1),
                )
            return a, b

        base_static = self._report(scaffold)["static_cost"]["triggers"]

        for op in ("shl", "shr"):
            def build(op=op):
                a = Var(0xDEADBEEF)
                b = Var(15)
                if op == "shl":
                    a <<= b
                else:
                    a >>= b

            report = self._report(build)
            with self.subTest(op=op):
                self.assertLessEqual(report["executed_cost"]["triggers"], 10)
                # 5 gate triggers + 5 shift-body triggers + 1 zero guard;
                # subtract the gates to pin the shift kernel itself.
                self.assertLessEqual(
                    report["static_cost"]["triggers"] - base_static, 10
                )

    def test_var_var_bitwise_stay_within_ten_triggers(self):
        for op in ("bitand", "bitor", "bitxor"):
            def build(op=op):
                a = Var(0xDEADBEEF)
                b = Var(0x0F0F0F0F)
                out = Var(0)
                if op == "bitand":
                    out.set(a & b)
                elif op == "bitor":
                    out.set(a | b)
                else:
                    out.set(a ^ b)

            report = self._report(build)
            with self.subTest(op=op):
                self.assertLessEqual(report["static_cost"]["triggers"], 10)
                self.assertLessEqual(report["executed_cost"]["triggers"], 10)

    def test_one_site_costs_match_ts_pins(self):
        # Same three-var probe shape and exact counts as the TS parity suite.
        # Unlike the old width-dependent ladders, these numbers do not move
        # with the operand, so a reintroduced ladder or temporary is loud.
        cases = [
            ("constant shl by 1", lambda a, b, out: out.set(a << 1), 3, 3),
            ("constant shr by 1", lambda a, b, out: out.set(a >> 1), 3, 3),
            ("dynamic shl", lambda a, b, out: out.set(a << b), 14, 11),
            ("dynamic shr", lambda a, b, out: out.set(a >> b), 14, 11),
            ("var & var", lambda a, b, out: out.set(a & b), 4, 4),
            ("var | var", lambda a, b, out: out.set(a | b), 4, 4),
            ("var ^ var", lambda a, b, out: out.set(a ^ b), 5, 6),
            ("bitnot", lambda a, b, out: out.set(~a), 3, 3),
            ("neg", lambda a, b, out: out.set(-a), 3, 3),
            ("mul by 0xFFFFFFFF", lambda a, b, out: out.set(a * 0xFFFFFFFF), 3, 3),
            ("mul by power of two", lambda a, b, out: out.set(a * 8), 3, 3),
            ("mul by popcount-2 constant", lambda a, b, out: out.set(a * 12), 6, 8),
            ("mul by 7", lambda a, b, out: out.set(a * 7), 7, 9),
            ("mul by 0xFF000000", lambda a, b, out: out.set(a * 0xFF000000), 9, 9),
        ]
        for name, store, static_triggers, executed_triggers in cases:
            def build(store=store):
                a = Var(0xDEADBEEF)
                b = Var(3)
                out = Var(0)
                store(a, b, out)

            report = self._report(build)
            with self.subTest(case=name):
                self.assertEqual(
                    report["static_cost"]["triggers"], static_triggers
                )
                self.assertEqual(
                    report["executed_cost"]["triggers"], executed_triggers
                )


_PARITY_TS_SCRIPT = textwrap.dedent(
    """
    import { Var, buildModule } from "./ts/index.ts";

    const mod = buildModule(() => {
      const a = Var(0xdeadbeef);
      const b = Var(0x0f0f0f0f);
      const out = Var(0);
      out.set(a.bitNot());
      out.set(a.neg());
      out.set(a.bitAnd(0xffff0000));
      out.set(a.bitOr(0xffff0000));
      out.set(a.bitXor(0xffff0000));
      out.set(a.bitAnd(b));
      out.set(a.bitOr(b));
      out.set(a.bitXor(b));
      out.set(a.shl(3));
      out.set(a.shr(3));
      out.set(a.shl(b));
      out.set(a.shr(b));
      for (const c of [0, 1, 0x80000000, 3, 5, 0x80000001, 12, 0xffffffff, 7, 0xff000000]) {
        out.set(a.mul(c));
      }
    });

    process.stdout.write(mod.toJson());
    """
)


class PyTsHirParity(unittest.TestCase):
    def test_cycle1_kernels_are_byte_identical(self):
        def prog() -> None:
            a = Var(0xDEADBEEF)
            b = Var(0x0F0F0F0F)
            out = Var(0)
            out.set(~a)
            out.set(-a)
            out.set(a & 0xFFFF0000)
            out.set(a | 0xFFFF0000)
            out.set(a ^ 0xFFFF0000)
            out.set(a & b)
            out.set(a | b)
            out.set(a ^ b)
            out.set(a << 3)
            out.set(a >> 3)
            out.set(a << b)
            out.set(a >> b)
            for c in (0, 1, 0x80000000, 3, 5, 0x80000001, 12, 0xFFFFFFFF, 7, 0xFF000000):
                out.set(a * c)

        py_hir = build_module(prog).to_json()
        result = subprocess.run(
            ["node", "--input-type=module", "-e", _PARITY_TS_SCRIPT],
            cwd=str(REPO_ROOT),
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(json.loads(py_hir), json.loads(result.stdout))
        self.assertEqual(py_hir, result.stdout)


if __name__ == "__main__":
    unittest.main()
