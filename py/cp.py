"""Scoped CurrentPlayer (`0x6509B0`) pointer views and absolute offset hops.
`with_cp(epd, ...)` and `with with_cp(epd) as cp:`

A constant EPD base is the only supported form: the CP register holds an
**EPD**, not a byte address, so a dynamic base would silently write a byte
address into it. Hops are absolute anchors (`SetTo baseEpd + target`), so a
hop inside a replayed runtime loop body re-anchors the cursor on every pass
instead of drifting.
"""

from __future__ import annotations

from typing import Any, Callable

from .hir import (
    Add,
    AddrReloc,
    AtLeast,
    Deaths,
    Exactly,
    SetDeaths,
    SetTo,
    emitVarStore,
)
from .stock_rows_built import ACT_FIELD_OFFSETS
from .temporal import when, while_loop
from .trigger import ACT_COUNT, ACT_STRIDE_EPDS, TRIGGER_STRIDE_EPDS, Trigger
from .var import Var
from .view import (
    CURRENT_PLAYER,
    EPD_CURRENT_PLAYER,
    EudslError,
    FieldSpec,
    MemFieldHandle,
    MemView,
    MemViewInstance,
    defineMemView,
    emit_bit_test_read,
)

#: Byte address of the runtime `CurrentPlayer` register.
CP_ADDR = 0x6509B0

#: `CP_ADDR` viewed as a dword field.
CP_REGISTER_VIEW = defineMemView(
    "CurrentPlayerRegister",
    4,
    {"value": FieldSpec(0, 4)},
)
CP_REGISTER_FIELD = FieldSpec(0, 4)

#: `CurrentPlayer` page-slot stride in EPDs (one 32-byte action slot).
SLOT_STRIDE_EPDS = ACT_STRIDE_EPDS
#: Extra EPDs needed to hop from the last slot of a page to the next page.
PAGE_HOP_EPDS = TRIGGER_STRIDE_EPDS - ACT_COUNT * ACT_STRIDE_EPDS
#: Slots per `TriggerPage`.
PAGE_SLOTS = ACT_COUNT


def read_current_player_register() -> Var:
    """Materialise the dword at ``CP_ADDR`` (the actual CurrentPlayer value)."""
    return MemFieldHandle(
        CP_REGISTER_VIEW,
        CP_REGISTER_FIELD,
        EPD_CURRENT_PLAYER,
        "value",
    ).read()


def emit_seed_cp_from_slot(slot0: Var) -> None:
    """Point CurrentPlayer at the runtime value field of ``slot0``.

    The mode-1 reloc materialises ``EPD(slot0.value)`` into the action's
    value field. The action then writes that EPD into the CurrentPlayer
    register itself, so ``Deaths(CurrentPlayer, ...)`` reads the slot value.
    """
    act = SetDeaths(EPD_CURRENT_PLAYER, SetTo, 0, 0)
    act["relocs"] = [
        {
            "field_offset": ACT_FIELD_OFFSETS["value"],
            "target_var": slot0.id,
            "target_offset": 20,
            "mode": 1,
        }
    ]
    Trigger(conds=[], acts=[act], preserved=True)


def cp_live() -> Any:
    """CurrentPlayer slot holds a non-zero member EPD."""
    return Deaths(CURRENT_PLAYER, AtLeast, 1, 0)


def cp_empty() -> Any:
    """CurrentPlayer slot is the zero sentinel."""
    return Deaths(CURRENT_PLAYER, Exactly, 0, 0)


def emit_cp_read_into(dst: Var) -> None:
    """Read the dword at CurrentPlayer into ``dst`` with 32 bit tests."""
    dst.set(0)
    emit_bit_test_read(CURRENT_PLAYER, 32, 0, dst)


def emit_cp_sweep(slot0: Var, capacity: int, iteration: Callable[[], Any]) -> None:
    """Drive ``iteration`` once per storage slot with a CP cursor.

    The actual CurrentPlayer register is saved once per site execution
    and restored after the loop, so nesting is path-complete even when
    the iteration body clobbers CurrentPlayer via ``with_cp``. The slot
    pointer is carried in ``offset`` (EPDs relative to ``slot0``'s
    value field) and re-seeded into CurrentPlayer before every slot;
    a body clobber therefore cannot corrupt the cursor.
    """
    saved = read_current_player_register()
    remaining = Var(0)
    page_remaining = Var(0)
    offset = Var(0)

    remaining.set(capacity)
    page_remaining.set(min(PAGE_SLOTS, capacity))
    offset.set(0)

    def body() -> None:
        emit_seed_cp_from_slot(slot0)
        emitVarStore(offset.id, AddrReloc(CP_ADDR), Add)
        iteration()
        offset.iadd(SLOT_STRIDE_EPDS)
        remaining.isub(1)
        page_remaining.isub(1)
        when(
            [page_remaining.eq(0)],
            lambda: (
                offset.iadd(PAGE_HOP_EPDS),
                page_remaining.set(PAGE_SLOTS),
            ),
        )

    while_loop([remaining.ge(1)], body)
    emitVarStore(saved.id, AddrReloc(CP_ADDR))


