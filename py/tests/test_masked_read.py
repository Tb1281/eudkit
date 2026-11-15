"""Ticket #20: field `read()` honours the field's `bitMask`.

`read()` used to emit one bit test per bit of the *nominal width window*
(`size * 8` bits from `shift`), ignoring a custom `bitMask`. A masked read then
returned the raw window shifted instead of `(raw & bitMask) >> shift` — a silent
wrong answer whenever the mask does not cover that window, and one trigger per
width bit instead of one per mask bit. These tests pin the mask-aware read on
both field surfaces (`MemFieldHandle` and `TriggerFieldRef`), the resulting
12T cost for a masked dword read, agreement with `eq`, and the rejection of a
mask with set bits below `shift`.
"""

from __future__ import annotations

import hashlib
import subprocess
import textwrap
import unittest

from py import (
    CUnit,
    EudslError,
    FieldSpec,
    SetDeaths,
    SetTo,
    TriggerFieldRef,
    Var,
    build_module,
    defineMemView,
    when,
)
from py.tests.helpers import REPO_ROOT, run_sim
from py.view import make_memview

# `v` sits at byte 0 (shift 0); the 0xFF00 mask covers the second byte only.
_MASKED_BYTE_SCHEMA = defineMemView(
    "MaskedByte", 4, {"v": FieldSpec(0, 1, mask=0xFF00)}
)
# A dword field with a narrow mask: the window has 32 bits, the mask 8.
_MASKED_DWORD_SCHEMA = defineMemView(
    "MaskedDw", 4, {"v": FieldSpec(0, 4, mask=0xFF00)}
)
# `shift = 16`, mask bits (0x00FF) live below it: unrepresentable in field units.
_LOW_MASK_SCHEMA = defineMemView(
    "LowMask", 4, {"v": FieldSpec(2, 2, mask=0x00FF)}
)

# Byte-field rows measured on HEAD ee2ae1c; the second column is the expected
# `(raw & bitMask) >> shift`.
_BYTE_ROWS = (
    (0x00001200, 0x00001200),
    (0x00001201, 0x00001200),
    (0x00000000, 0x00000000),
    (0x0000FF00, 0x0000FF00),
    (0x0000FF12, 0x0000FF00),
)

# Dword-field rows: the pre-fix width window returned the low bits too.
_DWORD_ROWS = (
    (0x00001200, 0x00001200),
    (0x00001201, 0x00001200),
    (0x0000FF12, 0x0000FF00),
)


def _read_masked(schema, raw: int) -> int:
    """Write `raw` at EPD 3000, read the single field into a `Var`, return it."""
    ids: dict[str, int] = {}

    def prog() -> None:
        SetDeaths(3000, SetTo, raw, 0)
        view = make_memview(schema, 3000)
        out = Var(0)
        ids["out"] = out.id
        out.set(view.v.read())

    report = run_sim(build_module(prog).to_json(), 1)
    return report["var_values"][ids["out"]]


def _masked_dword_read_prog() -> None:
    SetDeaths(3000, SetTo, 0x1201, 0)
    view = make_memview(_MASKED_DWORD_SCHEMA, 3000)
    out = Var(0)
    out.set(view.v.read())


class MaskedReadValueTest(unittest.TestCase):
    def test_masked_byte_read_returns_mask_units(self) -> None:
        for raw, want in _BYTE_ROWS:
            with self.subTest(raw=hex(raw)):
                self.assertEqual(_read_masked(_MASKED_BYTE_SCHEMA, raw), want)

    def test_masked_dword_read_returns_mask_units(self) -> None:
        for raw, want in _DWORD_ROWS:
            with self.subTest(raw=hex(raw)):
                self.assertEqual(_read_masked(_MASKED_DWORD_SCHEMA, raw), want)

    def test_read_agrees_with_eq_in_mask_units(self) -> None:
        # raw 0x1201 differs from the masked value only outside the mask.
        ids: dict[str, int] = {}

        def prog() -> None:
            SetDeaths(3000, SetTo, 0x1201, 0)
            view = make_memview(_MASKED_BYTE_SCHEMA, 3000)
            out = Var(0)
            ids["out"] = out.id
            out.set(view.v.read())
            hit = Var(0)
            ids["hit"] = hit.id
            miss = Var(0)
            ids["miss"] = miss.id
            when(view.v.eq(0x1200), lambda: hit.iadd(1))
            when(view.v.eq(0x1201), lambda: miss.iadd(1))

        report = run_sim(build_module(prog).to_json(), 1)
        self.assertEqual(report["var_values"][ids["out"]], 0x1200)
        self.assertEqual(report["var_values"][ids["hit"]], 1)
        self.assertEqual(report["var_values"][ids["miss"]], 0)


class MaskedReadCostTest(unittest.TestCase):
    def test_masked_dword_read_costs_12t(self) -> None:
        report = run_sim(build_module(_masked_dword_read_prog).to_json(), 1)
        static = report["static_cost"]
        self.assertEqual(static["triggers"], 12)
        self.assertEqual(static["conditions"], 8)
        self.assertEqual(static["actions"], 77)
        self.assertEqual(static["trigger_bytes"], 28_896)
        self.assertEqual(static["total_bytes"], 28_900)
        self.assertEqual(report["executed_cost"]["triggers"], 12)


