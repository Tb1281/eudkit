"""Ticket #32: masked `Var` comparisons follow the field value-space rule.

ADR 0017 settled one rule for both field surfaces: a comparison against a
constant is asked in the *field's value space* (shift 0 here):

* a constant outside the mask is impossible to represent, so equality is
  `Never` and inequality is `Always`; an ordering threshold beyond the mask
  is likewise folded;
* a constant inside the mask is compared **un-masked** — the simulator masks
  the left-hand raw value and compares against the right-hand amount as-is.

The `Var` comparison bodies used to mask **out-of-space** constants into the
space instead of folding them, and to **re-mask in-space** constants before
emitting them — both silent wrong answers about memory. Every behavioural test
below fails on `61cf4fc` and passes with the fix. Test 4 is a no-move guard: it
passes on both sides by design (like `test_ordering_representable_hir_sha_unchanged`).
"""

from __future__ import annotations

import hashlib
import json
import operator
import unittest
from typing import Any

from py import (
    FieldSpec,
    SetDeaths,
    SetTo,
    TriggerStruct,
    Var,
    bit,
    build_module,
    byte,
    defineMemView,
    masked,
    when,
    word,
)
from py.tests.helpers import run_sim, run_static
from py.view import make_memview

_OPS = ("eq", "ne", "ge", "le", "gt", "lt")
_OP_FNS = {
    "eq": operator.eq,
    "ne": operator.ne,
    "ge": operator.ge,
    "le": operator.le,
    "gt": operator.gt,
    "lt": operator.lt,
}


def _expected(raw: int, mask: int, op: str, const: int) -> int:
    """`(raw & mask) op (const & 0xFFFFFFFF)` in plain integer semantics."""
    return int(_OP_FNS[op](raw & mask, const & 0xFFFFFFFF))


# ---------------------------------------------------------------------------
# Test 1 — 1,248-row truth table. Every raw is a bit-subset of its mask, so
# `v.set(raw)` seeds exactly the intended value: 26 pairs x 6 ops x 8 consts.
# ---------------------------------------------------------------------------

_MASKS_RAWS = {
    0xFF: [0, 1, 0x7F, 0xFF],
    0xFFFF: [0, 1, 0x80, 0xFF, 0xFFFF],
    0xFFFFFFFF: [0, 1, 0xFF, 0x100, 0x7FFFFFFF, 0xFFFFFFFF],
    0x0F0F: [0, 0x000F, 0x0F00, 0x0F0F, 0x0102],
    0x0F00: [0, 0x0100, 0x0F00],
    0xE0: [0, 0x20, 0xE0],
}
_CONSTS = (0, 1, 0xFF, 0x100, 0xFFFF, 0x10000, 0xFFFFFFFF, -1)


class MaskedVarTruthTableTest(unittest.TestCase):
    def test_truth_table_1248_rows(self) -> None:
        rows = 0
        for mask, raws in _MASKS_RAWS.items():
            for raw in raws:
                ids: dict[tuple[str, int], int] = {}

                def prog(
                    mask: int = mask, raw: int = raw, ids: dict = ids
                ) -> None:
                    v = Var(0, explicit_mask=mask)
                    v.set(raw)
                    for op in _OPS:
                        for const in _CONSTS:
                            hit = Var(0)
                            ids[(op, const)] = hit.id
                            when(
                                [getattr(v, op)(const)],
                                lambda hit=hit: hit.iadd(1),
                            )

                report = run_sim(build_module(prog).to_json(), 1)
                for op in _OPS:
                    for const in _CONSTS:
                        rows += 1
                        with self.subTest(
                            mask=hex(mask), raw=hex(raw), op=op, const=const
                        ):
                            self.assertEqual(
                                report["var_values"][ids[(op, const)]],
                                _expected(raw, mask, op, const),
                            )
        self.assertEqual(rows, 1248)


# ---------------------------------------------------------------------------
# Test 2 — cross-surface behavioural equivalence against the shipped field
# rule: 29 (mask, raw) pairs x 48 = 1,392 rows. Anti-drift oracle.
# ---------------------------------------------------------------------------

_MASKS = (0xFF, 0xFFFF, 0xFFFFFFFF, 0x0F0F, 0x0F00, 0xE0)
_RAWS = (0, 1, 0x7F, 0xFF, 0x100, 0x1234, 0x0F0F, 0xE0)

