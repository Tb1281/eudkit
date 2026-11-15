"""Backing module for TriggerPage-backed containers.

This module is the single home for the indexing arithmetic that FixedArray and
Queue previously re-derived per container: carry-free page/slot EPD weights,
EPD decomposition, element masks, and the runtime PageSlotEPD address
computation. Containers import all of it from here and nowhere else.

Scope is indexing arithmetic only. Allocation (page-slot assignment) stays
per container, and the builder hooks in :mod:`py.trigger` are untouched.
Page-boundary alignment helpers live in the var module, not here, because
alignment is allocation, not indexing arithmetic.
``pageSlotEpdWeight`` itself is defined in :mod:`py.trigger` (it is Trigger
layout, not container arithmetic) and is re-exported here so containers have
one import source; re-defining it here would create a trigger/backing import
cycle.

STR-backed FixedString is not a client of this module: its dynamic byte path uses
STR layout, not page/slot decomposition, and stays outside this seam.
"""

from __future__ import annotations

from typing import Any

from .hir import AtLeast, DeathsX, SetTo
from .trigger import Trigger, pageSlotEpdWeight
from .var import Var, _emit_var_transfer
from .view import EudslError

# Re-exported from trigger so this module is the containers' single import
# source for indexing arithmetic.
__all__ = [
    "ELEM_MASK",
    "PageSlotEPD",
    "pageSlotBitCond",
    "pageSlotEpd",
    "pageSlotEpdWeight",
]

#: Element width mask by element type name.
ELEM_MASK: dict[str, int] = {
    "byte": 0xFF,
    "word": 0xFFFF,
    "dword": 0xFFFFFFFF,
    "var": 0xFFFFFFFF,
}


def pageSlotEpd(base: int, index: int) -> int:
    """Constant carry-free PageSlotEPD offset for a build-time index."""
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        raise EudslError(
            "EUDSL-BOUNDS", f"FixedArray index must be a non-negative integer, got {index!r}"
        )
    epd = base & 0xFFFFFFFF
    for bit in range(32):
        if (index >> bit) & 1:
            epd = (epd + pageSlotEpdWeight(bit)) & 0xFFFFFFFF
    return epd


def pageSlotBitCond(value: Var, bit: int) -> dict[str, Any]:
    """``(value & (1 << bit)) != 0`` as a 1C ``DeathsX`` condition."""
    mask = (1 << bit) & 0xFFFFFFFF
    return DeathsX(value.value, AtLeast, mask, 0, mask)


def PageSlotEPD(base: int | Var, index: int | Var, bits: int = 32) -> Var:
    """Compute ``base + PageSlotEPD(0, index)``, folding constant indices."""
    if isinstance(index, int):
        base_val = base if isinstance(base, int) else 0
        return Var(pageSlotEpd(base_val, index))
    if not (1 <= bits <= 32):
        raise EudslError("EUDSL-BOUNDS", f"PageSlotEPD bits must be in 1..32, got {bits}")
    if isinstance(base, Var):
        out = Var(0)
        _emit_var_transfer(out.id, base.id, SetTo)
    else:
        out = Var(base)
    for bit in range(bits):
        Trigger(conds=[pageSlotBitCond(index, bit)], acts=[out.act_add(pageSlotEpdWeight(bit))])
    return out