class TriggerFieldRefMaskedReadTest(unittest.TestCase):
    def _read_ref(self, size: int) -> tuple[int, int]:
        ids: dict[str, int] = {}

        def prog() -> None:
            value = Var(0x1234)
            out = Var(0)
            ids["out"] = out.id
            ref = TriggerFieldRef(-1, 20, size, 0, value.id, 0xFF00)
            out.set(ref.read())

        report = run_sim(build_module(prog).to_json(), 1)
        return report["var_values"][ids["out"]], report["static_cost"]["triggers"]

    def test_trigger_field_ref_masked_read_uses_custom_mask(self) -> None:
        for size in (4, 1):
            with self.subTest(size=size):
                value, triggers = self._read_ref(size)
                self.assertEqual(value, 0x1200)
                self.assertEqual(triggers, 12)


class MaskedReadBoundsTest(unittest.TestCase):
    def test_masked_read_rejects_mask_bits_below_shift(self) -> None:
        def prog() -> None:
            view = make_memview(_LOW_MASK_SCHEMA, 3000)
            out = Var(0)
            out.set(view.v.read())

        with self.assertRaises(EudslError) as ctx:
            build_module(prog)
        self.assertEqual(ctx.exception.code, "EUDSL-BOUNDS")


class MaskedReadDefaultPathTest(unittest.TestCase):
    def test_default_mask_read_hir_is_byte_identical(self) -> None:
        # The cost-gate probe; `mask=None` must stay the frozen-golden path.
        # #97 re-blessed the hash twice: a full-dword `mask=None` read now
        # emits one reader subroutine per distinct EPD (32 literal-`player`
        # bit tests plus the accumulator reset) and the call site just copies
        # the result. The *masked* partial-width paths below keep the inline
        # reader and are unchanged.
        def prog() -> None:
            out = Var(0)
            out.set(CUnit(1000).hp.read())

        hir = build_module(prog).to_json()
        self.assertEqual(
            hashlib.sha256(hir.encode()).hexdigest(),
            "72b36d7ddbefabfb72ec7d51ec03fb2e921ffeda790a1005e92efc0a3a405e8c",
        )


class MaskedReadParityTest(unittest.TestCase):
    def test_py_ts_hir_parity_for_every_new_case(self) -> None:
        py_hir = _build_masked_read_parity_module().to_json()
        res = subprocess.run(
            ["node", "--input-type=module", "-e", _TS_MASKED_READ_PARITY_SCRIPT],
            cwd=str(REPO_ROOT),
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(py_hir, res.stdout)


def _build_masked_read_parity_module():
    def prog() -> None:
        SetDeaths(3000, SetTo, 0x1201, 0)

        byte_view = make_memview(_MASKED_BYTE_SCHEMA, 3000)
        byte_out = Var(0)
        byte_out.set(byte_view.v.read())

        dword_view = make_memview(_MASKED_DWORD_SCHEMA, 3000)
        dword_out = Var(0)
        dword_out.set(dword_view.v.read())

        value = Var(0x1234)
        ref = TriggerFieldRef(-1, 20, 4, 0, value.id, 0xFF00)
        ref_out = Var(0)
        ref_out.set(ref.read())
        narrow = TriggerFieldRef(-1, 20, 1, 0, value.id, 0xFF00)
        narrow_out = Var(0)
        narrow_out.set(narrow.read())

        hit = Var(0)
        miss = Var(0)
        when(byte_view.v.eq(0x1200), lambda: hit.iadd(1))
        when(byte_view.v.eq(0x1201), lambda: miss.iadd(1))

    return build_module(prog)


_TS_MASKED_READ_PARITY_SCRIPT = textwrap.dedent(
    """
    import {
      MemView,
      SetDeaths,
      SetTo,
      TriggerFieldRef,
      Var,
      buildModule,
      defineMemView,
      when,
    } from "./ts/index.ts";

    const maskedByte = defineMemView("MaskedByte", 4, {
      v: { offset: 0, size: 1, mask: 0xff00 },
    });
    const maskedDw = defineMemView("MaskedDw", 4, {
      v: { offset: 0, size: 4, mask: 0xff00 },
    });

    const mod = buildModule(() => {
      SetDeaths(3000, SetTo, 0x1201, 0);

      const byteView = MemView(maskedByte, 3000);
      const byteOut = Var(0);
      byteOut.set(byteView.v.read());

      const dwordView = MemView(maskedDw, 3000);
      const dwordOut = Var(0);
      dwordOut.set(dwordView.v.read());

      const value = Var(0x1234);
      const ref = new TriggerFieldRef(-1, 20, 4, 0, value.id, 0xff00);
      const refOut = Var(0);
      refOut.set(ref.read());
      const narrow = new TriggerFieldRef(-1, 20, 1, 0, value.id, 0xff00);
      const narrowOut = Var(0);
      narrowOut.set(narrow.read());

      const hit = Var(0);
      const miss = Var(0);
      when(byteView.v.eq(0x1200), () => hit.iadd(1));
      when(byteView.v.eq(0x1201), () => miss.iadd(1));
    });

    process.stdout.write(mod.toJson());
    """
)


if __name__ == "__main__":
    unittest.main()