_REF_SCHEMAS = {
    mask: defineMemView(
        f"VarSpaceRef_{mask:08X}", 4, {"f": FieldSpec(0, 4, mask=mask)}
    )
    for mask in _MASKS
}


def _subset_pairs() -> list[tuple[int, int]]:
    return [
        (mask, raw)
        for mask in _MASKS
        for raw in _RAWS
        if raw & ~mask == 0
    ]


class MaskedVarCrossSurfaceTest(unittest.TestCase):
    def test_cross_surface_matches_field_rule(self) -> None:
        rows = 0
        for mask, raw in _subset_pairs():
            ids: dict[str, dict[tuple[str, int], int]] = {"var": {}, "ref": {}}

            def prog(mask: int = mask, raw: int = raw, ids: dict = ids) -> None:
                SetDeaths(3000, SetTo, raw, 0)
                v = Var(0, explicit_mask=mask)
                v.set(raw)
                ref = make_memview(_REF_SCHEMAS[mask], 3000).f
                for op in _OPS:
                    for const in _CONSTS:
                        key = (op, const)
                        hit = Var(0)
                        ids["var"][key] = hit.id
                        when([getattr(v, op)(const)], lambda hit=hit: hit.iadd(1))
                        ref_hit = Var(0)
                        ids["ref"][key] = ref_hit.id
                        when(
                            [getattr(ref, op)(const)],
                            lambda ref_hit=ref_hit: ref_hit.iadd(1),
                        )

            report = run_sim(build_module(prog).to_json(), 1)
            for op in _OPS:
                for const in _CONSTS:
                    rows += 1
                    key = (op, const)
                    with self.subTest(
                        mask=hex(mask), raw=hex(raw), op=op, const=const
                    ):
                        got = report["var_values"][ids["var"][key]]
                        ref = report["var_values"][ids["ref"][key]]
                        self.assertEqual(got, ref)
                        self.assertEqual(ref, _expected(raw, mask, op, const))
        self.assertEqual(rows, 1392)


# ---------------------------------------------------------------------------
# Test 3 — public `TriggerStruct` surface: 4 fields x 3 raws x 24 = 288 rows.
# ---------------------------------------------------------------------------

_BITS = TriggerStruct(
    "ValueSpaceBits",
    {
        "f": bit(3, 5),
        "b1": byte(1),
        "w1": word(1),
        "m": masked(0x0F00),
    },
)

_STRUCT_ROWS = {
    "f": (0xE0, (0x00, 0x80, 0xE0)),
    "b1": (0x1FE, (0x00, 0x80, 0x1FE)),
    "w1": (0x1FFFE, (0x00, 0x80, 0x1FFFE)),
    "m": (0x0F00, (0x00, 0x0100, 0x0F00)),
}
_STRUCT_CONSTS = (0, 1, 0x80, 0x8000)


class MaskedVarTriggerStructTest(unittest.TestCase):
    def test_public_trigger_struct_rows(self) -> None:
        rows = 0
        for field, (mask, raws) in _STRUCT_ROWS.items():
            for raw in raws:
                ids: dict[tuple[str, int], int] = {}

                def prog(
                    field: str = field, raw: int = raw, ids: dict = ids
                ) -> None:
                    s = _BITS(**{field: raw})
                    target = getattr(s, field)
                    target.set(raw)
                    for op in _OPS:
                        for const in _STRUCT_CONSTS:
                            hit = Var(0)
                            ids[(op, const)] = hit.id
                            when(
                                [getattr(target, op)(const)],
                                lambda hit=hit: hit.iadd(1),
                            )

                report = run_sim(build_module(prog).to_json(), 1)
                for op in _OPS:
                    for const in _STRUCT_CONSTS:
                        rows += 1
                        with self.subTest(
                            field=field, raw=hex(raw), op=op, const=const
                        ):
                            self.assertEqual(
                                report["var_values"][ids[(op, const)]],
                                _expected(raw, mask, op, const),
                            )
        self.assertEqual(rows, 288)


# ---------------------------------------------------------------------------
# Test 4 — full-mask HIR must not move (no-move guard; passes on both sides).
# Pin computed on unmodified 61cf4fc before editing `py/var.py`.
# ---------------------------------------------------------------------------