def _require_int_base(base_epd: Any) -> None:
    """Reject a dynamic CP base before any seed act is queued.

    The CP register must hold an EPD, so a `Var` base would seed it with a
    byte-address pointer (reloc mode 4) and every read through the cursor
    would be wrong. A dynamic base is already unusable, so it is rejected
    loudly instead of silently emitting a relative hop.
    """
    if not isinstance(base_epd, int) or isinstance(base_epd, bool):
        raise EudslError(
            "EUDSL-TYPE",
            f"with_cp base must be an int EPD; got {base_epd!r}",
        )


class CPCursor:
    def __init__(self, base_epd: Any) -> None:
        _require_int_base(base_epd)
        self.baseEpd = base_epd
        self.currentOffset: int = 0

    @property
    def current_offset(self) -> int:
        return self.currentOffset

    def moveTo(self, target_dword_offset: int) -> None:
        """Anchor CurrentPlayer at ``baseEpd + target_dword_offset``.

        The act is absolute (`SetTo`), not a delta from build-time state:
        inside a replayed runtime loop body it re-anchors the cursor on
        every pass, so the register cannot drift. Repeating the current
        offset emits nothing.
        """
        from .hir import SetDeaths, SetTo

        if target_dword_offset != self.currentOffset:
            SetDeaths(EPD_CURRENT_PLAYER, SetTo, self.baseEpd + target_dword_offset, 0)
        self.currentOffset = target_dword_offset

    move_to = moveTo

    def at(self, dword_offset: int) -> MemFieldHandle:
        spec = FieldSpec(offset=dword_offset * 4, size=4)
        schema = defineMemView("CP", (dword_offset + 1) * 4, {"val": spec})
        return MemFieldHandle(schema, spec, self, f"at({dword_offset})")

    def view(self, schema_or_ctor: Any) -> Any:
        if hasattr(schema_or_ctor, "schema"):
            return MemViewInstance(schema_or_ctor.schema, self)
        if isinstance(schema_or_ctor, MemView):
            return MemViewInstance(schema_or_ctor, self)
        if callable(schema_or_ctor):
            return schema_or_ctor(self)
        raise ValueError(f"Invalid schema or constructor passed to cp.view(): {schema_or_ctor}")

    def restore(self) -> None:
        """Re-anchor CurrentPlayer at ``baseEpd`` and reset the build-time offset."""
        from .hir import SetDeaths, SetTo

        if self.currentOffset != 0:
            SetDeaths(EPD_CURRENT_PLAYER, SetTo, self.baseEpd, 0)
        self.currentOffset = 0


class CPContextManager:
    def __init__(self, epd: Any) -> None:
        _require_int_base(epd)
        self.epd = epd
        self.cursor: CPCursor | None = None

    def __enter__(self) -> CPCursor:
        from .hir import SetDeaths, SetTo

        _require_int_base(self.epd)
        SetDeaths(EPD_CURRENT_PLAYER, SetTo, self.epd, 0)
        self.cursor = CPCursor(self.epd)
        return self.cursor

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self.cursor is not None:
            self.cursor.restore()


def with_cp(epd: Any, body: Callable[[CPCursor], Any] | None = None) -> Any:
    """Bind CurrentPlayer (`0x6509B0`) to `epd` for `body(cp)` or in `with with_cp(epd) as cp:`.

    `epd` must be a constant int EPD. Hops are absolute (`SetTo base + target`),
    so a hop inside a replayed loop body re-anchors instead of drifting;
    `restore` re-anchors to the base. A dynamic base raises `EUDSL-TYPE` before
    any act is queued, because the CP register holds an EPD, not a byte address.
    """
    cm = CPContextManager(epd)
    if body is not None:
        with cm as cp:
            return body(cp)
    return cm


withCP = with_cp
