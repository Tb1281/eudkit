"""FE1: one field-value-space comparison kernel for all three surfaces.

Mirror of ``ts/tests/test_field_surface_kernel.ts``.

The kernel is asked in the field's **value space**: the bit patterns inside a
field's mask, read as field units (``raw >> shift``). Its largest member is
``(mask & 0xFFFFFFFF) >> shift``, including for *straddling* fields where that
is smaller than the nominal width. From that one number the six comparison
decisions follow:

* a constant outside the space can never be equal (`eq` folds to `Never`) and
  can never be unequal (`ne` folds to `Always`);
* an ordering threshold beyond the space folds by direction;
* an in-space constant is emitted un-masked, shifted into the field's byte
  lane;
* a middle ``ne`` constant defers to the surface's use-site marker.

The expectation columns below are derived from that definition, not from the
kernel's implementation: ``max_value`` is computed independently in
``_expected`` from the width/sub-offset geometry.

The matrix covers widths ``1/2/4``, byte sub-offsets ``0..3`` and the boundary
constants ``0, 1, 0x7F, 0x80, 0xFF, 0x100, 0xFFFF, 0x10000, 0xFFFFFFFF`` for
the kernel itself and for all three surfaces (`TriggerFieldRef`,
`MemFieldHandle`, `Var(explicit_mask=...)`).
"""

from __future__ import annotations

import unittest
from typing import Any

from py import (
    Always,
    AtLeast,
    AtMost,
    Exactly,
    FieldSpec,
    Never,
    TriggerFieldRef,
    Var,
    build_module,
    defineMemView,
)
from py.view import FieldValueSpace, make_memview

_OPS = ("eq", "ne", "ge", "le", "gt", "lt")
_WIDTHS = (1, 2, 4)
_SUBOFFSETS = (0, 1, 2, 3)
_CONSTS = (0, 1, 0x7F, 0x80, 0xFF, 0x100, 0xFFFF, 0x10000, 0xFFFFFFFF)


def _raw_mask(width: int) -> int:
    return 0xFF if width == 1 else (0xFFFF if width == 2 else 0xFFFFFFFF)


def _field_mask(width: int, suboffset: int) -> int:
    return (_raw_mask(width) << (suboffset * 8)) & 0xFFFFFFFF


def _expected(op: str, constant: int, max_value: int) -> tuple[Any, ...]:
    """The independent value-space decision for one comparison."""
    if op == "eq":
        if constant & ~max_value:
            return ("fold", "never")
        return ("emit", Exactly, constant)
    if op == "ne":
        if constant & ~max_value:
            return ("fold", "always")
        if constant == 0:
            return ("emit", AtLeast, 1)
        if constant == max_value:
            return ("emit", AtMost, max_value - 1)
        return ("defer", constant)
    if op == "ge":
        return ("fold", "never") if constant > max_value else ("emit", AtLeast, constant)
    if op == "le":
        return ("fold", "always") if constant > max_value else ("emit", AtMost, constant)
    if op == "gt":
        return ("fold", "never") if constant >= max_value else ("emit", AtLeast, constant + 1)
    if op == "lt":
        if constant == 0:
            return ("fold", "never")
        if constant > max_value:
            return ("fold", "always")
        return ("emit", AtMost, constant - 1)
    raise AssertionError(f"not a comparison op: {op}")


def _plan(result: Any, shift: int, is_var: bool = False) -> tuple[Any, ...]:
    """Normalise one surface/kernel result to the matrix vocabulary."""
    if isinstance(result, dict):
        if result == Always():
            return ("fold", "always")
        if result == Never():
            return ("fold", "never")
        amount = result["amount"] if is_var else result["amount"] >> shift
        return ("emit", result["comparison"], amount & 0xFFFFFFFF)
    return ("defer", int(getattr(result, "ne_const")))


class FieldValueSpaceKernelTest(unittest.TestCase):
    def test_matrix_decisions(self) -> None:
        rows = 0
        for width in _WIDTHS:
            for suboffset in _SUBOFFSETS:
                shift = suboffset * 8
                mask = _field_mask(width, suboffset)
                space = FieldValueSpace(mask, shift, width)
                max_value = (mask & 0xFFFFFFFF) >> shift
                for op in _OPS:
                    for constant in _CONSTS:
                        rows += 1
                        with self.subTest(
                            width=width,
                            suboffset=suboffset,
                            op=op,
                            constant=hex(constant),
                        ):
                            self.assertEqual(space.max_value, max_value)
                            plan = space.compare(op, constant)
                            if plan.fold == "defer":
                                got: tuple[Any, ...] = ("defer", constant)
                            elif plan.fold is not None:
                                got = ("fold", plan.fold)
                            else:
                                got = (
                                    "emit",
                                    plan.comparison,
                                    (plan.value >> shift) & 0xFFFFFFFF,
                                )
                            self.assertEqual(
                                got, _expected(op, constant, max_value)
                            )
        self.assertEqual(rows, 648)


class SurfaceMatrixTest(unittest.TestCase):
    def test_three_surfaces_share_the_kernel(self) -> None:
        rows = 0

        def prog() -> None:
            nonlocal rows
            for width in _WIDTHS:
                for suboffset in _SUBOFFSETS:
                    shift = suboffset * 8
                    mask = _field_mask(width, suboffset)
                    max_value = (mask & 0xFFFFFFFF) >> shift
                    schema = defineMemView(
                        f"Kernel{width}_{suboffset}",
                        8,
                        {"f": FieldSpec(suboffset, width, mask=mask)},
                    )
                    surfaces = (
                        TriggerFieldRef(-1, suboffset, width, 0, 5, mask),
                        make_memview(schema, 3000).f,
                        Var(0, explicit_mask=max_value),
                    )
                    for op in _OPS:
                        for constant in _CONSTS:
                            want = _expected(op, constant, max_value)
                            for surface in surfaces:
                                rows += 1
                                with self.subTest(
                                    surface=type(surface).__name__,
                                    width=width,
                                    suboffset=suboffset,
                                    op=op,
                                    constant=hex(constant),
                                ):
                                    self.assertEqual(
                                        _plan(
                                            getattr(surface, op)(constant),
                                            shift,
                                            isinstance(surface, Var),
                                        ),
                                        want,
                                    )

        build_module(prog)
        self.assertEqual(rows, 3 * 648)


if __name__ == "__main__":
    unittest.main()