class FullMaskNoMoveTest(unittest.TestCase):
    def test_full_mask_hir_sha_unchanged(self) -> None:
        def prog() -> None:
            SetDeaths(3000, SetTo, 0x12345678, 0)
            v = Var(0)
            h = Var(0)
            for rhs in (0, 1, 0x7FFFFFFF, 0xFFFFFFFF, 0x100):
                for op in _OPS:
                    when([getattr(v, op)(rhs)], lambda: h.set(1))
            w = Var(0, explicit_mask=0xFFFFFFFF)
            for op in _OPS:
                when([getattr(w, op)(5)], lambda: h.set(1))

        hir = build_module(prog).to_json()
        self.assertEqual(len(hir), 17531)
        self.assertEqual(len(json.loads(hir)["module"]["stmts"]), 45)
        self.assertEqual(
            hashlib.sha256(hir.encode()).hexdigest(),
            "0a162b9f9ee1f504b2529b54b615e2c7a47820c2da9d627e32585dccefbd2d60",
        )


# ---------------------------------------------------------------------------
# Test 5 — masked value-space HIR pin, shared with T3 (`ts/var.ts`).
# On 61cf4fc the same fixture is 10,533 chars / sha256 5037cfaf..., so this
# pin is load-bearing. Do not re-pin it to whatever the local build prints.
# (Re-pinned once for HIR 3.1 -> 3.2, ADR 0021: the `version` literal is the
# only byte that changed — the same fixture hashes to the old value with the
# version set back to 3.1.)
# ---------------------------------------------------------------------------

_HIR_CASES = [
    (0xFF, "eq", 0x100),
    (0xFF, "ne", 0x100),
    (0xFF, "ge", 0x100),
    (0xFF, "le", 0x100),
    (0xFF, "gt", 0x100),
    (0xFF, "lt", 0x100),
    (0xFF, "eq", 0xFF),
    (0xFF, "ne", 0xFF),
    (0xFF, "ge", 0x7F),
    (0xFF, "le", 0x7F),
    (0xFFFF, "eq", 0x10000),
    (0xFFFF, "ge", 0x10000),
    (0xFFFF, "lt", 0x10000),
    (0x0F0F, "eq", 0xFF),
    (0x0F0F, "ne", 0xFF),
    (0x0F0F, "ge", 0xFF),
    (0x0F0F, "lt", 0xFF),
    (0x0F0F, "gt", 0x80),
    (0x0F0F, "le", 0x80),
    (0xFFFFFFFF, "eq", 0x100),
    (0xFFFFFFFF, "gt", 0xFFFFFFFF),
]


class MaskedVarHirPinTest(unittest.TestCase):
    def test_masked_fixture_hir_sha(self) -> None:
        def prog() -> None:
            v = Var(0, explicit_mask=0xFF)
            v.set(0x7F)
            mid = Var(0, explicit_mask=0xFFFF)
            mid.set(0x1234)
            odd = Var(0, explicit_mask=0x0F0F)
            odd.set(0x0F00)
            full = Var(0)
            full.set(5)
            targets = {0xFF: v, 0xFFFF: mid, 0x0F0F: odd, 0xFFFFFFFF: full}
            for mask, op, rhs in _HIR_CASES:
                when([getattr(targets[mask], op)(rhs)], lambda: full.iadd(0))

        hir = build_module(prog).to_json()
        self.assertEqual(len(hir), 9443)
        self.assertEqual(len(json.loads(hir)["module"]["stmts"]), 22)
        self.assertEqual(
            hashlib.sha256(hir.encode()).hexdigest(),
            "68ae017240617bab2ad03425b48cf1721378deac2da0757e893dc956b3036789",
        )


# ---------------------------------------------------------------------------
# Test 6 — measured fold cost (pin the win).
# ---------------------------------------------------------------------------


def _ne_static_cost(rhs: int, mask: int = 0xFF) -> dict[str, Any]:
    def prog() -> None:
        v = Var(0, explicit_mask=mask)
        v.set(0)
        hit = Var(0)
        when([v.ne(rhs)], lambda: hit.iadd(1))

    return run_static(build_module(prog).to_json())["static_cost"]


class MaskedVarFoldCostTest(unittest.TestCase):
    def test_ne_fold_cost(self) -> None:
        # Out-of-space: 0x180 & 0xFF == 0x80 is a middle value, but 0x180
        # itself cannot be held by the 0xFF mask, so the comparison folds to
        # `Always` and the diff temp disappears (2T / 1C / 2A / 4,824 B).
        self.assertEqual(
            _ne_static_cost(0x180),
            {
                "triggers": 2,
                "conditions": 1,
                "actions": 2,
                "trigger_bytes": 4816,
                "blob_bytes": 8,
                "total_bytes": 4824,
            },
        )
        # In-space: 0x80 is representable, so the middle-constant diff temp is
        # still built — but it now accumulates `target & mask` (ticket #36),
        # which folds the 0xE0 mask into one act and drops the row from 74 to
        # 73 actions. On `28bbbdf` this row is 74A (so the test fails there).
        self.assertEqual(
            _ne_static_cost(0x80),
            {
                "triggers": 4,
                "conditions": 1,
                "actions": 73,
                "trigger_bytes": 9632,
                "blob_bytes": 8,
                "total_bytes": 9640,
            },
        )
        # Full-mask no-move guard (#36): a plain `Var` keeps the unchanged
        # `_temp(init); diff.iadd(target)` path, so its row is 72A on both
        # `28bbbdf` and the fix. Passes on both sides by design.
        self.assertEqual(
            _ne_static_cost(0x80, mask=0xFFFFFFFF),
            {
                "triggers": 4,
                "conditions": 1,
                "actions": 72,
                "trigger_bytes": 9632,
                "blob_bytes": 8,
                "total_bytes": 9640,
            },
        )


# ---------------------------------------------------------------------------
# Test 7 — dirty-raw `ne(middle)` reads the value space (ticket #36).
# A lane can hold bits outside the mask (an `explicit_id` alias writes the
# whole dword), so `Var.ne(c)` must mask the left operand before the diff.
# The four rows below are the issue's measured repro. On `28bbbdf` every
# `v.ne(c)` row returns 1 (it accumulated the raw lane); the `v.ne(c + 1)`
# positive control returns 1 on both sides, so only the first assert is red.
# ---------------------------------------------------------------------------

_DIRTY_NE_ROWS = (
    (0xE0, 0xA5, 0xA0),
    (0x0F0F, 0x1F00, 0x0F00),
    (0xFFFF, 0x12345, 0x2345),
    (0xFF00, 0xA512, 0xA500),
)


class DirtyRawNeValueSpaceTest(unittest.TestCase):
    def test_dirty_raw_ne_middle_uses_value_space(self) -> None:
        for mask, raw, const in _DIRTY_NE_ROWS:
            ids: dict[int, int] = {}

            def prog(mask: int = mask, raw: int = raw, const: int = const,
                     ids: dict = ids) -> None:
                v = Var(0, explicit_mask=mask)
                # Seed through a full-mask alias: `v.set(raw)` would mask the
                # raw lane, hiding the defect.
                alias = Var(0, explicit_id=v.id)
                alias.set(raw)
                for c in (const, const + 1):
                    hit = Var(0)
                    ids[c] = hit.id
                    when([v.ne(c)], lambda hit=hit: hit.iadd(1))

            report = run_sim(build_module(prog).to_json(), 1)
            with self.subTest(mask=hex(mask), raw=hex(raw), const=hex(const)):
                self.assertEqual(report["var_values"][ids[const]], 0)
                self.assertEqual(report["var_values"][ids[const + 1]], 1)


# ---------------------------------------------------------------------------
# Test 8 — six-op dirty-raw truth table (ticket #36). Every row seeds a dirty
# lane through the alias, so the expected answer is always
# `(raw & mask) op c` for a constant RHS. On `28bbbdf` this table has exactly
# one wrong column — `ne(middle)` — because the other five constant
# comparisons already emit `DeathsX(..., mask)`. The table is a regression
# guard for those five, not evidence that six ops were broken.
# ---------------------------------------------------------------------------

_DIRTY_TABLE_ROWS = (
    (0xE0, 0xA5),
    (0xE0, 0x1F),
    (0x0F0F, 0x1F00),
    (0x0F0F, 0xF0F0),
    (0xFFFF, 0x12345),
    (0xFFFF, 0xABCDE),
    (0xFF00, 0xA512),
    (0xFF00, 0x00FF),
    (0xFFFFFFFF, 0xA512),
    (0xFFFFFFFF, 0x00FF),
)
_DIRTY_TABLE_CONSTS = (0, 1, 0x20, 0x80, 0xA0, 0xE0, 0x0F00, 0x0F0F, 0x2345, 0xFFFF)


class DirtyRawTruthTableTest(unittest.TestCase):
    def test_six_ops_on_a_dirty_lane(self) -> None:
        rows = 0
        for mask, raw in _DIRTY_TABLE_ROWS:
            ids: dict[tuple[str, int], int] = {}

            def prog(mask: int = mask, raw: int = raw,
                     ids: dict = ids) -> None:
                v = Var(0, explicit_mask=mask)
                alias = Var(0, explicit_id=v.id)
                alias.set(raw)
                for op in _OPS:
                    for const in _DIRTY_TABLE_CONSTS:
                        hit = Var(0)
                        ids[(op, const)] = hit.id
                        when(
                            [getattr(v, op)(const)],
                            lambda hit=hit: hit.iadd(1),
                        )

            report = run_sim(build_module(prog).to_json(), 1)
            for op in _OPS:
                for const in _DIRTY_TABLE_CONSTS:
                    rows += 1
                    with self.subTest(
                        mask=hex(mask), raw=hex(raw), op=op, const=hex(const)
                    ):
                        self.assertEqual(
                            report["var_values"][ids[(op, const)]],
                            _expected(raw, mask, op, const),
                        )
        self.assertEqual(rows, 600)


# ---------------------------------------------------------------------------
# Test 9 — variable RHS is raw by decision (ticket #36). ADR 0018 §6: the
# variable-RHS comparison reads both lanes raw; the in-space fix is deferred.
# This test passes on `28bbbdf` too — it is a decision pin, not a fix test.
# ---------------------------------------------------------------------------

class VariableRhsRawDecisionTest(unittest.TestCase):
    def test_variable_rhs_reads_raw(self) -> None:
        ids: dict[str, int] = {}

        def prog() -> None:
            v = Var(0, explicit_mask=0xE0)
            alias = Var(0, explicit_id=v.id)
            alias.set(0x0F)  # value-space value 0x00, raw 0x0F
            w = Var(0)       # 0
            for op in _OPS:
                hit = Var(0)
                ids[op] = hit.id
                when([getattr(v, op)(w)], lambda hit=hit: hit.iadd(1))

        report = run_sim(build_module(prog).to_json(), 1)
        self.assertEqual(
            {op: report["var_values"][ids[op]] for op in _OPS},
            {"eq": 0, "ne": 1, "ge": 1, "le": 0, "gt": 1, "lt": 0},
        )


# ---------------------------------------------------------------------------
# Test 10 — masked-`ne` HIR pin (ticket #36), shared with T3 (`ts/var.ts`).
# The fixture mixes a dirty-lane middle `ne`, an in-space middle `ne`, `ne(0)`
# and a full-mask `ne` (the no-move guard in the same module). On `28bbbdf`
# the same fixture is 3,292 chars / sha256 64fd3231..., so this pin is
# load-bearing. Do not re-pin it to whatever the local build prints; T3 pins
# the identical number as the py↔ts parity proof.
# ---------------------------------------------------------------------------


class MaskedNeHirPinTest(unittest.TestCase):
    def test_masked_ne_fixture_hir_sha(self) -> None:
        def prog() -> None:
            v = Var(0, explicit_mask=0xE0)
            alias = Var(0, explicit_id=v.id)
            alias.set(0xA5)
            h1 = Var(0)
            when([v.ne(0xA0)], lambda: h1.iadd(1))
            h2 = Var(0)
            when([v.ne(0x20)], lambda: h2.iadd(1))
            h3 = Var(0)
            when([v.ne(0)], lambda: h3.iadd(1))
            w = Var(0, explicit_mask=0xFF)
            h4 = Var(0)
            when([w.ne(0x80)], lambda: h4.iadd(1))

        hir = build_module(prog).to_json()
        self.assertEqual(len(hir), 3977)
        self.assertEqual(len(json.loads(hir)["module"]["stmts"]), 11)
        self.assertEqual(
            hashlib.sha256(hir.encode()).hexdigest(),
            "d4859e5c94230f46072ef091dcbeebf777248270e47b2afac4b51629c8a028e3",
        )


if __name__ == "__main__":
    unittest.main()
